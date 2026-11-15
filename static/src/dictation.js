/*
 * DigiClinic voice dictation (docs/plan/13-voice-dictation.md).
 *
 * Any <textarea>/<input> with `data-dictate` gets a 🎤 button (+ ع/EN toggle). Click (or Ctrl+Space in the field)
 * starts; it keeps recording (through silences) until the mic is pressed again. Interim text shows grey under the field; final text is inserted
 * at the cursor (never replaces what's written) and fires `input`, so the field's autosave runs.
 * Spoken commands: "سطر جديد" → new line, "نقطة" → ".".
 *
 * `data-dictate="rx"` (prescription): finals are collected instead of inserted; on stop the text is POSTed to
 * `data-dictate-url` and the server's line-by-line drug suggestions are shown in `data-dictate-target`.
 *
 * Providers implement: isSupported(), start(lang), stop(), onInterim(cb), onFinal(cb), onError(cb), onEnd(cb)
 * (+ optional onStatus(cb), graceMs). WebSpeechProvider = the browser's recogniser (Chrome / Edge, online);
 * LocalSpeechProvider = the offline service on this computer (speech/server.py), used when the desk's
 * #voice-config says engine "local" (PrescriptionSettings.voice_engine + the service running).
 */
(() => {
  "use strict";

  class WebSpeechProvider {
    static isSupported() {
      return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition) && window.isSecureContext !== false;
    }

    constructor() {
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      this.rec = new Recognition();
      this.rec.continuous = true;
      this.rec.interimResults = true;
      this.handlers = { interim: () => {}, final: () => {}, error: () => {}, end: () => {} };
      this.rec.onresult = (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const result = event.results[i];
          if (result.isFinal) this.handlers.final(result[0].transcript);
          else interim += result[0].transcript;
        }
        this.handlers.interim(interim);
      };
      this.rec.onerror = (event) => this.handlers.error(event.error || "error");
      this.rec.onend = () => this.handlers.end();
    }

    start(lang) { this.rec.lang = lang; this.rec.start(); }
    stop() { try { this.rec.stop(); } catch (e) { /* already stopped */ } }
    onInterim(cb) { this.handlers.interim = cb; }
    onFinal(cb) { this.handlers.final = cb; }
    onError(cb) { this.handlers.error = cb; }
    onEnd(cb) { this.handlers.end = cb; }
  }

  /*
   * The offline recogniser (speech/server.py on this computer — no internet). The mic is recorded here; a simple
   * voice-activity detector cuts it at each pause, and every utterance is posted as a 16 kHz WAV to
   * #voice-config[data-transcribe-url]; its text comes back as a "final". Utterances are sent one after another, so
   * their texts arrive in the order they were said. Arabic only (the English toggle uses the browser's recogniser).
   */
  class LocalSpeechProvider {
    static isSupported() {
      return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
        && (window.AudioContext || window.webkitAudioContext)) && window.isSecureContext !== false;
    }

    constructor() {
      this.handlers = { interim: () => {}, final: () => {}, error: () => {}, end: () => {}, status: () => {} };
      this.url = (document.getElementById("voice-config") || {}).dataset?.transcribeUrl;
      this.graceMs = 15000;  // after stop: wait for the last utterances to come back from the service
      this.queue = Promise.resolve();
      this.speech = [];  // frames of the utterance being said
      this.preroll = [];  // the moment just before speech starts (first syllable)
      this.speaking = false;
      this.quietMs = 0;
      this.floor = 0.005;  // running estimate of the room's noise level
      this.stopped = false;
      this.rate = LocalSpeechProvider.RATE;
      this.silenceMs = LocalSpeechProvider.SILENCE_MS;  // longer for the prescription mic (more context, nothing shown live)
    }

    start() {
      this._openMic().catch((e) => {
        const name = (e && e.name) || "";
        this.handlers.error(name === "NotAllowedError" || name === "SecurityError" ? "not-allowed" : "audio-capture");
      });
    }

    async _openMic() {
      // the raw mic: the browser's echo cancellation / noise suppression / gain control are tuned for calls and cut
      // the high frequencies (س ش ف ت…) — on a real voice they left ~0.2% of the energy above 3 kHz (≈3% is normal)
      // and both local models misheard almost every drug name. The browser's own recogniser copes; these don't.
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      const Ctx = window.AudioContext || window.webkitAudioContext;
      try { this.ctx = new Ctx({ sampleRate: LocalSpeechProvider.RATE }); } catch (e) { this.ctx = new Ctx(); }
      this.rate = this.ctx.sampleRate;
      const source = this.ctx.createMediaStreamSource(this.stream);
      this.node = this.ctx.createScriptProcessor(2048, 1, 1);  // ~0.13 s blocks: pauses measured finely
      this.node.onaudioprocess = (ev) => this._onFrame(new Float32Array(ev.inputBuffer.getChannelData(0)), this.rate);
      source.connect(this.node);
      this.node.connect(this.ctx.destination);
      this.handlers.status("🎙");
    }

    // one block of audio (~0.25 s): speech or silence? cut the utterance after SILENCE_MS of quiet
    _onFrame(frame, rate) {
      if (this.stopped) return;
      let sum = 0;
      for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
      const rms = Math.sqrt(sum / frame.length);
      const ms = (frame.length / rate) * 1000;
      // hysteresis: starting needs a clear level; once talking, half of it keeps the utterance going (the browser's
      // noise suppression / gain control make short dips inside words that must not cut a sentence in two)
      const start = Math.max(LocalSpeechProvider.MIN_LEVEL, this.floor * 3);
      const loud = rms > (this.speaking ? start / 2 : start);
      if (!loud) this.floor = this.floor * 0.95 + rms * 0.05;
      if (loud) {
        if (!this.speaking) { this.speaking = true; this.speech = [...this.preroll]; this.voicedMs = 0; }
        this.voicedMs += ms;
        this.quietMs = 0;
      } else if (this.speaking) {
        this.quietMs += ms;
      }
      if (this.speaking) {
        this.speech.push(frame);
        const length = this.speech.reduce((n, f) => n + f.length, 0) / rate * 1000;
        if (this.quietMs >= this.silenceMs || length >= LocalSpeechProvider.MAX_UTTERANCE_MS) {
          this._cut(rate);
        }
      } else {
        this.preroll = [...this.preroll, frame].slice(-4);
      }
    }

    _cut(rate) {
      const frames = this.speech;
      const voiced = this.voicedMs;
      this.speech = []; this.speaking = false; this.quietMs = 0; this.voicedMs = 0;
      if (voiced < LocalSpeechProvider.MIN_UTTERANCE_S * 1000) return;  // a cough / click (the quiet after it doesn't count)
      const wav = LocalSpeechProvider.wav(frames, rate);
      this.handlers.status("⏳");
      this.queue = this.queue.then(() => this._send(wav));
    }

    async _send(wav) {
      let token = "";
      try { token = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ""; } catch (e) { /* no cookie access */ }
      token = token || document.querySelector("[name=csrfmiddlewaretoken]")?.value
        || (JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || "");
      try {
        const r = await fetch(this.url, { method: "POST", body: wav, headers: { "Content-Type": "audio/wav", "X-CSRFToken": token } });
        if (!r.ok) { this.handlers.error(r.status === 503 ? "local-offline" : "local-error"); return; }
        const data = await r.json();
        if (data.text) this.handlers.final(data.text);
        if (!this.stopped) this.handlers.status("🎙");
      } catch (e) {
        this.handlers.error("local-offline");
      }
    }

    stop() {
      if (this.stopped) return;
      if (this.speaking) this._cut(this.rate);  // the words said just before the stop press
      this.stopped = true;
      try { this.node && this.node.disconnect(); } catch (e) { /* already */ }
      try { this.stream && this.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* already */ }
      try { this.ctx && this.ctx.close(); } catch (e) { /* already */ }
      this.queue.then(() => this.handlers.end());
    }

    // mono 16-bit PCM WAV at 16 kHz (resampled when the browser recorded at another rate)
    static wav(frames, rate) {
      const total = frames.reduce((n, f) => n + f.length, 0);
      const joined = new Float32Array(total);
      let at = 0;
      frames.forEach((f) => { joined.set(f, at); at += f.length; });
      const out = LocalSpeechProvider.RATE;
      const n = rate === out ? total : Math.floor(total * out / rate);
      const view = new DataView(new ArrayBuffer(44 + n * 2));
      const text = (o, s) => [...s].forEach((c, i) => view.setUint8(o + i, c.charCodeAt(0)));
      text(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); text(8, "WAVE"); text(12, "fmt ");
      view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
      view.setUint32(24, out, true); view.setUint32(28, out * 2, true); view.setUint16(32, 2, true);
      view.setUint16(34, 16, true); text(36, "data"); view.setUint32(40, n * 2, true);
      for (let i = 0; i < n; i++) {
        const v = rate === out ? joined[i] : joined[Math.min(total - 1, Math.floor(i * rate / out))];
        view.setInt16(44 + i * 2, Math.max(-1, Math.min(1, v)) * 0x7fff, true);
      }
      return new Blob([view], { type: "audio/wav" });
    }

    onInterim(cb) { this.handlers.interim = cb; }
    onFinal(cb) { this.handlers.final = cb; }
    onError(cb) { this.handlers.error = cb; }
    onEnd(cb) { this.handlers.end = cb; }
    onStatus(cb) { this.handlers.status = cb; }
  }
  LocalSpeechProvider.RATE = 16000;
  LocalSpeechProvider.SILENCE_MS = 800;  // a pause this long ends an utterance (sent at once: text appears while talking)
  LocalSpeechProvider.COLLECT_SILENCE_MS = 1500;  // the prescription mic (collect-and-post): fewer, longer utterances
  LocalSpeechProvider.MAX_UTTERANCE_MS = 15000;  // a long run without pauses is cut anyway
  LocalSpeechProvider.MIN_UTTERANCE_S = 0.3;  // a cough / click is not sent
  LocalSpeechProvider.MIN_LEVEL = 0.01;  // quieter than this is never speech

  // #voice-config[data-engine] (speech.engine): "local" = the offline service only; "browser" = the browser's;
  // "auto" = the browser's while there's internet, the offline service (when running) without — and for the rest of the
  // page once the browser's recogniser reported a network error. English always uses the browser's (Arabic model).
  let offline = false;
  const voiceConfig = () => document.getElementById("voice-config")?.dataset || {};
  const localUsable = () => LocalSpeechProvider.isSupported() && langPref() === "ar-EG";
  function canFallBackToLocal() {
    const cfg = voiceConfig();
    return cfg.engine === "auto" && cfg.localReady === "1" && localUsable();
  }
  function pickProvider() {
    const cfg = voiceConfig();
    if (cfg.engine === "local" && localUsable()) return LocalSpeechProvider;
    if (canFallBackToLocal() && (offline || navigator.onLine === false || !WebSpeechProvider.isSupported())) {
      return LocalSpeechProvider;
    }
    return WebSpeechProvider;
  }

  const MAX_QUIET_MS = 5 * 60 * 1000;  // safety net: 5 minutes with nothing heard (mic forgotten on) stops it
  const RESTART_DELAY_MS = 250;  // Chrome needs a moment between two sessions
  const STOP_GRACE_MS = 1500;  // after the stop press, how long to wait for Chrome's last result
  const ERRORS = {
    "not-allowed": "المايك مقفول — اسمح للمتصفح يستخدم المايك.",
    "service-not-allowed": "المايك مقفول — اسمح للمتصفح يستخدم المايك.",
    network: "الإملاء الصوتي محتاج إنترنت.",
    "audio-capture": "مفيش مايك متوصل.",
    "local-offline": "الصوت على الجهاز مش شغال — شغّل run.bat تاني، أو اختار «المتصفح» من إعدادات الروشتة.",
    "local-error": "مقدرتش أفهم التسجيل ده — جرّب تاني.",
  };

  function applyCommands(text) {
    return text
      .replace(/\s*سطر جديد\s*/g, "\n")
      .replace(/\s*(نقطة|نقطه)(?=\s|$)/g, ".")
      .replace(/[ \t]+/g, " ")
      .trim();
  }

  function insertAtCursor(el, text) {
    if (!text) return;
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    const before = el.value.slice(0, start);
    const needsSpace = before && !/\s$/.test(before) && !/^[\n.،,]/.test(text);
    el.setRangeText((needsSpace ? " " : "") + text, start, end, "end");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  let active = null; // the running session (one at a time)

  function langPref() {
    try { return localStorage.getItem("dictation-lang") || "ar-EG"; } catch (e) { return "ar-EG"; }
  }

  // Phosphor "microphone" (MIT), the same icon family as the rest of the system
  const MIC_ICON = '<svg viewBox="0 0 256 256" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M128,176a48.05,48.05,0,0,0,48-48V64a48,48,0,0,0-96,0v64A48.05,48.05,0,0,0,128,176ZM96,64a32,32,0,0,1,64,0v64a32,32,0,0,1-64,0Zm40,143.6V240a8,8,0,0,1-16,0V207.6A80.11,80.11,0,0,1,48,128a8,8,0,0,1,16,0,64,64,0,0,0,128,0,8,8,0,0,1,16,0A80.11,80.11,0,0,1,136,207.6Z"/></svg>';

  function attach(el) {
    if (el.dataset.dictateReady) return;
    el.dataset.dictateReady = "1";
    const bar = document.createElement("span");
    bar.className = "dictation-bar";
    bar.style.cssText = "display:inline-flex;gap:4px;align-items:center;margin-top:4px";
    const mic = document.createElement("button");
    mic.type = "button";
    mic.className = "dictation-mic";
    mic.title = "إملاء صوتي (Ctrl+Space)";
    mic.setAttribute("aria-label", "إملاء صوتي");
    mic.innerHTML = MIC_ICON;
    mic.style.cssText = "display:inline-flex;align-items:center;justify-content:center;width:28px;height:26px;border:1px solid #cfcbd1;border-radius:6px;background:#fff;color:#4a444c";
    const lang = document.createElement("button");
    lang.type = "button";
    lang.className = "dictation-lang";
    lang.style.cssText = "font-size:11px;color:#78716c;padding:2px 4px";
    const renderLang = () => { lang.textContent = langPref() === "ar-EG" ? "ع" : "EN"; };
    renderLang();
    lang.title = "تبديل لغة الإملاء";
    lang.addEventListener("click", () => {
      try { localStorage.setItem("dictation-lang", langPref() === "ar-EG" ? "en-US" : "ar-EG"); } catch (e) { /* ignore */ }
      document.querySelectorAll(".dictation-lang").forEach((b) => { b.textContent = langPref() === "ar-EG" ? "ع" : "EN"; });
    });
    const interim = document.createElement("span");
    interim.className = "dictation-interim";
    interim.style.cssText = "color:#a8a29e;font-size:12px";
    bar.append(mic, lang, interim);
    el.insertAdjacentElement("afterend", bar);
    mic.addEventListener("click", () => toggle(el, mic, interim));
  }

  function toggle(el, mic, interimEl) {
    if (active && active.el === el) { active.stop(); return; }
    if (active) active.stop();
    start(el, mic, interimEl);
  }

  function start(el, mic, interimEl) {
    // Recording continues until the doctor presses the mic again (or another mic): silence never stops it. The browser
    // ends a recognition session by itself after a quiet while, so a new one is started at once — the doctor sees
    // one continuous recording. Real errors (mic blocked, no internet, no mic) do stop it, with the reason shown.
    let provider = null;
    const collected = [];
    let failed = false; // an error message stays visible after the session ends
    let lastSpeech = Date.now();  // the last time anything was heard
    let quickEnds = [];  // times of sessions that ended within a second (Chrome refusing)
    let sessionStart = 0;
    // What Chrome has heard but not finalised yet. Chrome only finalises at a pause: if the doctor talks on and then
    // presses stop, everything since the last pause is still "interim" — it used to be dropped, so only the first
    // drug reached the server. Now it's kept: we wait for Chrome's last result, and fall back to the interim text.
    let pending = "";
    const takeText = (text) => {
      const clean = applyCommands(text);
      if (!clean) return;
      if (el.dataset.dictateUrl) collected.push(clean); // sent to the server when the mic is pressed again
      else insertAtCursor(el, clean);
    };
    const flushPending = () => {
      if (pending.trim()) takeText(pending);
      pending = "";
    };
    let finished = false;
    const done = () => {
      if (finished) return;
      finished = true;
      flushPending();
      finish();
    };
    const session = {
      el,
      stopped: false,
      stop() {
        if (this.stopped) return;
        this.stopped = true;
        mic.style.background = "#f59e0b";  // amber: finishing (Chrome delivers the last words)
        if (provider) provider.stop();
        // Chrome usually ends sooner (onEnd → done); the offline recogniser may still be transcribing the last words
        setTimeout(done, (provider && provider.graceMs) || STOP_GRACE_MS);
      },
    };
    const finish = () => {
      mic.style.background = "#fff";
      mic.style.color = "";
      mic.title = "إملاء صوتي (Ctrl+Space)";
      if (!failed) interimEl.textContent = "";
      else setTimeout(() => { if (!active) interimEl.textContent = ""; }, 8000);
      if (active === session) active = null;
      if (el.dataset.dictateUrl && collected.length && window.htmx) {
        // immediate feedback: the analysis takes a moment, the doctor must see it's working
        const target = document.querySelector(el.dataset.dictateTarget || "#rx-dictation");
        if (target && el.dataset.dictate === "rx") {
          target.innerHTML = '<div class="flex items-center gap-2 rounded-xl border border-brand/20 bg-brand-soft/50 px-4 py-3 text-sm font-semibold">'
            + '<span class="inline-block size-4 animate-spin rounded-full border-2 border-brand border-t-transparent"></span>'
            + "بيطلّع الأدوية من الكلام…</div>";
        }
        window.htmx.ajax("POST", el.dataset.dictateUrl, {
          target: el.dataset.dictateTarget || "#rx-dictation",
          swap: el.dataset.dictateSwap || "innerHTML",
          values: { text: collected.join("\n"), spoken: "1" },
        });
      }
    };
    const listen = () => {
      provider = new (pickProvider())();
      if (el.dataset.dictateUrl && provider.silenceMs) provider.silenceMs = LocalSpeechProvider.COLLECT_SILENCE_MS;
      if (provider.onStatus) {
        // the local recorder needs a moment to open the mic: the button stays amber ("wait") until it really records —
        // words said before that were lost (a real test: "بانادول" became "دول")
        mic.style.background = "#f59e0b";
        mic.title = "ثانية — بيفتح المايك…";
        interimEl.textContent = "ثانية…";
        provider.onStatus((t) => {
          if (t === "🎙" && !session.stopped) {
            mic.style.background = "#dc2626";
            mic.style.color = "#fff";
            mic.title = "بيسجل، دوس تاني عشان توقف";
          }
          if (!failed) interimEl.textContent = t === "🎙" ? "اتكلم دلوقتي" : t;
        });
      }
      provider.onInterim((text) => { interimEl.textContent = text; pending = text; if (text) lastSpeech = Date.now(); });
      provider.onFinal((text) => {
        lastSpeech = Date.now();
        pending = "";
        takeText(text);
      });
      provider.onError((code) => {
        if (code === "no-speech" || code === "aborted") return; // just quiet: the session restarts on "end"
        if (code === "network" && canFallBackToLocal()) {
          // the internet dropped: keep recording on this computer — the browser's session ends, "end" restarts it,
          // and pickProvider() now returns the offline recogniser
          offline = true;
          interimEl.textContent = "مفيش إنترنت — كمّلت بالصوت على الجهاز";
          return;
        }
        failed = true;
        interimEl.textContent = ERRORS[code] || "حصلت مشكلة في الإملاء.";
        session.stop();
        done();
      });
      provider.onEnd(() => {
        if (session.stopped) { done(); return; }
        flushPending(); // a session closed by the browser mid-sentence: keep what was heard
        // the browser closed the session on its own (silence / ~1 min limit): keep recording.
        // Stop only after MAX_QUIET_MS with nothing heard at all — never by counting restarts: Chrome can end a fresh
        // session instantly several times in a row, and a counter used to run out mid-sentence and stop silently.
        if (Date.now() - lastSpeech > MAX_QUIET_MS) { session.stop(); return; }
        const now = Date.now();
        if (now - sessionStart < 1000) quickEnds = [...quickEnds.filter((t) => now - t < 10000), now];
        if (quickEnds.length >= 8) {  // Chrome keeps refusing to listen: say so instead of failing silently
          failed = true;
          interimEl.textContent = "المايك وقف لوحده — دوس عليه تاني وكمل كلامك.";
          session.stop();
          return;
        }
        setTimeout(() => {
          if (session.stopped) return;
          try { listen(); } catch (e) { setTimeout(() => { if (!session.stopped) listen(); }, 400); }
        }, RESTART_DELAY_MS);
      });
      sessionStart = Date.now();
      provider.start(langPref());
    };
    active = session;
    mic.style.background = "#dc2626";
    mic.style.color = "#fff";
    mic.title = "بيسجل — دوس تاني عشان توقف";
    el.focus();
    try {
      listen();
    } catch (e) {
      failed = true;
      interimEl.textContent = "المايك مش راضي يبدأ — جرب تاني.";
      session.stop();
      done();
    }
  }

  function scan(root) {
    if (!pickProvider().isSupported()) {
      (root.querySelectorAll ? root : document).querySelectorAll("[data-dictation-unsupported]").forEach((n) => {
        n.hidden = false;
      });
      return;
    }
    (root.querySelectorAll ? root : document).querySelectorAll("[data-dictate]").forEach(attach);
  }

  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey && e.code === "Space")) return;
    const el = document.activeElement;
    if (!el || !el.dataset || el.dataset.dictateReady !== "1") return;
    e.preventDefault();
    const bar = el.nextElementSibling;
    if (bar && bar.classList.contains("dictation-bar")) bar.querySelector(".dictation-mic").click();
  });

  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.addEventListener("htmx:afterSettle", (e) => scan(e.target));

  window.DigiDictation = {
    providers: { webspeech: WebSpeechProvider, local: LocalSpeechProvider }, pickProvider, applyCommands, insertAtCursor,
    scan, attach,
  };
  if (document.readyState !== "loading") scan(document);
})();
