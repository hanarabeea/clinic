@echo off
REM DigiClinic - install the offline voice (one time, needs internet, ~2.5 GB). After this, run.bat starts it and the
REM doctor's dictation works without internet. Safe to run again (it only adds what is missing).
cd /d "%~dp0\.."
set PYTHONUTF8=1
set UV_NATIVE_TLS=1
set "UV=python -m uv"
if exist "C:\Python313\python.exe" "C:\Python313\python.exe" -m uv --version >nul 2>nul && set "UV="C:\Python313\python.exe" -m uv"
where uv >nul 2>nul && set "UV=uv"
set "SPEECH_ENV=%LOCALAPPDATA%\digiclinic\speech\.venv"

echo [1/4] Creating the voice environment...
if not exist "%SPEECH_ENV%\Scripts\python.exe" %UV% venv --python 3.12 "%SPEECH_ENV%" || goto :error
echo [2/4] Installing torch (CPU)...
%UV% pip install --python "%SPEECH_ENV%\Scripts\python.exe" torch torchaudio --index-url https://download.pytorch.org/whl/cpu || goto :error
echo [3/4] Installing the speech toolkit...
%UV% pip install --python "%SPEECH_ENV%\Scripts\python.exe" -r speech\requirements.txt || goto :error
echo [4/4] Downloading the Arabic speech model (once)...
"%SPEECH_ENV%\Scripts\python.exe" speech\server.py --download || goto :error
.venv\Scripts\python manage.py build_speech_boost
echo.
echo   Offline voice installed. Restart DigiClinic (run.bat) to use it.
pause
goto :eof

:error
echo.
echo   Something failed. Scroll up to see the error, and send a screenshot.
pause
