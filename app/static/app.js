"use strict";

/*
 * Canlı sesli asistan istemcisi (framework'süz).
 *
 * Akış:
 *   Mikrofon -> 16 kHz AudioContext -> AudioWorklet (Float32 -> Int16, 100 ms)
 *            -> base64 JSON {"type":"audio"} -> WebSocket /ws/live
 *   WebSocket {"type":"audio"} (24 kHz PCM16) -> 24 kHz AudioContext -> sıralı oynatma
 */

const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;
const CHUNK_MS = 100;
const VOICE_RMS_THRESHOLD = 0.02; // gecikme ölçümü için kaba konuşma algılama
const LOG_MAX_LINES = 400;
const FRAME_INTERVAL_MS = 1000; // saniyede 1 kare
const JPEG_QUALITY = 0.6;
const MAX_FRAME_EDGE = 1024;    // uzun kenar en fazla 1024 px

// ---------------------------------------------------------------------------
// AudioWorklet: ayrı dosya yerine Blob URL ile yüklenir.
// Bağlam 16 kHz değilse (ör. Firefox'ta farklı örnekleme hızı hatası sonrası
// geri dönüş) doğrusal yeniden örnekleme yapar.
// ---------------------------------------------------------------------------
const CAPTURE_WORKLET = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = options.processorOptions || {};
    this.targetRate = o.targetRate || 16000;
    this.chunkSamples = Math.round(this.targetRate * (o.chunkMs || 100) / 1000);
    this.ratio = sampleRate / this.targetRate;
    this.buf = new Int16Array(this.chunkSamples);
    this.len = 0;
    this.pos = 0;          // yeniden örnekleme için kesirli okuma konumu
    this.sumSq = 0;
    this.muted = false;
    this.port.onmessage = (e) => { if (e.data && 'muted' in e.data) this.muted = e.data.muted; };
  }
  push(s) {
    const c = Math.max(-1, Math.min(1, s));
    this.sumSq += c * c;
    this.buf[this.len++] = c < 0 ? c * 0x8000 : c * 0x7fff;
    if (this.len === this.chunkSamples) {
      const rms = Math.sqrt(this.sumSq / this.len);
      const out = this.buf.buffer.slice(0);
      this.port.postMessage({ pcm: out, rms }, [out]);
      this.len = 0;
      this.sumSq = 0;
    }
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (this.muted) { for (let i = 0; i < ch.length; i++) ch[i] = 0; }
    if (this.ratio === 1) {
      for (let i = 0; i < ch.length; i++) this.push(ch[i]);
    } else {
      while (this.pos < ch.length - 1) {
        const i = Math.floor(this.pos);
        const f = this.pos - i;
        this.push(ch[i] * (1 - f) + ch[i + 1] * f);
        this.pos += this.ratio;
      }
      this.pos -= ch.length;
      if (this.pos < 0) this.pos = 0;
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCaptureProcessor);
`;

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const el = {
  scenario: $("scenario"),
  scenarioDesc: $("scenario-desc"),
  scenarioTools: $("scenario-tools"),
  scenarioExamples: $("scenario-examples"),
  voice: $("voice"),
  start: $("btn-start"),
  stop: $("btn-stop"),
  mute: $("btn-mute"),
  statusDot: $("status-dot"),
  statusText: $("status-text"),
  micLevel: $("mic-level"),
  messages: $("messages"),
  cards: $("cards"),
  log: $("log"),
  banner: $("banner"),
  bannerText: $("banner-text"),
  textForm: $("text-form"),
  textInput: $("text-input"),
  send: $("btn-send"),
  statModel: $("stat-model"),
  statLatency: $("stat-latency"),
  statUp: $("stat-up"),
  statDown: $("stat-down"),
  statFrames: $("stat-frames"),
  statSentiment: $("stat-sentiment"),
  summary: $("btn-summary"),
  videoBlock: $("video-block"),
  camera: $("btn-camera"),
  screen: $("btn-screen"),
  preview: $("preview"),
  previewWrap: $("preview-wrap"),
  previewBadge: $("preview-badge"),
};

// ---------------------------------------------------------------------------
// Durum
// ---------------------------------------------------------------------------
const state = {
  ws: null,
  captureCtx: null,
  playCtx: null,
  micStream: null,
  micSource: null,
  worklet: null,
  muted: false,
  userClosing: false,
  nextStartTime: 0,
  activeSources: new Set(),
  bubbles: { user: null, model: null },
  interruptedBubble: null, // kesilen model balonu; ADK tam metni interrupted'dan önce VEYA sonra gönderebilir
  lastModelBubble: null,   // o turun (kapanmış olsa bile) son model balonu
  bytesUp: 0,
  bytesDown: 0,
  lastVoiceTs: 0,
  voiceOnsetTs: 0,        // sessizlikten sonra konuşmanın başladığı an (kesme gecikmesi için)
  wasVoiced: false,
  awaitingFirstAudio: true,
  scenarios: [],
  video: null, // { kind: "camera"|"screen", stream, timer, busy }
  frameCanvas: document.createElement("canvas"),
  framesSent: 0,
  frameBytes: 0,
  mode: "voice",                           // "voice" | "assist"
  assist: { transcript: [], insights: [] }, // CRM özeti için
};

// ---------------------------------------------------------------------------
// UI yardımcıları
// ---------------------------------------------------------------------------
function setStatus(kind, text) {
  el.statusDot.className = `dot ${kind}`;
  el.statusText.textContent = text;
}

function showBanner(text, level = "error") {
  el.bannerText.textContent = text;
  el.banner.className = `banner ${level === "warn" ? "warn" : ""}`;
}
function hideBanner() { el.banner.className = "banner hidden"; }
el.banner.querySelector("button").onclick = hideBanner;

function log(kind, text) {
  const line = document.createElement("div");
  line.className = "line";
  const t = new Date();
  const ts = t.toLocaleTimeString("tr-TR", { hour12: false }) + "." + String(t.getMilliseconds()).padStart(3, "0");
  line.innerHTML = `<span class="t">${ts}</span> <span class="${kind}"></span>`;
  line.lastChild.textContent = text;
  el.log.appendChild(line);
  while (el.log.childElementCount > LOG_MAX_LINES) el.log.firstChild.remove();
  el.log.scrollTop = el.log.scrollHeight;
  console.debug(`[${kind}] ${text}`);
}

function clearEmpty(container) {
  const e = container.querySelector(".empty");
  if (e) e.remove();
}

function addSystemMessage(text) {
  clearEmpty(el.messages);
  const div = document.createElement("div");
  div.className = "msg system";
  div.textContent = text;
  el.messages.appendChild(div);
  el.messages.scrollTop = el.messages.scrollHeight;
}

/*
 * Transkript: partial=true mesajları mevcut balona eklenen parçalardır;
 * partial=false mesajı o turun tam metnidir (balonu değiştirir ve kapatır).
 */
function onTranscript({ role, text, partial }) {
  clearEmpty(el.messages);
  if (role === "model") {
    const ib = state.interruptedBubble;
    if (ib && !partial && !state.bubbles.model) {
      ib.querySelector(".body").textContent = text; // kesilen turun tam metni
      state.interruptedBubble = null;
      return;
    }
    if (partial) state.interruptedBubble = null; // yeni model turu başladı
  }
  if (role === "user") finalizeBubble("model"); // yeni kullanıcı turu modelinkini kapatır
  let b = state.bubbles[role];
  if (!b) {
    b = document.createElement("div");
    b.className = `msg ${role} partial`;
    b.innerHTML = `<span class="role">${ROLE_LABELS[role] || role}</span><span class="body"></span>`;
    el.messages.appendChild(b);
    state.bubbles[role] = b;
    if (role === "model") state.lastModelBubble = b;
  }
  const body = b.querySelector(".body");
  if (partial) {
    body.textContent += text;
  } else {
    body.textContent = text;
    finalizeBubble(role);
  }
  el.messages.scrollTop = el.messages.scrollHeight;
}

const ROLE_LABELS = { user: "Siz", model: "Asistan", customer: "Müşteri" };

function finalizeBubble(role) {
  const b = state.bubbles[role];
  if (b) b.classList.remove("partial");
  state.bubbles[role] = null;
}

function updateStats() {
  const avg = state.framesSent ? ` (ort. ${(state.frameBytes / state.framesSent / 1024).toFixed(0)} KB)` : "";
  el.statFrames.textContent = `${state.framesSent}${avg}`;
  el.statUp.textContent = `${(state.bytesUp / 1024).toFixed(0)} KB`;
  el.statDown.textContent = `${(state.bytesDown / 1024).toFixed(0)} KB`;
}

function setControls(running) {
  el.start.disabled = running;
  el.stop.disabled = !running;
  el.mute.disabled = !running;
  // Senaryo/ses oturum sırasında da değiştirilebilir; değişiklik yeni oturum başlatır.
  el.textInput.disabled = !running;
  el.camera.disabled = !running || state.mode === "assist";
  el.screen.disabled = !running || state.mode === "assist";
  el.summary.disabled = state.assist.transcript.length === 0;
  el.send.disabled = !running;
}

// ---------------------------------------------------------------------------
// Aksiyon kartları (tool_call / tool_result)
// ---------------------------------------------------------------------------
const TOOL_LABELS = {
  get_order_status: "Sipariş durumu",
  approve_refund: "İade onayı",
  freeze_card: "Kart dondurma",
  check_table_availability: "Masa uygunluğu",
  make_reservation: "Rezervasyon",
  check_service_status: "Hizmet durumu",
  create_support_ticket: "Destek kaydı",
  analyze_with_flash: "Araştırma · Flash + Google",
};
const TOOL_ICONS = {
  get_order_status: "📦", approve_refund: "↩️", freeze_card: "🧊",
  check_table_availability: "🗓️", make_reservation: "🍽️",
  check_service_status: "🛰️", create_support_ticket: "🎫",
  analyze_with_flash: "🔎",
};
const FIELD_LABELS = {
  order_id: "Sipariş", musteri: "Müşteri", urun: "Ürün", tutar_tl: "Tutar (TL)", durum: "Durum",
  kargo_firmasi: "Kargo", tahmini_teslim: "Tahmini teslim", teslim_tarihi: "Teslim tarihi",
  siparis_tarihi: "Sipariş tarihi", iade_durumu: "İade", refund_id: "İade no", amount_tl: "Tutar (TL)",
  reason: "Neden", reason_code: "Kod", days_since_delivery: "Teslimattan beri (gün)",
  card_last4: "Kart (son 4)", card_type: "Kart tipi", reference: "Referans",
  date: "Tarih", party_size: "Kişi", available_times: "Boş saatler", reservation_id: "Rezervasyon no",
  guest_name: "Ad soyad", time: "Saat", reservation_date: "Tarih", service: "Hizmet",
  service_status: "Hizmet durumu", note: "Not", ticket_id: "Kayıt no", priority: "Öncelik",
  summary: "Özet", expected_response: "Yanıt süresi", valid_options: "Geçerli seçenekler",
  valid_times: "Geçerli saatler",
  topic: "Soru", context: "Bağlam",
};
const HIDDEN_FIELDS = new Set(["status", "retryable", "message", "suggested_next_step", "valid_options_hint"]);
const OK_STATUSES = new Set(["ok", "approved", "frozen", "confirmed", "created"]);
const STATUS_TEXT = {
  ok: "tamam", approved: "onaylandı", frozen: "donduruldu", confirmed: "onaylandı", created: "oluşturuldu",
  rejected: "reddedildi", not_found: "bulunamadı", invalid_argument: "geçersiz", no_availability: "yer yok",
  slot_taken: "dolu", already_refunded: "zaten iade", already_frozen: "zaten dondurulmuş", error: "hata",
  timeout: "zaman aşımı", no_results: "sonuç yok",
};

const toolLabel = (name) => TOOL_LABELS[name] || name;
const fieldLabel = (k) => FIELD_LABELS[k] || k;

function fmtValue(v) {
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "number") return v.toLocaleString("tr-TR");
  if (v && typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function kvList(obj, hidden = HIDDEN_FIELDS) {
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [k, v] of Object.entries(obj || {})) {
    if (hidden.has(k) || v === null || v === undefined || v === "") continue;
    const dt = document.createElement("dt");
    dt.textContent = fieldLabel(k);
    const dd = document.createElement("dd");
    dd.textContent = fmtValue(v);
    dl.append(dt, dd);
  }
  return dl;
}

function findCard(id) {
  return id ? el.cards.querySelector(`.card[data-id="${CSS.escape(id)}"]`) : null;
}

function createToolCard(id, name, args) {
  clearEmpty(el.cards);
  const card = document.createElement("article");
  card.className = "card tool pending";
  if (id) card.dataset.id = id;
  card.innerHTML = `
    <header class="card-head">
      <span class="card-icon"></span>
      <span class="card-title"></span>
      <span class="badge">çalışıyor…</span>
    </header>
    <div class="card-args"></div>
    <div class="card-result"></div>
    <footer class="card-foot"></footer>`;
  card.querySelector(".card-icon").textContent = TOOL_ICONS[name] || "⚙️";
  card.querySelector(".card-title").textContent = toolLabel(name);
  if (name === "analyze_with_flash") card.querySelector(".badge").textContent = "araştırıyor…";
  card.querySelector(".card-args").appendChild(kvList(args, new Set()));
  card._args = args || {};
  card.querySelector(".card-foot").textContent =
    `${name} · ${new Date().toLocaleTimeString("tr-TR", { hour12: false })}`;
  el.cards.prepend(card); // en yeni kart üstte
  return card;
}

function onToolCall({ id, name, args }) {
  createToolCard(id, name, args);
}

function escapeHtml(t) {
  return t.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Flash cevabı için küçük, güvenli markdown: kalın, madde işaretleri, başlıklar.
function renderMiniMarkdown(text) {
  const out = [];
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const inline = (t) => escapeHtml(t).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    const m = /^([*-]|\d+\.)\s+(.*)$/.exec(line);
    if (m) {
      if (!list) { list = []; }
      list.push(`<li>${inline(m[2])}</li>`);
      continue;
    }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (!line) continue;
    const h = /^#{1,6}\s+(.*)$/.exec(line);
    out.push(h ? `<p><strong>${inline(h[1])}</strong></p>` : `<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

function renderResearch(result, ui) {
  const answer = document.createElement("div");
  answer.className = "research-answer";
  answer.innerHTML = renderMiniMarkdown(ui.answer || "");
  result.appendChild(answer);
  // Uzun cevaplar katlanır; kaynaklar hemen görünür kalsın. Ölçüm senkron:
  // requestAnimationFrame gizli sekmede hiç çalışmaz.
  (() => {
    if (answer.scrollHeight <= 200) return; // kısa cevap: katlama yok
    answer.classList.add("clamped");
    const more = document.createElement("button");
    more.type = "button";
    more.className = "ghost more";
    more.textContent = "Devamını göster";
    more.onclick = () => {
      const open = answer.classList.toggle("clamped");
      more.textContent = open ? "Devamını göster" : "Daralt";
    };
    answer.after(more);
  })();

  if (ui.sources?.length) {
    const h = document.createElement("div");
    h.className = "research-h";
    h.textContent = `Kaynaklar (${ui.sources.length})`;
    const ol = document.createElement("ol");
    ol.className = "sources";
    for (const src of ui.sources) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = src.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = src.title;
      li.appendChild(a);
      ol.appendChild(li);
    }
    result.append(h, ol);
  } else {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = "Bu cevap arama kaynağına dayanmıyor (grounding yok).";
    result.appendChild(p);
  }

  if (ui.search_queries?.length) {
    const q = document.createElement("div");
    q.className = "chips";
    for (const t of ui.search_queries) {
      const c = document.createElement("span");
      c.className = "chip";
      c.textContent = `🔍 ${t}`;
      q.appendChild(c);
    }
    result.appendChild(q);
  }

  // Google Search grounding koşulu: arama önerileri (search entry point) gösterilmeli.
  // Google'ın verdiği HTML/CSS izole bir iframe'de çalışır; linkler yeni sekmede açılır.
  if (ui.search_entry_html) {
    const f = document.createElement("iframe");
    f.className = "search-entry";
    f.setAttribute("sandbox", "allow-popups allow-popups-to-escape-sandbox");
    f.setAttribute("title", "Google arama önerileri");
    f.srcdoc = `<base target="_blank">${ui.search_entry_html}`;
    f.onload = () => {
      try { f.style.height = `${f.contentDocument.body.scrollHeight + 4}px`; } catch (_) {}
    };
    result.appendChild(f);
  }
}

function onToolResult({ id, name, data, ms, ui }) {
  const card = findCard(id) || createToolCard(id, name, {});
  const status = (data && data.status) || "ok";
  const ok = OK_STATUSES.has(status);
  card.classList.remove("pending");
  card.classList.add(ok ? "ok" : "warn");
  const badge = card.querySelector(".badge");
  badge.textContent = STATUS_TEXT[status] || status;

  const result = card.querySelector(".card-result");
  result.innerHTML = "";
  if (ui?.kind === "research" && ok) {
    renderResearch(result, ui);
    card.querySelector(".card-foot").textContent += ` · ${ui.model}`;
    if (ms != null) card.querySelector(".card-foot").textContent += ` · ${ms} ms`;
    return;
  }
  if (data?.message) {
    const p = document.createElement("p");
    p.className = "card-msg";
    p.textContent = data.message;
    result.appendChild(p);
  }
  // Girdiyle aynı değeri taşıyan alanları sonuçta tekrar gösterme.
  const hidden = new Set(HIDDEN_FIELDS);
  for (const [k, v] of Object.entries(card._args || {})) if (data?.[k] === v) hidden.add(k);
  result.appendChild(kvList(data, hidden));
  if (ms != null) card.querySelector(".card-foot").textContent += ` · ${ms} ms`;
}

// ---------------------------------------------------------------------------
// Agent Assist: tur içgörü kartları + CRM özeti
// ---------------------------------------------------------------------------
const RISK_LABELS = {
  "kayıp_riski": "Kayıp riski", "şikayet_eskalasyonu": "Şikayet eskalasyonu", "yasal_tehdit": "Yasal tehdit",
  "dolandırıcılık_şüphesi": "Dolandırıcılık şüphesi", "kvkk_hassas_veri": "KVKK / hassas veri",
  "tekrarlayan_sorun": "Tekrarlayan sorun", "sosyal_medya_tehdidi": "Sosyal medya tehdidi",
};

function sentimentColor(score) {
  // 0 kırmızı → 50 sarı → 100 yeşil
  const hue = Math.round((Math.max(0, Math.min(100, score)) / 100) * 120);
  return `hsl(${hue} 70% 50%)`;
}

function assistCard(turn) {
  let card = el.cards.querySelector(`.card[data-turn="${turn}"]`);
  if (card) return card;
  clearEmpty(el.cards);
  card = document.createElement("article");
  card.className = "card assist pending";
  card.dataset.turn = turn;
  card.innerHTML = `
    <header class="card-head"><span class="card-icon">🎧</span><span class="card-title"></span><span class="badge">analiz ediliyor…</span></header>
    <p class="quote"></p>
    <div class="card-result"></div>
    <footer class="card-foot"></footer>`;
  card.querySelector(".card-title").textContent = `Müşteri turu #${turn}`;
  el.cards.prepend(card);
  return card;
}

function onAssistPending({ turn, text }) {
  const card = assistCard(turn);
  card.querySelector(".quote").textContent = `“${text}”`;
}

function onAssistInsight({ turn, text, data, errors, ms }) {
  const card = assistCard(turn);
  card.querySelector(".quote").textContent = `“${text}”`;
  card.classList.remove("pending");
  const risks = data.risk_flags || [];
  const hasScore = Number.isInteger(data.sentiment_score);
  card.classList.add(risks.length || (hasScore && data.sentiment_score < 35) ? "warn" : "ok");
  card.querySelector(".badge").textContent = risks.length ? `${risks.length} risk` : "tamam";

  const r = card.querySelector(".card-result");
  r.innerHTML = "";
  if (hasScore) {
    const g = document.createElement("div");
    g.className = "gauge";
    g.innerHTML = `<div class="gauge-top"><span>Duygu</span><strong></strong></div><div class="gauge-bar"><div class="gauge-fill"></div></div>`;
    g.querySelector("strong").textContent = `${data.sentiment_score}/100 · ${data.emotion || ""}`;
    const fill = g.querySelector(".gauge-fill");
    fill.style.width = `${data.sentiment_score}%`;
    fill.style.background = sentimentColor(data.sentiment_score);
    r.appendChild(g);
  }
  const kv = document.createElement("dl");
  kv.className = "kv";
  for (const [label, value] of [["Niyet", data.intent]]) {
    if (!value) continue;
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value;
    kv.append(dt, dd);
  }
  r.appendChild(kv);
  if (data.next_best_action) {
    const nba = document.createElement("div");
    nba.className = "nba";
    nba.innerHTML = `<span>Sonraki en iyi aksiyon</span><p></p>`;
    nba.querySelector("p").textContent = data.next_best_action;
    r.appendChild(nba);
  }
  if (risks.length) {
    const chips = document.createElement("div");
    chips.className = "chips";
    for (const f of risks) {
      const c = document.createElement("span");
      c.className = "chip risk";
      c.textContent = `⚠ ${RISK_LABELS[f] || f}`;
      chips.appendChild(c);
    }
    r.appendChild(chips);
  }
  if (errors?.length) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = `Kısmi sonuç: ${errors.join(", ")}`;
    r.appendChild(p);
  }
  card.querySelector(".card-foot").textContent =
    `${ms} ms · ${data.analyzer || "Flash"} · ${new Date().toLocaleTimeString("tr-TR", { hour12: false })}`;

  state.assist.insights[turn - 1] = data;
  if (hasScore) updateSentimentStat();
}

function updateSentimentStat() {
  const scores = state.assist.insights.filter(Boolean).map((d) => d.sentiment_score).filter(Number.isInteger);
  if (!scores.length) return;
  const last = scores[scores.length - 1];
  const prev = scores.length > 1 ? scores[scores.length - 2] : null;
  const arrow = prev == null ? "" : last > prev ? " ↑" : last < prev ? " ↓" : " →";
  el.statSentiment.textContent = `${last}/100${arrow}`;
  el.statSentiment.style.color = sentimentColor(last);
}

async function createCrmSummary() {
  if (!state.assist.transcript.length) return;
  el.summary.disabled = true;
  el.summary.textContent = "Özet hazırlanıyor…";
  log("out", `CRM özeti istendi (${state.assist.transcript.length} tur)`);
  try {
    const res = await fetch("/api/assist/summary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: state.assist.transcript, insights: state.assist.insights.filter(Boolean) }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    renderCrmCard(body);
    log("in", `CRM özeti hazır (${body.ms} ms)`);
  } catch (err) {
    showBanner(`Özet oluşturulamadı: ${err.message}`);
    log("err", `CRM özeti: ${err.message}`);
  } finally {
    el.summary.textContent = "📝 Özet oluştur (CRM)";
    el.summary.disabled = state.assist.transcript.length === 0;
  }
}

function renderCrmCard({ summary, ms, turns, model }) {
  clearEmpty(el.cards);
  const card = document.createElement("article");
  card.className = "card crm ok";
  card.innerHTML = `
    <header class="card-head"><span class="card-icon">📝</span><span class="card-title">CRM özeti</span><span class="badge"></span></header>
    <p class="crm-ozet"></p>
    <dl class="kv"></dl>
    <div class="crm-lists"></div>
    <div class="row crm-actions"><button type="button" class="ghost copy">JSON'u kopyala</button></div>
    <footer class="card-foot"></footer>`;
  card.querySelector(".badge").textContent = `${summary.oncelik || "?"} öncelik`;
  card.querySelector(".crm-ozet").textContent = summary.ozet || "";
  const kv = card.querySelector(".kv");
  for (const [k, label] of [["musteri_talebi", "Talep"], ["kategori", "Kategori"], ["duygu_seyri", "Duygu seyri"]]) {
    if (!summary[k]) continue;
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = summary[k];
    kv.append(dt, dd);
  }
  const lists = card.querySelector(".crm-lists");
  for (const [k, label] of [["anahtar_bilgiler", "Anahtar bilgiler"], ["yapilan_islemler", "Yapılan işlemler"], ["acik_konular", "Açık konular"], ["sonraki_adimlar", "Sonraki adımlar"]]) {
    const items = summary[k] || [];
    if (!items.length) continue;
    const h = document.createElement("div"); h.className = "research-h"; h.textContent = label;
    const ul = document.createElement("ul");
    for (const it of items) { const li = document.createElement("li"); li.textContent = it; ul.appendChild(li); }
    lists.append(h, ul);
  }
  card.querySelector(".copy").onclick = async (e) => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(summary, null, 2));
      e.target.textContent = "Kopyalandı ✓";
    } catch (_) { e.target.textContent = "Kopyalanamadı"; }
  };
  card.querySelector(".card-foot").textContent = `${turns} tur · ${model} · ${ms} ms`;
  el.cards.prepend(card);
}

// ---------------------------------------------------------------------------
// Base64 <-> ArrayBuffer
// ---------------------------------------------------------------------------
function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
  }
  return btoa(bin);
}

function b64ToBuf(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

// ---------------------------------------------------------------------------
// Ses çıkışı: 24 kHz bağlamda boşluksuz sıralı oynatma
// ---------------------------------------------------------------------------
function rateFromMime(mime) {
  const m = /rate=(\d+)/.exec(mime || "");
  return m ? parseInt(m[1], 10) : OUTPUT_RATE;
}

function playPcm16(buf, rate) {
  const ctx = state.playCtx;
  if (!ctx) return;
  const pcm = new Int16Array(buf);
  if (pcm.length === 0) return;
  const f32 = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 0x8000;

  const audioBuf = ctx.createBuffer(1, f32.length, rate);
  audioBuf.copyToChannel(f32, 0);
  const src = ctx.createBufferSource();
  src.buffer = audioBuf;
  src.connect(ctx.destination);

  // Kuyruk boşaldıysa küçük bir tamponla hemen başla; değilse bir öncekinin bittiği anda.
  const now = ctx.currentTime;
  if (state.nextStartTime < now + 0.01) state.nextStartTime = now + 0.03;
  src.start(state.nextStartTime);
  state.nextStartTime += audioBuf.duration;

  state.activeSources.add(src);
  src.onended = () => state.activeSources.delete(src);
}

/*
 * Barge-in: çalan ve zamanlanmış tüm kaynakları anında durdurur, kuyruğu
 * temizler. Döndürülen değer atılan (henüz çalınmamış) sesin süresidir.
 */
function stopPlayback() {
  const ctx = state.playCtx;
  const droppedSec = ctx ? Math.max(0, state.nextStartTime - ctx.currentTime) : 0;
  const count = state.activeSources.size;
  for (const src of state.activeSources) {
    src.onended = null;
    try { src.stop(); } catch (_) {}
    try { src.disconnect(); } catch (_) {}
  }
  state.activeSources.clear();
  state.nextStartTime = 0;
  return { count, droppedMs: Math.round(droppedSec * 1000) };
}

// ---------------------------------------------------------------------------
// Mikrofon
// ---------------------------------------------------------------------------
async function createCaptureContext(stream) {
  // Tercih: doğrudan 16 kHz bağlam. Bazı tarayıcılar (Firefox) farklı hızdaki
  // mikrofonu bu bağlama bağlayamaz; o durumda yerel hızda açıp worklet'te
  // yeniden örnekleriz.
  let ctx = new AudioContext({ sampleRate: INPUT_RATE });
  let source;
  try {
    source = ctx.createMediaStreamSource(stream);
  } catch (err) {
    log("warn", `16 kHz bağlama bağlanamadı (${err.name}); yerel hız + yeniden örnekleme kullanılıyor`);
    await ctx.close();
    ctx = new AudioContext();
    source = ctx.createMediaStreamSource(stream);
  }
  const url = URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "application/javascript" }));
  try {
    await ctx.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  const node = new AudioWorkletNode(ctx, "pcm-capture", {
    processorOptions: { targetRate: INPUT_RATE, chunkMs: CHUNK_MS },
  });
  source.connect(node);
  // Worklet'in process() çağrılması için grafiğe bağlı olmalı; sessiz çıkış.
  const sink = ctx.createGain();
  sink.gain.value = 0;
  node.connect(sink).connect(ctx.destination);
  log("out", `mikrofon bağlamı ${ctx.sampleRate} Hz → ${INPUT_RATE} Hz PCM16, ${CHUNK_MS} ms parçalar`);
  return { ctx, source, node };
}

async function startMic() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw Object.assign(new Error("Tarayıcı mikrofon erişimini desteklemiyor (HTTPS veya localhost gerekli)."), { name: "Unsupported" });
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  state.micStream = stream;
  stream.getAudioTracks()[0].addEventListener("ended", () => {
    showBanner("Mikrofon bağlantısı kesildi (cihaz çıkarıldı veya izin geri alındı).");
    log("err", "mikrofon izi sonlandı");
    stopSession();
  });

  const { ctx, source, node } = await createCaptureContext(stream);
  state.captureCtx = ctx;
  state.micSource = source;
  state.worklet = node;

  node.port.onmessage = ({ data }) => {
    const { pcm, rms } = data;
    el.micLevel.style.width = `${Math.min(100, rms * 400)}%`;
    const voiced = rms > VOICE_RMS_THRESHOLD && !state.muted;
    if (voiced) {
      state.lastVoiceTs = performance.now();
      if (!state.wasVoiced) state.voiceOnsetTs = state.lastVoiceTs;
    }
    state.wasVoiced = voiced;
    const ws = state.ws;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "audio", data: bufToB64(pcm) }));
      state.bytesUp += pcm.byteLength;
    }
  };
}

function stopMic() {
  try { state.worklet?.port && (state.worklet.port.onmessage = null); } catch (_) {}
  try { state.micSource?.disconnect(); } catch (_) {}
  try { state.worklet?.disconnect(); } catch (_) {}
  state.micStream?.getTracks().forEach((t) => t.stop());
  state.captureCtx?.close().catch(() => {});
  state.micStream = state.micSource = state.worklet = state.captureCtx = null;
  el.micLevel.style.width = "0";
}

function micErrorMessage(err) {
  switch (err.name) {
    case "NotAllowedError":
    case "SecurityError":
      return "Mikrofon izni reddedildi. Adres çubuğundaki kilit simgesinden mikrofon iznini verip tekrar deneyin.";
    case "NotFoundError":
      return "Mikrofon bulunamadı. Bir mikrofon bağlayıp tekrar deneyin.";
    case "NotReadableError":
      return "Mikrofon başka bir uygulama tarafından kullanılıyor olabilir.";
    default:
      return `Mikrofon başlatılamadı: ${err.message || err.name}`;
  }
}

// ---------------------------------------------------------------------------
// Görüntü: kamera / ekran paylaşımı → 1 kare/sn JPEG
// ---------------------------------------------------------------------------
const VIDEO_NAMES = { camera: "Kamera", screen: "Ekran paylaşımı" };

async function toggleVideo(kind) {
  if (state.video?.kind === kind) { stopVideo(); return; }
  stopVideo();
  let stream;
  try {
    if (kind === "camera") {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
    } else {
      if (!navigator.mediaDevices.getDisplayMedia) throw Object.assign(new Error("Tarayıcı ekran paylaşımını desteklemiyor."), { name: "Unsupported" });
      stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: false });
    }
  } catch (err) {
    if (kind === "screen" && err.name === "NotAllowedError") {
      log("warn", "ekran paylaşımı iptal edildi");
      return;
    }
    const msg = err.name === "NotAllowedError" ? "Kamera izni reddedildi. Adres çubuğundaki kilit simgesinden izin verin."
      : err.name === "NotFoundError" ? "Kamera bulunamadı."
      : err.name === "NotReadableError" ? "Kamera başka bir uygulama tarafından kullanılıyor olabilir."
      : `${VIDEO_NAMES[kind]} başlatılamadı: ${err.message || err.name}`;
    showBanner(msg);
    log("err", `${kind}: ${err.name} ${err.message}`);
    return;
  }
  startVideoStream(kind, stream);
}

// Akışı önizlemeye bağlar ve kare zamanlayıcısını başlatır (test için ayrı).
async function startVideoStream(kind, stream) {
  const track = stream.getVideoTracks()[0];
  track.addEventListener("ended", () => {
    if (state.video?.stream === stream) {
      log("warn", `${VIDEO_NAMES[kind]} kaynak tarafından sonlandırıldı`);
      stopVideo();
    }
  });
  el.preview.srcObject = stream;
  // play()'i bekleme: gizli sekmede/önizlemede hiç çözülmeyebilir ve kare
  // gönderimini kilitler. sendFrame zaten readyState'i kontrol ediyor.
  el.preview.play().catch(() => {});
  el.previewWrap.classList.remove("hidden");
  el.previewBadge.textContent = VIDEO_NAMES[kind];
  (kind === "camera" ? el.camera : el.screen).classList.add("active");
  state.video = { kind, stream, busy: false, timer: setInterval(sendFrame, FRAME_INTERVAL_MS) };
  const s = track.getSettings();
  log("out", `${VIDEO_NAMES[kind]} başladı (${s.width || "?"}×${s.height || "?"}), ${1000 / FRAME_INTERVAL_MS} kare/sn`);
}

function stopVideo() {
  const v = state.video;
  if (!v) return;
  clearInterval(v.timer);
  v.stream.getTracks().forEach((t) => t.stop());
  state.video = null;
  el.preview.srcObject = null;
  el.previewWrap.classList.add("hidden");
  el.camera.classList.remove("active");
  el.screen.classList.remove("active");
  log("out", `${VIDEO_NAMES[v.kind]} durduruldu`);
}

async function sendFrame() {
  const v = state.video;
  const ws = state.ws;
  const video = el.preview;
  if (!v || v.busy || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (video.readyState < 2 || !video.videoWidth) return; // henüz kare yok
  v.busy = true;
  try {
    const scale = Math.min(1, MAX_FRAME_EDGE / Math.max(video.videoWidth, video.videoHeight));
    const w = Math.round(video.videoWidth * scale);
    const h = Math.round(video.videoHeight * scale);
    const c = state.frameCanvas;
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    c.getContext("2d").drawImage(video, 0, 0, w, h);
    const blob = await new Promise((res) => c.toBlob(res, "image/jpeg", JPEG_QUALITY));
    if (!blob || state.video !== v || ws.readyState !== WebSocket.OPEN) return;
    const buf = await blob.arrayBuffer();
    ws.send(JSON.stringify({ type: "image", data: bufToB64(buf), source: v.kind }));
    state.framesSent += 1;
    state.frameBytes += buf.byteLength;
    if (state.framesSent === 1) log("out", `ilk kare gönderildi: ${w}×${h}, ${(buf.byteLength / 1024).toFixed(0)} KB`);
    updateStats();
  } catch (err) {
    log("err", `kare yakalanamadı: ${err.message}`);
  } finally {
    v.busy = false;
  }
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = new URLSearchParams({ scenario: el.scenario.value, voice: el.voice.value });
  return `${proto}//${location.host}/ws/live?${q}`;
}

function openSocket() {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl());
    let opened = false;
    ws.onopen = () => { opened = true; resolve(ws); };
    ws.onerror = () => { if (!opened) reject(new Error("WebSocket bağlantısı kurulamadı. Sunucu çalışıyor mu?")); };
    ws.onmessage = (e) => handleServerMessage(e.data);
    ws.onclose = (e) => onSocketClose(e);
  });
}

function onSocketClose(e) {
  log(e.wasClean ? "warn" : "err", `WebSocket kapandı (code=${e.code}${e.reason ? ", " + e.reason : ""})`);
  const unexpected = !state.userClosing;
  state.ws = null;
  if (unexpected) {
    const reason = e.code === 1008 ? "Sunucu bağlantıyı reddetti (izinsiz origin)." :
      "Sunucu bağlantısı koptu. Oturumu yeniden başlatabilirsiniz.";
    showBanner(reason);
    setStatus("error", "Bağlantı koptu");
    addSystemMessage("— bağlantı koptu —");
  }
  teardown();
}

function handleServerMessage(raw) {
  let msg;
  try { msg = JSON.parse(raw); } catch { log("err", "sunucudan geçersiz JSON"); return; }

  switch (msg.type) {
    case "audio": {
      const buf = b64ToBuf(msg.data);
      state.bytesDown += buf.byteLength;
      if (state.awaitingFirstAudio) {
        state.awaitingFirstAudio = false;
        if (state.lastVoiceTs) {
          const ms = Math.round(performance.now() - state.lastVoiceTs);
          el.statLatency.textContent = `${ms} ms`;
          log("lat", `gecikme ≈ ${ms} ms (son konuşma sesinden ilk model sesine)`);
        }
      }
      playPcm16(buf, rateFromMime(msg.mime));
      updateStats();
      break;
    }
    case "transcript":
      onTranscript(msg);
      if (msg.role === "customer" && !msg.partial) {
        state.assist.transcript.push({ role: "müşteri", text: msg.text });
        el.summary.disabled = false;
      }
      if (!msg.partial) log("in", `transcript ${msg.role}: ${msg.text}`);
      break;
    case "interrupted": {
      const { count, droppedMs } = stopPlayback();
      // Tam metin interrupted'dan önce geldiyse balon zaten kapanmıştır; son balonu işaretle.
      const b = state.bubbles.model || state.lastModelBubble;
      if (b) b.classList.add("interrupted");
      state.interruptedBubble = state.bubbles.model; // açıksa, gelecek tam metin buna yazılır
      finalizeBubble("model");
      state.awaitingFirstAudio = true;
      const onset = state.voiceOnsetTs ? ` · konuşma başlangıcından ${Math.round(performance.now() - state.voiceOnsetTs)} ms sonra` : "";
      log("warn", `interrupted: ${count} kaynak durduruldu, ${droppedMs} ms ses atıldı${onset}`);
      break;
    }
    case "assist_pending":
      onAssistPending(msg);
      break;
    case "assist_insight":
      onAssistInsight(msg);
      log("in", `içgörü tur ${msg.turn}: skor=${msg.data.sentiment_score ?? "?"} niyet=${msg.data.intent ?? "?"} risk=${(msg.data.risk_flags || []).join(",") || "-"} (${msg.ms} ms)`);
      break;
    case "tool_call":
      onToolCall(msg);
      log("in", `tool_call ${msg.name}(${JSON.stringify(msg.args)})`);
      break;
    case "tool_result":
      onToolResult(msg);
      log("in", `tool_result ${msg.name} status=${msg.data?.status ?? "?"} (${msg.ms ?? "?"} ms)`);
      break;
    case "turn_complete":
      finalizeBubble("model");
      state.interruptedBubble = null;
      state.lastModelBubble = null;
      state.awaitingFirstAudio = true;
      log("in", "turn_complete");
      break;
    case "telemetry":
      log("in", `event: ${msg.kinds.join(", ")}`);
      break;
    case "session":
      setStatus("live", msg.mode === "assist" ? "Dinleniyor — model konuşmaz" : "Canlı — konuşabilirsiniz");
      el.statModel.textContent = `${msg.model} @ ${msg.location}`;
      addSystemMessage(`Oturum başladı · ${msg.scenario_title || msg.scenario}` +
        (msg.voice ? ` · ses: ${msg.voice}` : " · yalnızca transkripsiyon") +
        (msg.tools?.length ? ` · ${msg.tools.length} araç` : ""));
      log("in", `session ${msg.session_id} model=${msg.model} ses=${msg.voice}`);
      break;
    case "error":
      showBanner(msg.message);
      log("err", `${msg.code}: ${msg.message}`);
      if (msg.code !== "bad_request") setStatus("error", "Hata");
      break;
    case "warning":
      showBanner(msg.message, "warn");
      log("warn", msg.message);
      break;
    case "info":
      log("warn", msg.message);
      addSystemMessage(msg.message);
      break;
    case "pong":
      log("lat", `ws gidiş-dönüş ${Math.round(performance.now() - msg.t)} ms`);
      break;
    default:
      log("warn", `bilinmeyen mesaj: ${msg.type}`);
  }
}

// ---------------------------------------------------------------------------
// Oturum yaşam döngüsü
// ---------------------------------------------------------------------------
async function startSession() {
  hideBanner();
  state.userClosing = false;
  state.bytesUp = state.bytesDown = 0;
  state.framesSent = state.frameBytes = 0;
  state.assist = { transcript: [], insights: [] };
  el.statSentiment.textContent = "–";
  state.lastVoiceTs = 0;
  state.awaitingFirstAudio = true;
  el.cards.innerHTML = '<p class="empty">Araç çağrıları burada kart olarak görünecek.</p>';
  updateStats();
  setControls(true);
  setStatus("connecting", "Bağlanıyor…");

  // Çıkış bağlamı kullanıcı hareketi içinde oluşturulmalı (autoplay politikası).
  state.playCtx = new AudioContext({ sampleRate: OUTPUT_RATE });
  state.nextStartTime = 0;

  try {
    setStatus("connecting", "Mikrofon izni bekleniyor…");
    await startMic();
    setStatus("connecting", "Bağlanıyor…");
  } catch (err) {
    log("err", `mikrofon: ${err.name} ${err.message}`);
    showBanner(micErrorMessage(err));
    setStatus("error", "Mikrofon yok");
    teardown();
    return;
  }

  try {
    state.ws = await openSocket();
    log("out", `bağlandı ${wsUrl()}`);
    state.ws.send(JSON.stringify({ type: "ping", t: performance.now() }));
  } catch (err) {
    showBanner(err.message);
    setStatus("error", "Bağlanamadı");
    log("err", err.message);
    teardown();
  }
}

function stopSession() {
  state.userClosing = true;
  if (state.ws && state.ws.readyState <= WebSocket.OPEN) state.ws.close(1000, "user");
  teardown();
  setStatus("idle", "Bağlı değil");
}

function teardown() {
  stopMic();
  stopVideo();
  stopPlayback();
  state.playCtx?.close().catch(() => {});
  state.playCtx = null;
  finalizeBubble("user");
  finalizeBubble("model");
  state.muted = false;
  el.mute.textContent = "Mikrofonu kapat";
  el.mute.classList.remove("muted-on");
  setControls(false);
}

// ---------------------------------------------------------------------------
// Olaylar
// ---------------------------------------------------------------------------
el.start.onclick = () => startSession();
el.summary.onclick = () => createCrmSummary();
el.camera.onclick = () => toggleVideo("camera");
el.screen.onclick = () => toggleVideo("screen");
el.stop.onclick = () => { stopSession(); addSystemMessage("— oturum bitti —"); };

el.mute.onclick = () => {
  state.muted = !state.muted;
  state.worklet?.port.postMessage({ muted: state.muted });
  el.mute.textContent = state.muted ? "Mikrofonu aç" : "Mikrofonu kapat";
  el.mute.classList.toggle("muted-on", state.muted);
  log("out", state.muted ? "mikrofon susturuldu" : "mikrofon açıldı");
};

function sendText(text) {
  if (!text) return false;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    showBanner("Önce oturumu başlatın.", "warn");
    return false;
  }
  state.ws.send(JSON.stringify({ type: "text", text }));
  if (state.mode === "assist") {
    // Agent Assist: metin doğrudan müşteri cümlesi sayılır (sunucu yankılamaz).
    onTranscript({ role: "customer", text, partial: false });
    state.assist.transcript.push({ role: "müşteri", text });
    el.summary.disabled = false;
  } else {
    onTranscript({ role: "user", text, partial: false });
  }
  state.lastVoiceTs = performance.now();
  state.awaitingFirstAudio = true;
  log("out", `metin: ${text}`);
  return true;
}

el.textForm.onsubmit = (e) => {
  e.preventDefault();
  if (sendText(el.textInput.value.trim())) el.textInput.value = "";
};

$("btn-clear").onclick = () => {
  el.messages.innerHTML = "";
  state.bubbles = { user: null, model: null };
};
$("btn-clear-log").onclick = () => { el.log.innerHTML = ""; };

$("unlock-form").onsubmit = async (e) => {
  e.preventDefault();
  const code = $("unlock-code").value;
  const res = await fetch("/api/unlock", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
  });
  if (res.ok) { $("unlock-code").value = ""; hideBanner(); log("out", "tam demo açıldı"); init(); }
  else showBanner("Erişim kodu hatalı.");
};

function renderScenarioInfo() {
  const s = state.scenarios.find((x) => x.id === el.scenario.value);
  state.mode = s?.mode || "voice";
  const assist = state.mode === "assist";
  el.voice.disabled = assist;
  el.voice.title = assist ? "Agent Assist modunda model konuşmaz" : "";
  el.videoBlock.classList.toggle("hidden", assist);
  el.summary.classList.toggle("hidden", !assist);
  el.scenarioDesc.textContent = s ? s.description : "";
  el.scenarioTools.innerHTML = "";
  for (const t of s?.tools || []) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = toolLabel(t);
    chip.title = t;
    el.scenarioTools.appendChild(chip);
  }
  el.scenarioExamples.innerHTML = "";
  for (const ex of s?.examples || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "example";
    b.textContent = `“${ex}”`;
    b.title = "Oturum açıksa metin olarak gönderir";
    b.onclick = () => sendText(ex);
    el.scenarioExamples.appendChild(b);
  }
}

// Oturum açıkken senaryo veya ses değişirse yeni oturum başlat.
async function onSelectionChange(what) {
  renderScenarioInfo();
  if (!state.ws) return;
  log("warn", `${what} değişti → yeni oturum`);
  stopSession();
  addSystemMessage(`— ${what} değişti, yeni oturum başlatılıyor —`);
  await startSession();
}
el.scenario.onchange = () => onSelectionChange("senaryo");
el.voice.onchange = () => onSelectionChange("ses");
$("btn-clear-cards").onclick = () => { el.cards.innerHTML = ""; };

window.addEventListener("beforeunload", () => { if (state.ws) stopSession(); });

// ---------------------------------------------------------------------------
// Başlangıç
// ---------------------------------------------------------------------------
async function init() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    state.scenarios = cfg.scenarios;
    const locked = !!cfg.locked;
    // Kilitliyse yalnızca herkese açık senaryolar seçilebilir.
    const defaultId = locked ? (cfg.scenarios.find((s) => s.public)?.id || cfg.default_scenario) : cfg.default_scenario;
    el.scenario.innerHTML = cfg.scenarios.map((s) => {
      const disabled = locked && !s.public;
      return `<option value="${s.id}" ${s.id === defaultId ? "selected" : ""} ${disabled ? "disabled" : ""}>${s.title}${disabled ? " 🔒" : ""}</option>`;
    }).join("");
    $("unlock-form").classList.toggle("hidden", !locked);
    el.voice.innerHTML = cfg.voices.map((v) => `<option ${v === cfg.default_voice ? "selected" : ""}>${v}</option>`).join("");
    renderScenarioInfo();
    el.statModel.textContent = cfg.live_model;
    if (cfg.config_error) {
      showBanner(`Sunucu yapılandırma hatası: ${cfg.config_error}`);
      el.start.disabled = true;
    }
    log("out", `yapılandırma yüklendi: live=${cfg.live_model} flash=${cfg.flash_model}`);
  } catch (err) {
    showBanner("Sunucuya ulaşılamadı. `./run.sh` çalışıyor mu?");
    log("err", `config: ${err.message}`);
  }
}

init();
