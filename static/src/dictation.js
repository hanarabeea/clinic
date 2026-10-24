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
 * Providers implement: isSupported(), start(lang), stop(), onInterim(cb), onFinal(cb), onError(cb), onEnd(cb).
 * v1 = WebSpeechProvider (Chrome/Edge). A WhisperProvider (MediaRecorder → /api/transcribe/) can be added later
 * without touching the UI code.
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

  const Provider = WebSpeechProvider;
  const MAX_QUIET_MS = 5 * 60 * 1000;  // safety net: 5 minutes with nothing heard (mic forgotten on) stops it
  const RESTART_DELAY_MS = 250;  // Chrome needs a moment between two sessions
  const STOP_GRACE_MS = 1500;  // after the stop press, how long to wait for Chrome's last result
  const ERRORS = {
    "not-allowed": "المايك مقفول — اسمح للمتصفح يستخدم المايك.",
    "service-not-allowed": "المايك مقفول — اسمح للمتصفح يستخدم المايك.",
    network: "الإملاء الصوتي محتاج إنترنت.",
    "audio-capture": "مفيش مايك متوصل.",
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
    mic.textContent = "🎤";
    mic.style.cssText = "border:1px solid #d6d3d1;border-radius:9999px;padding:2px 8px;background:#fff;font-size:13px";
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
        setTimeout(done, STOP_GRACE_MS);  // Chrome usually ends sooner (onEnd → done)
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
      provider = new Provider();
      provider.onInterim((text) => { interimEl.textContent = text; pending = text; if (text) lastSpeech = Date.now(); });
      provider.onFinal((text) => {
        lastSpeech = Date.now();
        pending = "";
        takeText(text);
      });
      provider.onError((code) => {
        if (code === "no-speech" || code === "aborted") return; // just quiet: the session restarts on "end"
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
    if (!Provider.isSupported()) {
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

  window.DigiDictation = { providers: { webspeech: WebSpeechProvider }, applyCommands, insertAtCursor, scan, attach };
  if (document.readyState !== "loading") scan(document);
})();
