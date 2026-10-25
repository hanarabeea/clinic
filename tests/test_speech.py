"""Offline voice: the speech service client + transcribe view (apps/prescriptions/speech.py), which recogniser a desk
page uses, the drug-name list the service boosts, and the service itself (speech/server.py) with a stand-in model."""

import importlib.util
import io
import json
import threading
import wave
from http.client import HTTPConnection
from pathlib import Path

import httpx
import pytest
from django.conf import settings as dj_settings
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse

from apps.prescriptions import speech
from apps.prescriptions.models import Drug, PrescriptionSettings

KEY = "test-key"


def _wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
def service(settings):
    """A stand-in for speech/server.py behind httpx.MockTransport: records requests, answers like the real one."""
    cache.clear()
    state = {"ready": True, "text": "اوجمنتين قرص كل اتناشر ساعه", "requests": []}

    def handler(request):
        state["requests"].append(request)
        if request.headers.get("X-Api-Key") != KEY:
            return httpx.Response(401, json={"error": "unauthorised"})
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": state["ready"], "loading": False, "phrases": 3, "error": ""})
        if not state["ready"]:
            return httpx.Response(503, json={"error": "loading"})
        return httpx.Response(200, json={"text": state["text"], "seconds": 0.3})

    settings.SPEECH_KEY = KEY
    settings.SPEECH_TRANSPORT = httpx.MockTransport(handler)
    yield state
    cache.clear()


@pytest.fixture
def no_service(settings):
    cache.clear()
    settings.SPEECH_KEY = KEY
    settings.SPEECH_TRANSPORT = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    yield
    cache.clear()


# ---------------------------------------------------------------- which recogniser


@pytest.mark.parametrize(
    ("choice", "ready", "expected"),
    [("auto", True, "local"), ("auto", False, "browser"), ("local", False, "local"), ("browser", True, "browser")],
)
def test_engine_choice(org_a, service, choice, ready, expected):
    service["ready"] = ready
    rx = PrescriptionSettings.for_org(org_a)
    rx.voice_engine = choice
    assert speech.engine(rx) == expected


def test_status_is_offline_without_a_service(no_service):
    assert speech.status() == {"ready": False, "loading": False, "error": "offline"}


def test_status_is_cached(service):
    speech.status()
    speech.status()
    assert len([r for r in service["requests"] if r.url.path == "/health"]) == 1


def test_desk_pages_carry_the_engine(client, org_a, make_member, service):
    client.force_login(make_member(org_a, "doctor"))
    html = client.get(reverse("clinical:desk")).content.decode()
    assert 'id="voice-config"' in html and 'data-engine="local"' in html
    assert reverse("prescriptions:transcribe") in html
    PrescriptionSettings.objects.filter(organization=org_a).update(voice_engine="browser")
    assert 'data-engine="browser"' in client.get(reverse("clinical:desk")).content.decode()


# ---------------------------------------------------------------- the transcribe view


def _post(client, body):
    return client.post(reverse("prescriptions:transcribe"), data=body, content_type="audio/wav")


def test_transcribe_returns_the_text_and_forwards_the_audio(client, org_a, make_member, service):
    client.force_login(make_member(org_a, "doctor"))
    wav = _wav()
    resp = _post(client, wav)
    assert resp.status_code == 200 and resp.json() == {"text": "اوجمنتين قرص كل اتناشر ساعه"}
    sent = next(r for r in service["requests"] if r.url.path == "/transcribe")
    assert sent.content == wav and sent.headers["X-Api-Key"] == KEY


@pytest.mark.parametrize("role", ["reception", "viewer", "admin"])
def test_transcribe_needs_a_writer(client, org_a, make_member, service, role):
    client.force_login(make_member(org_a, role))
    assert _post(client, _wav()).status_code == 403
    assert not [r for r in service["requests"] if r.url.path == "/transcribe"]


def test_transcribe_allowed_with_only_prescription_write(client, org_a, service):
    from apps.accounts.models import User
    from apps.organizations.models import Membership

    user = User.objects.create_user(email="rx-only@example.com", password="Strong-Pass-2026!", full_name="x")
    Membership.objects.create(user=user, organization=org_a, role="viewer", granted_permissions=["prescription.write"])
    client.force_login(user)
    assert _post(client, _wav()).status_code == 200


def test_transcribe_get_and_anonymous(client, org_a, make_member, service):
    assert _post(client, _wav()).status_code == 302  # login first
    client.force_login(make_member(org_a, "doctor"))
    assert client.get(reverse("prescriptions:transcribe")).status_code == 405


def test_transcribe_service_down_or_loading(client, org_a, make_member, service, no_service):
    client.force_login(make_member(org_a, "doctor"))
    resp = _post(client, _wav())
    assert resp.status_code == 503 and resp.json()["error"] == "offline"


def test_transcribe_service_loading(client, org_a, make_member, service):
    service["ready"] = False
    client.force_login(make_member(org_a, "doctor"))
    resp = _post(client, _wav())
    assert resp.status_code == 503 and resp.json()["error"] == "loading"


def test_transcribe_refuses_long_audio(client, org_a, make_member, service):
    client.force_login(make_member(org_a, "doctor"))
    assert _post(client, _wav(seconds=70)).status_code == 413
    assert not [r for r in service["requests"] if r.url.path == "/transcribe"]


# ---------------------------------------------------------------- drug-name list


def test_boost_list_is_the_spoken_names(org_a, settings, tmp_path):
    settings.SPEECH_DATA_DIR = tmp_path
    Drug.objects.create(organization=org_a, name="PANADOL EXTRA 24 TABS.", aliases_ar=["بانادول اكسترا"])
    Drug.objects.create(organization=org_a, name="ZITHROMAX 500", aliases_ar=["زيثروماكس /", "زِيثرُوماكس"])
    Drug.objects.create(organization=org_a, name="OLD 1", aliases_ar=["قديمة"], is_active=False)
    Drug.objects.create(organization=org_a, name="AB", aliases_ar=["اب"])  # too short to boost safely
    Drug.objects.create(organization=org_a, name="TELFAST", aliases_ar=["تيلفاسْت إكسترا"])  # marks, hamza, ة
    phrases = speech.boost_phrases()
    assert {"بانادول", "زيثروماكس", "تيلفاست", "بانادول اكسترا", "تيلفاست اكسترا"} <= set(phrases)
    assert "قديمه" not in phrases and "اب" not in phrases
    assert all("ة" not in p and "إ" not in p and "ْ" not in p for p in phrases)

    out = io.StringIO()
    call_command("build_speech_boost", stdout=out)
    path = Path(tmp_path) / "boost.txt"
    assert path.read_text(encoding="utf-8").splitlines() == phrases
    mtime = path.stat().st_mtime_ns
    call_command("build_speech_boost", stdout=io.StringIO())
    assert path.stat().st_mtime_ns == mtime  # unchanged list: the file is left alone (no rebuild in the service)


# ---------------------------------------------------------------- settings screen


def test_rx_settings_show_the_engine_and_its_state(client, org_a, make_member, service):
    client.force_login(make_member(org_a, "owner"))
    html = client.get(reverse("prescriptions:settings")).content.decode()
    assert 'name="voice_engine"' in html and "الصوت على الجهاز شغال" in html
    cache.clear()
    service["ready"] = False
    html = client.get(reverse("prescriptions:settings")).content.decode()
    assert "speech\\setup.bat" in html


# ---------------------------------------------------------------- the service (speech/server.py)


def _server_module():
    spec = importlib.util.spec_from_file_location("speech_server", Path(dj_settings.BASE_DIR) / "speech" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # no torch / NeMo at import time: they load inside Recognizer.load()
    return module


class FakeRecognizer:
    def __init__(self, ready=True):
        self.ready, self.error, self.phrases, self.heard = ready, "", 7, []

    def transcribe(self, audio):
        self.heard.append(len(audio))
        return "بانادول اكسترا"


@pytest.fixture
def server():
    mod = _server_module()
    fake = FakeRecognizer()
    httpd = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(fake, KEY))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield mod, fake, httpd.server_address[1]
    httpd.shutdown()


def _call(port, method, path, body=b"", key=KEY):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers={"X-Api-Key": key} if key else {})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b"{}")


def test_server_needs_the_key(server):
    _mod, _fake, port = server
    assert _call(port, "GET", "/health", key="")[0] == 401
    assert _call(port, "GET", "/health", key="wrong")[0] == 401
    assert _call(port, "GET", "/health") == (200, {"ready": True, "loading": False, "phrases": 7, "error": ""})


def test_server_transcribes_any_rate_as_16k(server):
    _mod, fake, port = server
    status, body = _call(port, "POST", "/transcribe", _wav(seconds=1.0, rate=48000))
    assert status == 200 and body["text"] == "بانادول اكسترا"
    assert fake.heard == [16000]  # resampled to 16 kHz mono


def test_server_refuses_bad_input(server):
    _mod, fake, port = server
    assert _call(port, "POST", "/transcribe", b"not audio")[0] == 400
    assert _call(port, "POST", "/transcribe", _wav(seconds=61))[0] == 413
    assert _call(port, "POST", "/transcribe", _wav(seconds=0.1)) == (200, {"text": "", "seconds": 0.0})
    assert _call(port, "GET", "/other")[0] == 404
    fake.ready, fake.error = False, ""
    assert _call(port, "POST", "/transcribe", _wav())[0] == 503
    assert fake.heard == []


def test_server_never_logs_what_was_said(server):
    import logging

    _mod, _fake, port = server
    lines = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    logger = logging.getLogger("speech")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        _call(port, "POST", "/transcribe", _wav())
    finally:
        logger.removeHandler(handler)
    assert lines and not any("بانادول" in line for line in lines)  # only durations
