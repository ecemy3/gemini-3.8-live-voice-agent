/*
 * ecem.dev sesli asistan widget'ı — konuşan küre.
 *
 * Kullanım (herhangi bir sayfada, </body> öncesi):
 *   <script src="https://SUNUCU/static/widget/ecem-widget.js" defer></script>
 * İsteğe bağlı data-* öznitelikleri:
 *   data-server="https://SUNUCU"  (varsayılan: script'in geldiği origin)
 *   data-lang="tr|en"             (varsayılan: tarayıcı dili)
 *   data-voice="Aoede"            data-scenario="ecem"
 *   data-bottom="20" data-right="20"  (px; sayfadaki başka sabit butonlarla çakışmasın diye)
 *
 * Framework yok; stiller Shadow DOM içinde, host sayfayla çakışmaz.
 */
(() => {
  "use strict";
  if (window.__ecemVoiceWidget) return;
  window.__ecemVoiceWidget = true;

  const script = document.currentScript;
  const SERVER = (script?.dataset.server || new URL(script?.src || location.href).origin).replace(/\/$/, "");
  const SCENARIO = script?.dataset.scenario || "ecem";
  const VOICE = script?.dataset.voice || "Aoede";
  const BOTTOM = parseInt(script?.dataset.bottom || "20", 10);
  const RIGHT = parseInt(script?.dataset.right || "20", 10);
  const INPUT_RATE = 16000;
  const OUTPUT_RATE = 24000;
  const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const I18N = {
    tr: {
      title: "Ecem'in asistanı",
      hint: "Bana Ecem'i sor 👋",
      idle: "Konuşmak için küreye dokunun",
      connecting: "Bağlanıyor…",
      mic: "Mikrofon izni bekleniyor…",
      listening: "Dinliyorum…",
      speaking: "Konuşuyor…",
      ended: "Görüşme bitti. Tekrar başlatmak için küreye dokunun.",
      timeUp: "Süre doldu. Tekrar başlatmak için küreye dokunun.",
      limitSoon: (s) => `Görüşmenin bitmesine ${s} sn kaldı`,
      micDenied: "Mikrofon izni reddedildi. Tarayıcı ayarlarından izin verin.",
      micMissing: "Mikrofon bulunamadı.",
      netError: "Sunucuya bağlanılamadı. Biraz sonra tekrar deneyin.",
      stop: "Bitir",
      greet: "[Ziyaretçi bağlandı] Kendini tek cümleyle tanıt ve Ecem hakkında neler sorulabileceğini kısaca söyle.",
      privacy: "Sesiniz yalnızca bu görüşme için Google Gemini'ye iletilir, kaydedilmez.",
      orbLabel: "Sesli asistanı başlat",
    },
    en: {
      title: "Ecem's assistant",
      hint: "Ask me about Ecem 👋",
      idle: "Tap the orb to talk",
      connecting: "Connecting…",
      mic: "Waiting for microphone permission…",
      listening: "Listening…",
      speaking: "Speaking…",
      ended: "Conversation ended. Tap the orb to start again.",
      timeUp: "Time is up. Tap the orb to start again.",
      limitSoon: (s) => `${s}s left in this conversation`,
      micDenied: "Microphone permission denied. Allow it in your browser settings.",
      micMissing: "No microphone found.",
      netError: "Couldn't reach the server. Please try again later.",
      stop: "End",
      greet: "[Visitor connected] Introduce yourself in one sentence and briefly say what they can ask about Ecem.",
      privacy: "Your voice is sent to Google Gemini only for this conversation and is not stored.",
      orbLabel: "Start voice assistant",
    },
  };

  const savedLang = (() => { try { return localStorage.getItem("ecemWidgetLang"); } catch (_) { return null; } })();
  let lang = script?.dataset.lang || savedLang || ((navigator.language || "en").toLowerCase().startsWith("tr") ? "tr" : "en");
  const t = (k, ...a) => (typeof I18N[lang][k] === "function" ? I18N[lang][k](...a) : I18N[lang][k]);

  // ---------------------------------------------------------------------------
  // DOM (Shadow)
  // ---------------------------------------------------------------------------
  const host = document.createElement("div");
  host.id = "ecem-voice-widget";
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
<style>
  :host { all: initial; }
  * { box-sizing: border-box; font-family: "Geist", system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { position: fixed; right: ${RIGHT}px; bottom: ${BOTTOM}px; z-index: 2147483000; display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
  .panel {
    width: min(340px, calc(100vw - 32px)); padding: 14px 16px 12px; border-radius: 16px;
    background: rgba(20,20,20,.86); color: #f4f4f4; border: 1px solid #2a2a2a;
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    box-shadow: 0 10px 40px rgba(0,0,0,.35); font-size: 13px; line-height: 1.5;
    transform-origin: bottom right; transition: opacity .18s ease, transform .18s ease;
  }
  .panel[hidden] { display: block; opacity: 0; transform: scale(.96) translateY(6px); pointer-events: none; }
  .head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .title { font-weight: 600; flex: 1; font-size: 14px; }
  .langs { display: inline-flex; border: 1px solid #333; border-radius: 999px; overflow: hidden; }
  .langs button { all: unset; cursor: pointer; padding: 2px 10px; font-size: 12px; color: #a3a3a3; }
  .langs button[aria-pressed="true"] { background: #7c5cff; color: #fff; }
  .x { all: unset; cursor: pointer; color: #8a8a8a; font-size: 18px; line-height: 1; padding: 0 2px; }
  .status { color: #a3a3a3; font-size: 12px; min-height: 18px; }
  .status.err { color: #ff8a80; }
  .captions { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; max-height: 180px; overflow-y: auto; }
  .cap { padding: 6px 10px; border-radius: 10px; background: #1d1d1d; }
  .cap.user { align-self: flex-end; background: #2b2446; }
  .cap.model { align-self: flex-start; }
  .foot { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; gap: 8px; }
  .privacy { color: #6f6f6f; font-size: 11px; }
  .stop { all: unset; cursor: pointer; font-size: 12px; padding: 4px 12px; border-radius: 999px; border: 1px solid #ff8a80; color: #ff8a80; }
  .stop[hidden] { display: none; }
  .hint {
    padding: 8px 12px; border-radius: 12px; background: #7c5cff; color: #fff; font-size: 13px; font-weight: 500;
    box-shadow: 0 6px 20px rgba(124,92,255,.35); cursor: pointer; animation: pop .35s ease;
  }
  .hint[hidden] { display: none; }
  @keyframes pop { from { opacity: 0; transform: translateY(6px); } }
  .orb { all: unset; position: relative; width: 132px; height: 132px; margin: -34px -34px -34px 0; cursor: pointer; border-radius: 50%; -webkit-tap-highlight-color: transparent; }
  .orb:focus-visible { outline: 2px solid #a996ff; outline-offset: -30px; }
  canvas { width: 132px; height: 132px; display: block; pointer-events: none; }
  @media (max-width: 480px) { .wrap { right: ${Math.min(RIGHT, 12)}px; } }
</style>
<div class="wrap">
  <div class="panel" hidden role="dialog" aria-live="polite">
    <div class="head">
      <span class="title"></span>
      <span class="langs" role="group" aria-label="Language">
        <button type="button" data-lang="tr">TR</button><button type="button" data-lang="en">EN</button>
      </span>
      <button type="button" class="x" aria-label="Close">×</button>
    </div>
    <div class="status"></div>
    <div class="captions"></div>
    <div class="foot"><span class="privacy"></span><button type="button" class="stop" hidden></button></div>
  </div>
  <div class="hint" hidden></div>
  <button type="button" class="orb"><canvas width="264" height="264"></canvas></button>
</div>`;
  const $ = (s) => root.querySelector(s);
  const ui = {
    panel: $(".panel"), title: $(".title"), status: $(".status"), captions: $(".captions"),
    privacy: $(".privacy"), stop: $(".stop"), hint: $(".hint"), orb: $(".orb"), canvas: $("canvas"),
    close: $(".x"), langBtns: [...root.querySelectorAll(".langs button")],
  };

  function applyLang() {
    ui.title.textContent = t("title");
    ui.hint.textContent = t("hint");
    ui.privacy.textContent = t("privacy");
    ui.stop.textContent = t("stop");
    ui.orb.setAttribute("aria-label", t("orbLabel"));
    ui.langBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
    if (state.phase === "idle" || state.phase === "error" || state.phase === "ended") setStatus(t("idle"));
  }

  // ---------------------------------------------------------------------------
  // Durum
  // ---------------------------------------------------------------------------
  const state = {
    phase: "idle", // idle | connecting | listening | speaking | error
    ws: null, capCtx: null, playCtx: null, analyser: null, micStream: null, worklet: null,
    nextStart: 0, sources: new Set(), micLevel: 0, outLevel: 0, userClosing: false,
    bubble: { user: null, model: null },
  };

  function setStatus(text, err = false) {
    ui.status.textContent = text;
    ui.status.classList.toggle("err", err);
  }
  function setPhase(p) {
    state.phase = p;
    ui.stop.hidden = !(p === "listening" || p === "speaking" || p === "connecting");
    if (p === "listening") setStatus(t("listening"));
    if (p === "speaking") setStatus(t("speaking"));
  }

  function caption(role, text, partial) {
    let b = state.bubble[role];
    if (!b) {
      b = document.createElement("div");
      b.className = `cap ${role}`;
      ui.captions.appendChild(b);
      state.bubble[role] = b;
      while (ui.captions.childElementCount > 6) ui.captions.firstChild.remove();
    }
    if (partial) b.textContent += text;
    else { b.textContent = text; state.bubble[role] = null; }
    ui.captions.scrollTop = ui.captions.scrollHeight;
  }

  // ---------------------------------------------------------------------------
  // Ses
  // ---------------------------------------------------------------------------
  const WORKLET = `
class P extends AudioWorkletProcessor {
  constructor(o) { super(); const p = o.processorOptions; this.ratio = sampleRate / p.rate; this.n = Math.round(p.rate / 10);
    this.buf = new Int16Array(this.n); this.len = 0; this.pos = 0; this.sq = 0; }
  push(s) { const c = Math.max(-1, Math.min(1, s)); this.sq += c * c; this.buf[this.len++] = c < 0 ? c * 0x8000 : c * 0x7fff;
    if (this.len === this.n) { const out = this.buf.buffer.slice(0); this.port.postMessage({ pcm: out, rms: Math.sqrt(this.sq / this.n) }, [out]); this.len = 0; this.sq = 0; } }
  process(inp) { const ch = inp[0] && inp[0][0]; if (!ch) return true;
    if (this.ratio === 1) { for (let i = 0; i < ch.length; i++) this.push(ch[i]); }
    else { while (this.pos < ch.length - 1) { const i = Math.floor(this.pos), f = this.pos - i; this.push(ch[i] * (1 - f) + ch[i + 1] * f); this.pos += this.ratio; } this.pos -= ch.length; if (this.pos < 0) this.pos = 0; }
    return true; } }
registerProcessor("ecem-pcm", P);`;

  function b64(buf) {
    const u = new Uint8Array(buf); let s = "";
    for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  }
  function unb64(s) {
    const bin = atob(s); const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u.buffer;
  }

  async function startMic() {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    state.micStream = stream;
    let ctx = new AudioContext({ sampleRate: INPUT_RATE });
    let src;
    try { src = ctx.createMediaStreamSource(stream); }
    catch (_) { await ctx.close(); ctx = new AudioContext(); src = ctx.createMediaStreamSource(stream); }
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
    try { await ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
    const node = new AudioWorkletNode(ctx, "ecem-pcm", { processorOptions: { rate: INPUT_RATE } });
    src.connect(node);
    const sink = ctx.createGain(); sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);
    node.port.onmessage = ({ data }) => {
      state.micLevel = Math.min(1, data.rms * 6);
      if (state.ws?.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: "audio", data: b64(data.pcm) }));
    };
    state.capCtx = ctx; state.worklet = node;
  }

  function play(buf, rate) {
    const ctx = state.playCtx; if (!ctx) return;
    const pcm = new Int16Array(buf); if (!pcm.length) return;
    const f = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 0x8000;
    const ab = ctx.createBuffer(1, f.length, rate); ab.copyToChannel(f, 0);
    const s = ctx.createBufferSource(); s.buffer = ab; s.connect(state.analyser);
    const now = ctx.currentTime;
    if (state.nextStart < now + 0.01) state.nextStart = now + 0.03;
    s.start(state.nextStart); state.nextStart += ab.duration;
    state.sources.add(s);
    s.onended = () => { state.sources.delete(s); if (!state.sources.size && state.phase === "speaking") setPhase("listening"); };
    if (state.phase !== "speaking") setPhase("speaking");
  }

  function stopPlayback() {
    for (const s of state.sources) { s.onended = null; try { s.stop(); } catch (_) {} }
    state.sources.clear(); state.nextStart = 0;
  }

  // ---------------------------------------------------------------------------
  // Sayfada gezinme (show_on_page aracı)
  // ---------------------------------------------------------------------------
  const CASES = { case_in_vehicle_ser: "case-ser", case_call_center: "case-cc", case_thesis: "case-mer" };
  function showOnPage(target) {
    if (CASES[target]) {
      const trigger = document.querySelector(`[data-case="${CASES[target]}"]`);
      if (trigger) { trigger.click(); return; }
      target = "projects";
    }
    const el = document.getElementById(target === "about" ? "about" : target) || document.getElementById("intro");
    el?.scrollIntoView({ behavior: REDUCED_MOTION ? "auto" : "smooth", block: "start" });
  }

  // ---------------------------------------------------------------------------
  // Oturum
  // ---------------------------------------------------------------------------
  async function start() {
    if (state.ws || state.phase === "connecting") return;
    ui.hint.hidden = true;
    ui.panel.hidden = false;
    ui.captions.textContent = "";
    state.bubble = { user: null, model: null };
    state.userClosing = false;
    setPhase("connecting");
    setStatus(t("mic"));

    state.playCtx = new AudioContext({ sampleRate: OUTPUT_RATE });
    state.analyser = state.playCtx.createAnalyser();
    state.analyser.fftSize = 512;
    state.analyser.connect(state.playCtx.destination);

    try {
      await startMic();
    } catch (err) {
      teardown();
      setPhase("error");
      setStatus(err.name === "NotFoundError" ? t("micMissing") : t("micDenied"), true);
      return;
    }
    setStatus(t("connecting"));
    const q = new URLSearchParams({ scenario: SCENARIO, voice: VOICE, lang });
    const ws = new WebSocket(`${SERVER.replace(/^http/, "ws")}/ws/live?${q}`);
    state.ws = ws;
    let opened = false;
    ws.onopen = () => { opened = true; };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onerror = () => { if (!opened) setStatus(t("netError"), true); };
    ws.onclose = () => {
      const unexpected = !state.userClosing && state.phase !== "ended";
      state.ws = null;
      teardown();
      if (unexpected && state.phase !== "error") { setPhase("error"); if (!opened) setStatus(t("netError"), true); else setStatus(t("ended")); }
    };
  }

  function onMessage(m) {
    switch (m.type) {
      case "session":
        setPhase("listening");
        state.ws.send(JSON.stringify({ type: "text", text: t("greet") }));
        break;
      case "audio": {
        const r = /rate=(\d+)/.exec(m.mime || ""); play(unb64(m.data), r ? +r[1] : OUTPUT_RATE);
        break;
      }
      case "interrupted":
        stopPlayback(); state.bubble.model = null; setPhase("listening");
        break;
      case "transcript":
        caption(m.role === "user" ? "user" : "model", m.text, m.partial);
        break;
      case "turn_complete":
        state.bubble.model = null;
        break;
      case "tool_result":
        if (m.name === "show_on_page" && m.data?.status === "ok") showOnPage(m.data.target);
        break;
      case "session_limit":
        setStatus(t("limitSoon", m.remaining));
        break;
      case "session_ended":
        state.phase = "ended"; stop(); setStatus(t("timeUp"));
        break;
      case "error":
        setPhase("error"); setStatus(m.message, true);
        break;
      default:
        break;
    }
  }

  function stop() {
    state.userClosing = true;
    if (state.ws && state.ws.readyState <= WebSocket.OPEN) state.ws.close(1000, "user");
    teardown();
    if (state.phase !== "ended") setStatus(t("ended"));
    state.phase = state.phase === "ended" ? "ended" : "idle";
    ui.stop.hidden = true;
  }

  function teardown() {
    try { state.worklet && (state.worklet.port.onmessage = null); } catch (_) {}
    state.micStream?.getTracks().forEach((tr) => tr.stop());
    state.capCtx?.close().catch(() => {});
    stopPlayback();
    state.playCtx?.close().catch(() => {});
    Object.assign(state, { micStream: null, capCtx: null, worklet: null, playCtx: null, analyser: null, micLevel: 0 });
  }

  // ---------------------------------------------------------------------------
  // Küre animasyonu
  // ---------------------------------------------------------------------------
  const ctx2d = ui.canvas.getContext("2d");
  const W = 264, C = W / 2, BASE_R = 54; // canvas 2x (retina)
  const rings = [];
  let lastRing = 0, smooth = 0, tick = 0;
  const td = new Float32Array(512);

  function outputLevel() {
    if (!state.analyser || !state.sources.size) return 0;
    state.analyser.getFloatTimeDomainData(td);
    let sq = 0; for (let i = 0; i < td.length; i++) sq += td[i] * td[i];
    return Math.min(1, Math.sqrt(sq / td.length) * 5);
  }

  function frame(now) {
    tick++;
    const speaking = state.phase === "speaking";
    const listening = state.phase === "listening";
    const target = speaking ? outputLevel() : listening ? state.micLevel * 0.7 : 0;
    smooth += (target - smooth) * (target > smooth ? 0.35 : 0.12);

    const breathe = REDUCED_MOTION ? 0 : Math.sin(now / 900) * 2;
    const r = BASE_R * (1 + smooth * 0.22) + breathe;
    const hueA = speaking ? "124,92,255" : listening ? "56,210,200" : "124,92,255";

    if (!REDUCED_MOTION && (speaking || listening) && smooth > 0.06 && now - lastRing > (speaking ? 140 : 220)) {
      rings.push({ r, a: 0.55 * Math.min(1, smooth * 2 + 0.2), c: hueA });
      lastRing = now;
    }

    ctx2d.clearRect(0, 0, W, W);
    // dalgalar
    for (let i = rings.length - 1; i >= 0; i--) {
      const g = rings[i];
      g.r += 1.1; g.a *= 0.955;
      if (g.a < 0.02 || g.r > C - 2) { rings.splice(i, 1); continue; }
      ctx2d.beginPath(); ctx2d.arc(C, C, g.r, 0, Math.PI * 2);
      ctx2d.strokeStyle = `rgba(${g.c},${g.a})`; ctx2d.lineWidth = 3; ctx2d.stroke();
    }
    // hale
    const halo = ctx2d.createRadialGradient(C, C, r * 0.6, C, C, r * 1.7);
    halo.addColorStop(0, `rgba(${hueA},${0.28 + smooth * 0.3})`); halo.addColorStop(1, `rgba(${hueA},0)`);
    ctx2d.fillStyle = halo; ctx2d.beginPath(); ctx2d.arc(C, C, r * 1.7, 0, Math.PI * 2); ctx2d.fill();
    // küre
    const g = ctx2d.createRadialGradient(C - r * 0.35, C - r * 0.4, r * 0.1, C, C, r);
    if (listening) { g.addColorStop(0, "#c9fff6"); g.addColorStop(0.45, "#38d2c8"); g.addColorStop(1, "#1b6f8f"); }
    else { g.addColorStop(0, "#e4dcff"); g.addColorStop(0.45, "#7c5cff"); g.addColorStop(1, "#3b22b8"); }
    ctx2d.fillStyle = g; ctx2d.beginPath(); ctx2d.arc(C, C, r, 0, Math.PI * 2); ctx2d.fill();
    // bağlanırken dönen yay
    if (state.phase === "connecting") {
      const a = (now / 300) % (Math.PI * 2);
      ctx2d.beginPath(); ctx2d.arc(C, C, r + 8, a, a + Math.PI * 0.6);
      ctx2d.strokeStyle = "rgba(169,150,255,.9)"; ctx2d.lineWidth = 4; ctx2d.lineCap = "round"; ctx2d.stroke();
    }
    // parlama
    ctx2d.beginPath(); ctx2d.ellipse(C - r * 0.32, C - r * 0.42, r * 0.28, r * 0.16, -0.5, 0, Math.PI * 2);
    ctx2d.fillStyle = "rgba(255,255,255,.35)"; ctx2d.fill();

    requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------------------
  // Olaylar
  // ---------------------------------------------------------------------------
  ui.orb.addEventListener("click", () => {
    if (state.ws) { ui.panel.hidden = !ui.panel.hidden; return; }
    start();
  });
  ui.hint.addEventListener("click", () => start());
  ui.stop.addEventListener("click", () => stop());
  ui.close.addEventListener("click", () => { ui.panel.hidden = true; });
  ui.langBtns.forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.lang === lang) return;
    lang = b.dataset.lang;
    try { localStorage.setItem("ecemWidgetLang", lang); } catch (_) {}
    applyLang();
    if (state.ws) { stop(); setTimeout(start, 300); } // dil değişince yeni oturum
  }));
  window.addEventListener("pagehide", () => { if (state.ws) stop(); });

  function mount() {
    document.body.appendChild(host);
    applyLang();
    setTimeout(() => { if (state.phase === "idle") ui.hint.hidden = false; }, 1800);
    requestAnimationFrame(frame);
  }
  if (document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
})();
