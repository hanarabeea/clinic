"""Score speech-to-text results the way the doctor experiences them: run each transcript through the real prescription
pipeline (investigations.split_requests → extraction.extract, the `dictate` view's path) against the clinic's 25k drug
catalog, and count which expected medicines (best candidate) and tests/scans come out. Also the character error rate
on Arabic-normalised text.

    .venv/Scripts/python tools/asr_bench/score.py <bench dir>      (bench dir = %LOCALAPPDATA%/digiclinic/asr-bench)

"reference" = the scripts' own text: the pipeline's ceiling (what a perfect recogniser would give).
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import django

django.setup()

from apps.core.arabic import normalize_arabic  # noqa: E402
from apps.organizations.models import Organization  # noqa: E402
from apps.prescriptions import extraction  # noqa: E402
from apps.prescriptions.investigations import split_requests  # noqa: E402


def cer(ref, hyp):
    a, b = normalize_arabic(ref).replace(" ", ""), normalize_arabic(hyp).replace(" ", "")
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / max(len(a), 1)


def run(org, text):
    requests, rest = split_requests(text)
    lines = extraction.extract(org, rest)
    drugs = [ln.candidates[0].drug.name for ln in lines if ln.candidates]
    return drugs, [name for name, _kind in requests]


def pick(org, texts, rank_penalty=2.0):
    """Of the recogniser's alternative readings (best first), the one whose medicine names match the catalog best:
    each recognised line adds how far its best match is above the weak threshold; later readings pay a small penalty,
    so the first reading wins unless another one clearly names real drugs better."""
    best, best_score = texts[0], None
    for rank, text in enumerate(texts):
        _requests, rest = split_requests(text)
        lines = extraction.extract(org, rest)
        total = sum(max(ln.candidates[0].score - 80, 0) for ln in lines if ln.candidates) - rank_penalty * rank
        if best_score is None or total > best_score:
            best, best_score = text, total
    return best


def score(org, samples, rows):
    found = expected = req_found = req_expected = extra = 0
    errs, secs, audio, misses = [], 0.0, 0.0, []
    for row in rows:
        s = samples[row["file"][:2]]
        drugs, reqs = run(org, row["text"])
        for want in s["drugs"]:
            expected += 1
            if any(d.upper().startswith(want) for d in drugs):
                found += 1
            else:
                misses.append(f"{row['file']}: {want} ← «{row['text']}» → {drugs}")
        extra += sum(1 for d in drugs if not any(d.upper().startswith(w) for w in s["drugs"]))
        for want in s["requests"]:
            req_expected += 1
            req_found += any(want.lower() in r.lower() for r in reqs)
        errs.append(cer(s["text"], row["text"]))
        secs += row.get("seconds", 0)
        audio += row.get("audio", 0)
    return {
        "drugs": f"{found}/{expected}",
        "drug_recall": round(100 * found / max(expected, 1), 1),
        "wrong_extra_drugs": extra,
        "requests": f"{req_found}/{req_expected}",
        "cer": round(100 * sum(errs) / max(len(errs), 1), 1),
        "speed": round(secs / audio, 2) if audio else None,  # seconds of work per second of speech (<1 = faster)
        "misses": misses,
    }


def main(bench):
    bench = Path(bench)
    org = Organization.objects.get(slug="demo-clinic")
    samples = {s["id"]: s for s in json.loads((bench / "samples.json").read_text(encoding="utf-8"))}
    wavs = sorted(p.name for p in (bench / "samples").glob("*.wav"))
    results = {"reference": [{"file": w, "text": samples[w[:2]]["text"]} for w in wavs]}
    for f in sorted((bench / "results").glob("*.json")):
        rows = json.loads(f.read_text(encoding="utf-8"))["rows"]
        results[f.stem] = rows
        if any("nbest" in r for r in rows):  # also: the best of the alternatives, picked by the catalog
            results[f.stem + "+pick"] = [{**r, "text": pick(org, r.get("nbest") or [r["text"]])} for r in rows]
    report = {name: score(org, samples, rows) for name, rows in results.items()}
    for name, r in report.items():
        print(f"{name:28} drugs {r['drugs']:>6} ({r['drug_recall']:5.1f}%)  extra {r['wrong_extra_drugs']:2}  "
              f"tests {r['requests']:>5}  CER {r['cer']:5.1f}%  speed {r['speed']}")  # fmt: skip
    (bench / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(os.environ["LOCALAPPDATA"]) / "digiclinic" / "asr-bench")
