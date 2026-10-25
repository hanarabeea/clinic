"""Transcribe every sample with a NeMo ASR model on CPU → results/<name>.json (text + seconds)."""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(HERE / "models"))
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from nemo.collections.asr.models import ASRModel  # noqa: E402

name, repo = sys.argv[1], sys.argv[2]
torch.set_num_threads(int(os.environ.get("THREADS", "4")))
t0 = time.perf_counter()
if repo.endswith(".nemo") or "/" not in repo:
    model = ASRModel.restore_from(repo, map_location="cpu")
else:
    from huggingface_hub import snapshot_download

    folder = Path(snapshot_download(repo))
    nemo_files = sorted(folder.rglob("*.nemo"))
    print("nemo files:", [f.name for f in nemo_files])
    model = ASRModel.restore_from(str(nemo_files[0]), map_location="cpu")
model.eval()
boost = os.environ.get("BOOST")  # a phrase file (one per line): the drug names to expect
if boost:
    from omegaconf import open_dict

    cfg = model.cfg.decoding
    beam = int(os.environ.get("BEAM", "0"))
    with open_dict(cfg):
        cfg.strategy = "beam_batch" if beam else "greedy_batch"
        node = cfg.beam if beam else cfg.greedy
        if beam:
            node.beam_size = beam
            node.allow_cuda_graphs = False
            node.beam_beta = float(os.environ.get("BETA", "0.0"))
        node.boosting_tree.key_phrases_file = boost
        node.boosting_tree.context_score = float(os.environ.get("CONTEXT_SCORE", "1.0"))
        node.boosting_tree.depth_scaling = float(os.environ.get("DEPTH_SCALING", "2.0"))
        node.boosting_tree.use_triton = False
        node.boosting_tree_alpha = float(os.environ.get("ALPHA", "0.5"))
    model.change_decoding_strategy(cfg)
load = time.perf_counter() - t0
print(f"loaded in {load:.1f}s")
rows = []
for wav in sorted((HERE / "samples").glob("*.wav")):
    audio, sr = sf.read(str(wav), dtype="float32")
    t = time.perf_counter()
    with torch.inference_mode():
        out = model.transcribe([audio], batch_size=1, verbose=False)
    hyp = out[0] if not isinstance(out, tuple) else out[0][0]
    text = hyp.text if hasattr(hyp, "text") else str(hyp)
    secs = time.perf_counter() - t
    rows.append({"file": wav.name, "text": text, "seconds": round(secs, 2), "audio": round(len(audio) / sr, 2)})
    print(f"{wav.name} {secs:5.2f}s  {text}")
out_dir = HERE / "results"
out_dir.mkdir(exist_ok=True)
result = {"model": repo, "load": round(load, 1), "rows": rows}
(out_dir / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
