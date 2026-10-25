"""Transcribe every sample with a faster-whisper model on CPU (int8) → results/<name>.json (text + seconds)."""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(HERE / "models"))
try:
    import truststore  # the machine's AV intercepts TLS: trust the Windows certificate store

    truststore.inject_into_ssl()
except ImportError:
    pass
import soundfile as sf  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

name, repo = sys.argv[1], sys.argv[2]
threads = int(os.environ.get("THREADS", "4"))
t0 = time.perf_counter()
model = WhisperModel(repo, device="cpu", compute_type="int8", cpu_threads=threads)
load = time.perf_counter() - t0
rows = []
for wav in sorted((HERE / "samples").glob("*.wav")):
    t = time.perf_counter()
    audio, _sr = sf.read(str(wav), dtype="float32")  # 16 kHz mono (make_samples / the browser recorder)
    segments, info = model.transcribe(audio, language="ar", beam_size=1, vad_filter=False)
    text = " ".join(s.text.strip() for s in segments)
    secs = time.perf_counter() - t
    rows.append({"file": wav.name, "text": text, "seconds": round(secs, 2), "audio": round(info.duration, 2)})
    print(f"{wav.name} {secs:5.1f}s  {text}")
out = HERE / "results"
out.mkdir(exist_ok=True)
result = {"model": repo, "load": round(load, 1), "rows": rows}
(out / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
