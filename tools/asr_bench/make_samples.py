"""Synthetic Egyptian dictation samples with Microsoft's neural ar-EG voices (edge-tts) → 16 kHz mono WAV."""

import asyncio
import json
import subprocess
from pathlib import Path

import edge_tts

HERE = Path(__file__).parent
VOICES = {"f": "ar-EG-SalmaNeural", "m": "ar-EG-ShakirNeural"}


async def main():
    samples = json.loads((HERE / "samples.json").read_text(encoding="utf-8"))
    out = HERE / "samples"
    out.mkdir(exist_ok=True)
    for s in samples:
        for tag, voice in VOICES.items():
            mp3, wav = out / f"{s['id']}{tag}.mp3", out / f"{s['id']}{tag}.wav"
            if wav.exists():
                continue
            await edge_tts.Communicate(s["text"], voice, rate="+5%").save(str(mp3))
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-ar", "16000", "-ac", "1", str(wav)]
            subprocess.run(cmd, check=True)  # noqa: S603 — a local dev tool, ffmpeg from PATH
            mp3.unlink()
            print(wav.name)


asyncio.run(main())
