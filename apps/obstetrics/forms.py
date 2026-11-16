from django import forms
from django.utils.translation import gettext_lazy as _

from . import calc
from .models import BabySex, EddMethod, Milestone, ObstetricProfile, Outcome, RiskLevel
from .services import Dating

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DatingForm(forms.Form):
    """How the pregnancy is dated. Only the chosen method's fields matter; services.Dating.edd() says what is
    missing."""

    method = forms.ChoiceField(label=_("طريقة الحساب"), choices=EddMethod.choices, initial=EddMethod.LMP)
    lmp = forms.DateField(label=_("أول يوم في آخر دورة"), required=False, widget=DATE)
    cycle_days = forms.IntegerField(label=_("طول الدورة (يوم)"), required=False, min_value=15, max_value=60)
    us_date = forms.DateField(label=_("تاريخ السونار"), required=False, widget=DATE)
    us_weeks = forms.IntegerField(label=_("العمر في السونار: أسابيع"), required=False, min_value=0, max_value=44)
    us_days = forms.IntegerField(label=_("وأيام"), required=False, min_value=0, max_value=6)
    transfer_date = forms.DateField(label=_("تاريخ نقل الأجنة"), required=False, widget=DATE)
    embryo_age_days = forms.IntegerField(
        label=_("عمر الجنين عند النقل (يوم)"), required=False, min_value=2, max_value=6
    )
    fetuses = forms.IntegerField(label=_("عدد الأجنة"), required=False, min_value=1, max_value=5)

    def dating(self) -> Dating:
        d = self.cleaned_data
        return Dating(
            method=d["method"],
            lmp=d.get("lmp"),
            cycle_days=d.get("cycle_days") or calc.STANDARD_CYCLE,
            us_date=d.get("us_date"),
            us_weeks=d.get("us_weeks"),
            us_days=d.get("us_days") or 0,
            transfer_date=d.get("transfer_date"),
            embryo_age_days=d.get("embryo_age_days") or 5,
        )


class DetailsForm(forms.Form):
    fetuses = forms.IntegerField(label=_("عدد الأجنة"), min_value=1, max_value=5)
    risk_level = forms.ChoiceField(label=_("الخطورة"), choices=RiskLevel.choices)
    risk_notes = forms.CharField(label=_("سبب الخطورة"), required=False, max_length=300)
    notes = forms.CharField(label=_("ملاحظات"), required=False, widget=forms.Textarea(attrs={"rows": 2}))


class OutcomeForm(forms.Form):
    """How a pregnancy ended: closing the followed one (with its date), or a row of the past (with its year)."""

    outcome = forms.ChoiceField(label=_("انتهى بـ"), choices=Outcome.choices)
    ended_on = forms.DateField(label=_("التاريخ"), required=False, widget=DATE)
    year = forms.IntegerField(label=_("السنة"), required=False, min_value=1950, max_value=2100)
    weeks = forms.IntegerField(label=_("في الأسبوع"), required=False, min_value=0, max_value=44)
    living_children = forms.IntegerField(label=_("أطفال عايشين منه"), required=False, min_value=0, max_value=6)
    baby_sex = forms.ChoiceField(label=_("النوع"), required=False, choices=[("", "-"), *BabySex.choices])
    baby_weight_g = forms.IntegerField(label=_("وزن المولود (جرام)"), required=False, min_value=200, max_value=7000)
    place = forms.CharField(label=_("المكان"), required=False, max_length=100)
    complications = forms.CharField(label=_("مضاعفات / ملاحظات"), required=False, max_length=300)

    def details(self) -> dict:
        d = self.cleaned_data
        return {
            "living_children": d.get("living_children"),
            "baby_sex": d.get("baby_sex") or "",
            "baby_weight_g": d.get("baby_weight_g"),
            "place": d.get("place") or "",
            "complications": d.get("complications") or "",
        }


class ProfileForm(forms.ModelForm):
    cycle_regular = forms.TypedChoiceField(
        label=_("انتظام الدورة"),
        required=False,
        choices=[("", "-"), ("1", _("منتظمة")), ("0", _("مش منتظمة"))],
        coerce=lambda v: v == "1",
        empty_value=None,
    )

    class Meta:
        model = ObstetricProfile
        fields = ["blood_group", "husband_blood_group", "cycle_days", "cycle_regular", "contraception", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.cycle_regular is not None:
            self.initial["cycle_regular"] = "1" if self.instance.cycle_regular else "0"


class MilestoneForm(forms.ModelForm):
    class Meta:
        model = Milestone
        fields = ["name", "kind", "from_week", "to_week", "rh_negative_only", "is_active"]

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.org = org
        for name in ("from_week", "to_week"):
            self.fields[name].widget.attrs.update({"dir": "ltr", "inputmode": "numeric"})

    def clean(self):
        data = super().clean()
        first, last = data.get("from_week"), data.get("to_week")
        if first is not None and last is not None and last < first:
            self.add_error("to_week", _("آخر أسبوع لازم يكون بعد أول أسبوع."))
        name = " ".join((data.get("name") or "").split())
        if name and Milestone.objects.filter(organization=self.org, name=name).exclude(pk=self.instance.pk).exists():
            self.add_error("name", _("فيه محطة بنفس الاسم."))
        data["name"] = name
        return data

    def validate_unique(self):
        pass  # checked per clinic above (the form has no organization field)
