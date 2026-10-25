"""Client for the offline speech service (speech/server.py) + the drug-name list it boosts.

The browser records the doctor (static/src/dictation.js, LocalSpeechProvider), posts each utterance as a WAV to
`prescriptions:transcribe`, which forwards it here. If the service isn't installed / running, the desk falls back to
the browser's own recogniser (Chrome / Edge, needs internet) — `engine()` decides which one a page uses.
"""

import logging
import re

import httpx
from django.conf import settings
from django.core.cache import cache

# "ready" is remembered a minute, a failure only a few seconds: a busy moment (the model decoding) must not flip a page
# to the online recogniser, while a stopped service is noticed quickly.
STATUS_CACHE_SECONDS = 60
STATUS_FAIL_CACHE_SECONDS = 5
log = logging.getLogger(__name__)
_AR_WORD = re.compile(r"^[ء-ي]+$")
_NORM = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه"})
_MARKS = re.compile(r"[ً-ْـ]")


class SpeechError(Exception):
    """The service can't turn this audio into text right now (not installed, loading, stopped, bad audio)."""


def _key():
    return settings.SPEECH_KEY or settings.WA_GATEWAY_KEY


def _client(timeout):
    # plain http on localhost: don't load TLS certificates at all (an antivirus blocking them once broke every page
    # that asked the WhatsApp gateway — same rule as messaging.whatsapp._client)
    return httpx.Client(
        base_url=settings.SPEECH_URL,
        verify=str(settings.SPEECH_URL).startswith("https"),
        headers={"X-Api-Key": _key()},
        timeout=timeout,
        transport=getattr(settings, "SPEECH_TRANSPORT", None),  # tests inject httpx.MockTransport
    )


def status() -> dict:
    """{"ready": bool, "loading": bool, ...} — cached briefly: every desk page asks."""
    cached = cache.get("speech:status")
    if cached is not None:
        return cached
    result = {"ready": False, "loading": False, "error": "offline"}
    if _key():
        try:
            with _client(timeout=3.0) as c:
                r = c.get("/health")
            if r.status_code == 200:
                result = r.json()
            else:
                log.warning("speech service answered %s", r.status_code)
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.info("speech service unreachable (%s)", type(e).__name__)
    cache.set("speech:status", result, STATUS_CACHE_SECONDS if result.get("ready") else STATUS_FAIL_CACHE_SECONDS)
    return result


def engine(rx_settings) -> str:
    """Which recogniser the desk uses: "local" (offline service) or "browser" (Chrome / Edge, online)."""
    choice = rx_settings.voice_engine
    if choice == "browser":
        return "browser"
    if choice == "local":
        return "local"  # forced: the page shows the service's state instead of silently using the internet
    return "local" if status().get("ready") else "browser"  # auto


def transcribe(wav: bytes) -> str:
    try:
        with _client(timeout=60.0) as c:
            r = c.post("/transcribe", content=wav, headers={"Content-Type": "audio/wav"})
    except (httpx.HTTPError, OSError) as e:
        cache.delete("speech:status")
        raise SpeechError("offline") from e
    if r.status_code != 200:
        cache.delete("speech:status")
        is_json = r.headers.get("content-type", "").startswith("application/json")
        raise SpeechError(r.json().get("error", "error") if is_json else "error")
    return r.json().get("text", "")


# --- the drug-name list -----------------------------------------------------------------------------------------


def _norm(word: str) -> str:
    return _MARKS.sub("", word).translate(_NORM)


def boost_phrases() -> list[str]:
    """The Arabic way every active drug is said (aliases: the catalog's transliteration + the spoken forms learned when
    the doctor picked a voice suggestion): its first word, and its first two words. Normalised the way the model
    writes (ة→ه, أ/إ/آ→ا, no diacritics) — the decoder can only boost spellings it would produce."""
    from .models import Drug

    one, two = set(), set()
    for aliases in Drug.objects.filter(is_active=True).values_list("aliases_ar", flat=True).iterator():
        for alias in aliases or ():
            words = [w for w in (_norm(w) for w in alias.split()) if _AR_WORD.match(w)]
            if words and len(words[0]) >= 4:
                one.add(words[0])
            if len(words) >= 2 and len(words[0]) >= 3 and len(words[1]) >= 3:
                two.add(f"{words[0]} {words[1]}")
    return sorted(one) + sorted(two - one)


def write_boost() -> int:
    """(Re)write the list the service reads; only touches the file when it changed (the service rebuilds on change)."""
    path = settings.SPEECH_DATA_DIR / "boost.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(boost_phrases())
    try:
        if path.read_text(encoding="utf-8") == text:
            return text.count("\n") + 1 if text else 0
    except OSError:
        pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # atomic: the service never reads half a file
    return text.count("\n") + 1 if text else 0
