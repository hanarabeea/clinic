# Offline speech-to-text bench

Compares local speech models on prescription dictation, the way the doctor experiences it: each transcript goes through
the real pipeline (`split_requests` → `extraction.extract`, 25k-drug catalog) and the expected drugs / tests are counted.

Runs in its own environment (heavy packages: torch CPU, NeMo, faster-whisper), never in the project's `.venv`:

    set B=%LOCALAPPDATA%\digiclinic\asr-bench
    uv venv --python 3.12 %B%\.venv
    uv pip install --python %B%\.venv\Scripts\python.exe edge-tts faster-whisper soundfile truststore
    uv pip install --python %B%\.venv\Scripts\python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cpu
    uv pip install --python %B%\.venv\Scripts\python.exe "nemo_toolkit[asr]"
    copy samples.json make_samples.py run_*.py %B%

    %B%\.venv\Scripts\python %B%\make_samples.py                       # synthetic ar-EG voices (needs internet once)
    %B%\.venv\Scripts\python %B%\run_whisper.py whisper-turbo mobiuslabsgmbh/faster-whisper-large-v3-turbo
    %B%\.venv\Scripts\python %B%\run_nemo.py lemura lemuralabs/lemura-arabic-asr
    set BEAM=8& set ALPHA=1.3& set BOOST=%B%\boost_catalog.txt
    %B%\.venv\Scripts\python %B%\run_nemo.py lemura-beam8-boost-1.3 lemuralabs/lemura-arabic-asr
    .venv\Scripts\python tools\asr_bench\score.py                      # project venv: scores every results/*.json

Real recordings: put 16 kHz mono WAVs named `<id><tag>.wav` in `%B%\samples` with their text in `samples.json`.
