"""DigiClinic offline speech-to-text service: the doctor's voice is turned into text on this computer, no internet.

Model: lemura-arabic-asr (FastConformer-CTC, ~115M parameters, CC-BY-4.0 — multi-dialect Arabic incl. Egyptian), run on
the CPU with NeMo. Decoding is a beam search boosted with the clinic's drug names (the phrase file Django writes —
`manage.py build_speech_boost`), which is what makes names like "بانادول اكسترا" / "زيثروماكس" come out right
(tools/asr_bench: drug recall 54% → 75% on the synthetic bench, ~0.1 s per second of speech).

Like the WhatsApp gateway: listens on 127.0.0.1 (SPEECH_PORT, default 3321), every request needs the X-Api-Key header
(SPEECH_KEY, default WA_GATEWAY_KEY), settings come from the project's .env. Never logs what was said.

    GET  /health      → {"ready": bool, "loading": bool, "phrases": int, "error": str}
    POST /transcribe  (body: a WAV file, any rate / channels, ≤ 60 s) → {"text": str, "seconds": float}

Runs in its own environment (torch + NeMo are big): speech\\setup.bat creates it; run.bat starts this when it exists.
"""

import io
import json
import logging
import os
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "lemuralabs/lemura-arabic-asr"
SAMPLE_RATE = 16000
MAX_SECONDS = 60
MAX_BODY = (MAX_SECONDS * SAMPLE_RATE * 2 * 2) + 4096  # 60 s of 16-bit stereo at 16 kHz, plus the header
BEAM_SIZE = 8
BOOST_ALPHA = 1.3  # stronger than ~1.6 starts bending ordinary words into drug names (tools/asr_bench)

log = logging.getLogger("speech")


def load_env():
    """KEY=VALUE lines from the project's .env (real environment variables win) — same as the WhatsApp gateway."""
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def data_dir() -> Path:
    """Model files + the drug-name list. Django writes the list into the same folder (settings.SPEECH_DATA_DIR)."""
    default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "digiclinic" / "speech"
    path = Path(os.environ.get("SPEECH_DATA_DIR") or default)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_wav(body: bytes):
    """16-bit PCM WAV (what dictation.js sends) → mono float32 at 16 kHz, resampled if recorded at another rate."""
    import numpy as np

    with wave.open(io.BytesIO(body), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("16-bit PCM only")
    audio = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE and len(audio):
        n = round(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype("float32")
    return audio


class Recognizer:
    """The NeMo model + boosted beam search. One utterance at a time (a lock): a clinic has one doctor dictating."""

    def __init__(self, boost_file: Path, threads: int):
        self.boost_file = boost_file
        self.threads = threads
        self.model = None
        self.error = ""
        self.phrases = 0
        self._boost_mtime = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.model is not None

    def load(self):
        try:
            os.environ.setdefault("HF_HOME", str(data_dir() / "models"))
            try:
                import truststore  # antivirus TLS interception on clinic PCs: trust the Windows certificate store

                truststore.inject_into_ssl()
            except ImportError:
                pass
            import torch
            from huggingface_hub import snapshot_download
            from nemo.collections.asr.models import ASRModel

            torch.set_num_threads(self.threads)
            folder = Path(snapshot_download(MODEL_ID))
            model = ASRModel.restore_from(str(sorted(folder.rglob("*.nemo"))[0]), map_location="cpu")
            model.eval()
            self.model = model
            self.refresh_boost(force=True)
            log.info("model ready (%s phrases)", self.phrases)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            log.exception("model failed to load")

    def refresh_boost(self, force=False):
        """Re-read the drug-name list when Django rewrote it (checked before each utterance; a rebuild takes ~1 s)."""
        try:
            mtime = self.boost_file.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._boost_mtime:
            return
        from omegaconf import open_dict

        cfg = self.model.cfg.decoding
        with open_dict(cfg):
            if mtime is None:  # no list yet: plain greedy decoding
                cfg.strategy = "greedy_batch"
                self.phrases = 0
            else:
                cfg.strategy = "beam_batch"
                cfg.beam.beam_size = BEAM_SIZE
                cfg.beam.allow_cuda_graphs = False
                cfg.beam.beam_beta = 0.0
                cfg.beam.boosting_tree.key_phrases_file = str(self.boost_file)
                cfg.beam.boosting_tree.context_score = 1.0
                cfg.beam.boosting_tree.depth_scaling = 2.0
                cfg.beam.boosting_tree.use_triton = False
                cfg.beam.boosting_tree_alpha = BOOST_ALPHA
                self.phrases = sum(1 for line in self.boost_file.read_text(encoding="utf-8").splitlines() if line)
        self.model.change_decoding_strategy(cfg, verbose=False)
        self._boost_mtime = mtime

    def transcribe(self, audio) -> str:
        import torch

        with self._lock:
            self.refresh_boost()
            with torch.inference_mode():
                signal = torch.from_numpy(audio)[None]
                log_probs, enc_len, _greedy = self.model.forward(
                    input_signal=signal, input_signal_length=torch.tensor([len(audio)])
                )
                hyps = self.model.decoding.ctc_decoder_predictions_tensor(
                    log_probs, decoder_lengths=enc_len, return_hypotheses=False
                )
        first = hyps[0] if not isinstance(hyps, tuple) else hyps[0][0]
        text = first.text if hasattr(first, "text") else str(first)
        return " ".join(text.replace("⁇", " ").split())


def make_handler(recognizer, key: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DigiClinicSpeech/1"

        def log_message(self, fmt, *args):  # the default logs every request line; keep the console quiet
            pass

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if key and self.headers.get("X-Api-Key") == key:
                return True
            self._send(401, {"error": "unauthorised"})
            return False

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            if not self._authorised():
                return None
            return self._send(
                200,
                {
                    "ready": recognizer.ready,
                    "loading": not recognizer.ready and not recognizer.error,
                    "phrases": recognizer.phrases,
                    "error": recognizer.error,
                },
            )

        def do_POST(self):
            if self.path != "/transcribe":
                return self._send(404, {"error": "not found"})
            if not self._authorised():
                return None
            size = int(self.headers.get("Content-Length") or 0)
            if size <= 0 or size > MAX_BODY:
                self.close_connection = True  # the unread body must not be taken for the next request
                return self._send(413, {"error": "audio missing or longer than 60 seconds"})
            body = self.rfile.read(size)  # always read it before answering, or the client sees a reset connection
            if not recognizer.ready:
                return self._send(503, {"error": recognizer.error or "loading"})
            try:
                audio = read_wav(body)
            except Exception:
                return self._send(400, {"error": "not a WAV file"})
            if len(audio) > MAX_SECONDS * SAMPLE_RATE:
                return self._send(413, {"error": "longer than 60 seconds"})
            if len(audio) < SAMPLE_RATE // 5:  # under 0.2 s: nothing was said
                return self._send(200, {"text": "", "seconds": 0.0})
            started = time.perf_counter()
            text = recognizer.transcribe(audio)
            seconds = round(time.perf_counter() - started, 2)
            log.info("utterance %.1fs audio → %.2fs", len(audio) / SAMPLE_RATE, seconds)  # never the words
            return self._send(200, {"text": text, "seconds": seconds})

    return Handler


def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s speech %(levelname)s %(message)s")
    threads = int(os.environ.get("SPEECH_THREADS", str(max(1, min(4, (os.cpu_count() or 2) // 2)))))
    recognizer = Recognizer(data_dir() / "boost.txt", threads)
    if "--download" in sys.argv:  # setup.bat: fetch + load the model once (then the service works offline)
        recognizer.load()
        sys.exit(1 if recognizer.error else 0)
    key = os.environ.get("SPEECH_KEY") or os.environ.get("WA_GATEWAY_KEY") or ""
    if not key:
        sys.exit("SPEECH_KEY (or WA_GATEWAY_KEY) is not set in .env — refusing to start without a key.")
    host = os.environ.get("SPEECH_HOST", "127.0.0.1")
    port = int(os.environ.get("SPEECH_PORT", "3321"))
    threading.Thread(target=recognizer.load, daemon=True).start()  # /health answers "loading" meanwhile
    server = ThreadingHTTPServer((host, port), make_handler(recognizer, key))
    log.info("listening on http://%s:%s (model loading in the background)", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
