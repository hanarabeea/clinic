"""Safety warnings for the prescription builder (03 §3.6, plan 15 §0.1-0.2, §2.5).

What is checked, for every line of a draft:
- allergies: allergy text vs the drug's trade name, generic name and Arabic aliases, plus a small table of drug classes
  ("Penicillin" also matches Amoxicillin, Flucloxacillin...);
- the same medicine written twice;
- pregnancy (a followed pregnancy, by its week) and breastfeeding (a live birth in the last
  `PrescriptionSettings.lactation_months`), from `drug_rules.PREGNANCY` / `LACTATION`;
- interactions between the lines, and between a line and the patient's current medications
  (`patients.CurrentMedication`), from `drug_rules.INTERACTIONS`.

It is a safety net, not a clinical decision system: the doctor must still review. Every warning whose level needs it
(`drug_rules.NEEDS_ACK`) must be acknowledged before the prescription is finalized; `info` lines are advice only.
"""

import re
from dataclasses import dataclass
from datetime import timedelta

from apps.core.arabic import normalize_arabic

from . import drug_rules
from .drug_rules import AVOID, CAUTION, NEEDS_ACK

# class keyword (as a doctor would write the allergy, English or Arabic) → generic names it covers
DRUG_CLASSES = {
    "penicillin": ["penicillin", "amoxicillin", "ampicillin", "flucloxacillin", "cloxacillin", "piperacillin",
                   "benzathine", "phenoxymethyl"],
    "بنسلين": ["penicillin", "amoxicillin", "ampicillin", "flucloxacillin", "cloxacillin", "piperacillin",
               "benzathine", "phenoxymethyl"],
    "cephalosporin": ["cef", "ceph"],
    "sulfa": ["sulfamethoxazole", "sulfasalazine", "sulfadiazine"],
    "سلفا": ["sulfamethoxazole", "sulfasalazine", "sulfadiazine"],
    "nsaid": ["ibuprofen", "diclofenac", "naproxen", "ketoprofen", "acetylsalicylic", "aspirin", "mefenamic",
              "celecoxib", "meloxicam"],
    "aspirin": ["acetylsalicylic", "aspirin"],
    "اسبرين": ["acetylsalicylic", "aspirin"],
    "macrolide": ["azithromycin", "clarithromycin", "erythromycin"],
    "quinolone": ["ciprofloxacin", "levofloxacin", "moxifloxacin", "ofloxacin"],
}  # fmt: skip


def _norm(text):
    return normalize_arabic((text or "").lower()).strip()


def allergy_terms(allergy_name):
    """Every lower-case term that an allergy entry should match."""
    name = _norm(allergy_name)
    terms = {name} if len(name) >= 3 else set()
    for keyword, members in DRUG_CLASSES.items():
        if _norm(keyword) in name:
            terms.update(members)
    return terms


def drug_haystack(item):
    parts = [item.drug_name]
    drug = getattr(item, "drug", None)
    if drug is not None:
        parts += [drug.name, drug.generic_name, *drug.aliases_ar]
    return " | ".join(_norm(p) for p in parts if p)


# --- the patient's state: pregnant (which week) or breastfeeding ------------------------------------------------


@dataclass(frozen=True)
class PatientState:
    pregnancy_weeks: int | None = None  # set while a pregnancy is being followed
    lactating: bool = False

    @property
    def pregnant(self) -> bool:
        return self.pregnancy_weeks is not None

    @property
    def any(self) -> bool:
        return self.pregnant or self.lactating


NONE = PatientState()


def patient_state(patient, *, on=None, lactation_months=None) -> PatientState:
    """Pregnant (a followed pregnancy) or breastfeeding (a live birth recorded with its date in the last
    `lactation_months`; the clinic's setting when not given). Men and records without dates are never flagged."""
    from apps.obstetrics import services as ob
    from apps.obstetrics.models import LIVE_BIRTHS, Pregnancy, PregnancyHistory, PregnancyStatus
    from apps.patients.models import Gender

    from .models import PrescriptionSettings

    if patient is None or patient.gender != Gender.FEMALE:
        return NONE
    on = on or ob.today()
    active = Pregnancy.objects.filter(patient=patient, status=PregnancyStatus.ACTIVE).first()
    if active is not None:
        return PatientState(pregnancy_weeks=active.ga(on).weeks)
    if lactation_months is None:
        lactation_months = PrescriptionSettings.for_org(patient.organization).lactation_months
    if not lactation_months:
        return NONE
    since = on - timedelta(days=round(lactation_months * 30.44))
    births = PregnancyHistory.objects.filter(
        patient=patient, is_removed=False, outcome__in=LIVE_BIRTHS, pregnancy__ended_on__gte=since
    )
    return PatientState(lactating=births.exists())


# --- matching ---------------------------------------------------------------------------------------------------


def _ingredients(name, drug=None) -> str:
    """Trade + generic name, lower-case, for the English rule terms."""
    parts = [name or ""]
    if drug is not None:
        parts += [drug.name, drug.generic_name]
    return " | ".join(p.lower() for p in parts if p)


def _has(hay: str, terms) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(t)}", hay) for t in terms)


def _is_local(form) -> bool:
    return (form or "").strip() in drug_rules.LOCAL_FORMS


def _rule_hits(rules, hay, form, weeks):
    for rule in rules:
        if _has(hay, rule.terms):
            level = rule.level_at(weeks, _is_local(form))
            if level:
                yield rule, level


def drug_flags(drug, state: PatientState) -> list[dict]:
    """Short tags for a catalog drug in the search results: [{level, label}] (pregnancy / breastfeeding)."""
    if not state.any:
        return []
    hay = _ingredients(drug.name, drug)
    out = []
    if state.pregnant:
        for _rule, level in _rule_hits(drug_rules.PREGNANCY, hay, drug.form, state.pregnancy_weeks):
            out.append({"level": level, "label": "ممنوع في الحمل" if level == AVOID else "حذر في الحمل"})
            break
    if state.lactating:
        for _rule, level in _rule_hits(drug_rules.LACTATION, hay, drug.form, None):
            out.append({"level": level, "label": "ممنوع مع الرضاعة" if level == AVOID else "حذر مع الرضاعة"})
            break
    return out


# --- warnings ---------------------------------------------------------------------------------------------------


def _warning(key, kind, level, text):
    return {"key": key, "kind": kind, "level": level, "text": text, "needs_ack": level in NEEDS_ACK}


def warnings_for(rx, state: PatientState | None = None, medications=None):
    """[{key, kind, level, text, needs_ack}]. `key` is stable so an acknowledgement survives re-rendering."""
    items = list(rx.items.select_related("drug"))
    out = []
    for allergy in rx.patient.allergies.all():
        terms = allergy_terms(allergy.name)
        for item in items:
            hay = drug_haystack(item)
            if any(t and t in hay for t in terms):
                text = f"المريض عنده حساسية من {allergy.name}، و{item.drug_name} ممكن يكون منها"
                out.append(_warning(f"allergy:{allergy.pk}:{item.pk}", "allergy", AVOID, text))
    seen = {}
    for item in items:
        ident = item.drug_id or re.sub(r"\s+", " ", _norm(item.drug_name))
        if ident in seen:
            out.append(_warning(f"duplicate:{seen[ident]}:{item.pk}", "duplicate", CAUTION,
                                f"{item.drug_name} مكتوب مرتين"))  # fmt: skip
        else:
            seen[ident] = item.pk

    state = patient_state(rx.patient) if state is None else state
    hays = {item.pk: _ingredients(item.drug_name, item.drug) for item in items}
    if state.pregnant:
        weeks = state.pregnancy_weeks
        for item in items:
            for rule, level in _rule_hits(drug_rules.PREGNANCY, hays[item.pk], item.form, weeks):
                out.append(_warning(f"pregnancy:{rule.key}:{item.pk}", "pregnancy", level,
                                    f"حامل في الأسبوع {weeks}: {item.drug_name}، {rule.reason}"))  # fmt: skip
                break
    if state.lactating:
        for item in items:
            for rule, level in _rule_hits(drug_rules.LACTATION, hays[item.pk], item.form, None):
                out.append(_warning(f"lactation:{rule.key}:{item.pk}", "lactation", level,
                                    f"لو بترضع: {item.drug_name}، {rule.reason}"))  # fmt: skip
                break

    if medications is None:
        medications = list(rx.patient.medications.active().select_related("drug"))
    # what the patient already takes counts as a second side; a cream or drop rarely interacts
    current = [(f"m{m.pk}", m.name, _ingredients(m.name, m.drug), m.drug.form if m.drug else "") for m in medications]
    lines = [(str(i.pk), i.drug_name, hays[i.pk], i.form) for i in items]
    out += _interactions(lines, current)
    return out


def _interactions(lines, current):
    out, done = [], set()
    for rule in drug_rules.INTERACTIONS:
        for a_id, a_name, a_hay, a_form in lines + current:
            if not _has(a_hay, rule.a) or (_is_local(a_form) and not rule.local_counts):
                continue
            for b_id, b_name, b_hay, b_form in lines + current:
                if b_id == a_id or not _has(b_hay, rule.b) or (_is_local(b_form) and not rule.local_counts):
                    continue
                if a_id.startswith("m") and b_id.startswith("m"):
                    continue  # two medicines she already takes: not this prescription's business
                pair = (rule.key, *sorted((a_id, b_id)))
                if pair in done:
                    continue
                done.add(pair)
                text = rule.text.format(a=a_name, b=b_name)
                if a_id.startswith("m") or b_id.startswith("m"):
                    text += " (من الأدوية اللي بتاخدها)"
                out.append(_warning(f"interaction:{rule.key}:{a_id}:{b_id}", "interaction", rule.level, text))
    return out


def pending(warnings, acknowledged) -> list[dict]:
    """The warnings that still block finalizing."""
    acknowledged = set(acknowledged)
    return [w for w in warnings if w["needs_ack"] and w["key"] not in acknowledged]
