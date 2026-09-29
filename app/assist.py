"""Çağrı merkezi Agent Assist modu.

Model konuşmaz; yalnızca müşterinin sesini yazıya döker
(gemini-3.5-transcribe-live-preview, ADK run_live üzerinden). Her müşteri turu
bittiğinde arka planda iki paralel analiz çalışır:
  - analyze_emotion(text, audio_chunk)  -> sentiment_score (değiştirilebilir, app/emotion.py)
  - Flash                               -> intent, next_best_action, risk_flags
Sonuç tek bir kart olarak istemciye gider. Çağrı sonunda "Özet oluştur" ile
Flash'tan yapılandırılmış bir CRM özeti alınır.

Neden 3.8 Live değil: Vertex, gemini-3.8-live için TEXT modalitesini kabul etmiyor
("Unsupported modality"); AUDIO modunda ise model konuşur. Transcribe modeli ise
cevap üretmez. Kısıtı: Preview ve oturum başına en fazla 10 dk ses; bu yüzden
oturum ASSIST_SEGMENT_SECONDS'da bir sessizce yenilenir.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import WebSocket
from google.adk.agents import Agent, LiveRequestQueue, RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.models import Gemini
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, Field

from app import config
from app.emotion import analyze_emotion
from app.scenarios import Scenario

logger = logging.getLogger("assist")

# Transcribe Live modelinin 10 dk sınırından önce oturumu yenile.
ASSIST_SEGMENT_SECONDS = 540
ASSIST_MAX_TURN_AUDIO_SECONDS = 60
MAX_TURN_AUDIO_BYTES = config.INPUT_SAMPLE_RATE * 2 * ASSIST_MAX_TURN_AUDIO_SECONDS
CONTEXT_TURNS = 8  # niyet analizi için geriye dönük tur sayısı
INSIGHT_TIMEOUT_S = 15  # kart sonsuza kadar "analiz ediliyor" kalmasın


# ---------------------------------------------------------------------------
# Flash çıktı şemaları
# ---------------------------------------------------------------------------


class TurnInsightOut(BaseModel):
    intent: str = Field(description="Müşterinin bu turdaki niyeti, kısa Türkçe ifade (ör. 'sipariş iptali')")
    next_best_action: str = Field(description="Temsilcinin şimdi yapması/söylemesi gereken en iyi adım, tek cümle")
    risk_flags: list[str] = Field(
        description=(
            "Varsa risk işaretleri: 'kayıp_riski', 'şikayet_eskalasyonu', 'yasal_tehdit', "
            "'dolandırıcılık_şüphesi', 'kvkk_hassas_veri', 'tekrarlayan_sorun', 'sosyal_medya_tehdidi'. "
            "Yoksa boş liste."
        )
    )


class CrmSummaryOut(BaseModel):
    ozet: str = Field(description="Görüşmenin 2-3 cümlelik özeti")
    musteri_talebi: str
    kategori: str = Field(description="ör. sipariş, iade, ödeme, kart, teknik, bilgi, şikayet")
    oncelik: str = Field(description="düşük, orta veya yüksek")
    duygu_seyri: str = Field(description="Görüşme boyunca müşteri duygusunun nasıl değiştiği, tek cümle")
    yapilan_islemler: list[str]
    acik_konular: list[str]
    sonraki_adimlar: list[str]
    anahtar_bilgiler: list[str] = Field(description="Sipariş no, kart son 4 hane, tarih gibi geçen önemli bilgiler")


INSIGHT_INSTRUCTION = (
    "Bir çağrı merkezi temsilcisine canlı yardım eden asistansın (Agent Assist). Müşterinin "
    "son cümlesini ve önceki bağlamı değerlendir. Temsilciye pratik, uygulanabilir ve kısa "
    "öneriler ver. Müşteriye değil temsilciye hitap et. Türkçe yaz."
)

SUMMARY_INSTRUCTION = (
    "Bir çağrı merkezi görüşmesinin transkriptinden CRM kaydı için özet çıkar. Yalnızca "
    "transkriptte geçen bilgileri kullan; bilgi yoksa ilgili listeyi boş bırak. Türkçe yaz, "
    "kısa ve iş diline uygun ol."
)


def _flash():
    from app.tools import _flash_client  # tek Flash client (FLASH_LOCATION)

    return _flash_client()


async def analyze_turn(text: str, history: list[str]) -> TurnInsightOut:
    context = "\n".join(f"- {t}" for t in history[-CONTEXT_TURNS:]) or "(ilk tur)"
    response = await _flash().aio.models.generate_content(
        model=config.FLASH_MODEL,
        contents=f"Önceki müşteri cümleleri:\n{context}\n\nMüşterinin son cümlesi: {text}",
        config=types.GenerateContentConfig(
            system_instruction=INSIGHT_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=TurnInsightOut,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.3,
        ),
    )
    return TurnInsightOut.model_validate_json(response.text)


async def build_crm_summary(transcript: list[dict], insights: list[dict]) -> dict:
    """Konuşmanın tamamından CRM özeti üretir (HTTP ucundan çağrılır)."""
    lines = [f"{t.get('role', 'müşteri')}: {t.get('text', '')}" for t in transcript if t.get("text")]
    scores = [i.get("sentiment_score") for i in insights if isinstance(i.get("sentiment_score"), int)]
    trend = f"Tur bazlı duygu skorları (0-100): {scores}" if scores else "Duygu skoru yok."
    started = time.monotonic()
    response = await _flash().aio.models.generate_content(
        model=config.FLASH_MODEL,
        contents="Transkript:\n" + "\n".join(lines) + f"\n\n{trend}",
        config=types.GenerateContentConfig(
            system_instruction=SUMMARY_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=CrmSummaryOut,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.2,
        ),
    )
    summary = CrmSummaryOut.model_validate_json(response.text).model_dump()
    ms = round((time.monotonic() - started) * 1000)
    logger.info("CRM özeti üretildi: %d tur, %d ms", len(lines), ms)
    return {"summary": summary, "ms": ms, "turns": len(lines), "model": config.FLASH_MODEL}


# ---------------------------------------------------------------------------
# Canlı oturum
# ---------------------------------------------------------------------------


@dataclass
class AssistState:
    queue: LiveRequestQueue
    history: list[str] = field(default_factory=list)
    turn_audio: bytearray = field(default_factory=bytearray)
    turn: int = 0
    tasks: set[asyncio.Task] = field(default_factory=set)


def _build_transcriber(settings: config.Settings, scenario: Scenario) -> tuple[InMemoryRunner, RunConfig]:
    agent = Agent(
        name="agent_assist_transkript",
        model=Gemini(model=config.TRANSCRIBE_MODEL, client_kwargs=config.transcribe_client_kwargs(settings)),
    )
    runner = InMemoryRunner(agent=agent, app_name=settings.app_name)
    run_config = RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=["tr-TR"],
            custom_vocabulary=scenario.vocabulary or None,
        ),
    )
    return runner, run_config


async def run_assist_session(ws: WebSocket, ch, settings: config.Settings, scenario: Scenario, user_id: str) -> None:
    """Agent Assist WebSocket oturumu. ch: main.ClientChannel."""
    state = AssistState(queue=LiveRequestQueue())

    await ch.send({
        "type": "session", "mode": "assist", "session_id": ch.sid,
        "model": config.TRANSCRIBE_MODEL, "location": settings.transcribe_location,
        "scenario": scenario.id, "scenario_title": scenario.title, "tools": [],
        "analyzer_model": config.FLASH_MODEL,
    })

    def on_customer_turn(text: str) -> None:
        state.turn += 1
        audio = bytes(state.turn_audio)
        state.turn_audio.clear()
        history = list(state.history)
        state.history.append(text)
        task = asyncio.create_task(_insight_task(ch, state.turn, text, audio, history))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)

    up = asyncio.create_task(_assist_upstream(ws, ch, state, on_customer_turn), name=f"aup-{ch.sid}")
    down = asyncio.create_task(_assist_supervisor(ch, settings, scenario, user_id, state, on_customer_turn), name=f"adown-{ch.sid}")
    try:
        done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception():
                raise t.exception()
    finally:
        for t in (up, down, *state.tasks):
            t.cancel()
        await asyncio.gather(up, down, *state.tasks, return_exceptions=True)
        state.queue.close()


async def _assist_upstream(ws: WebSocket, ch, state: AssistState, on_customer_turn) -> None:
    while True:
        try:
            msg = json.loads(await ws.receive_text())
        except json.JSONDecodeError:
            await ch.error("Geçersiz JSON mesajı alındı.", "bad_request")
            continue
        mtype = msg.get("type")
        if mtype == "audio":
            pcm = base64.b64decode(msg.get("data", ""))
            state.queue.send_realtime(types.Blob(data=pcm, mime_type=config.INPUT_AUDIO_MIME))
            if len(state.turn_audio) + len(pcm) <= MAX_TURN_AUDIO_BYTES:
                state.turn_audio.extend(pcm)
        elif mtype == "text":
            # Mikrofonsuz test: metin doğrudan müşteri cümlesi sayılır.
            text = str(msg.get("text", "")).strip()
            if text:
                logger.info("[%s] metin müşteri turu: %s", ch.sid, text)
                on_customer_turn(text)
        elif mtype == "ping":
            await ch.send({"type": "pong", "t": msg.get("t")})
        elif mtype == "image":
            pass  # Agent Assist görüntü kullanmaz
        else:
            logger.warning("[%s] assist: bilinmeyen mesaj tipi %s", ch.sid, mtype)


async def _assist_supervisor(ch, settings, scenario, user_id, state: AssistState, on_customer_turn) -> None:
    """Transkripsiyon oturumlarını sırayla çalıştırır; 10 dk sınırından önce yeniler."""
    segment = 0
    while True:
        segment += 1
        seg_started = time.monotonic()
        runner, run_config = _build_transcriber(settings, scenario)
        session = await runner.session_service.create_session(app_name=settings.app_name, user_id=user_id)
        queue = state.queue
        logger.info("[%s] transkripsiyon segmenti #%d başladı", ch.sid, segment)
        task = asyncio.create_task(_transcribe_segment(ch, runner, session.id, user_id, queue, run_config, on_customer_turn))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=ASSIST_SEGMENT_SECONDS)
            # Model oturumu kendiliğinden bitti (ör. sınır/go_away) -> yenile
            logger.info("[%s] segment #%d sunucu tarafından bitti, yenileniyor", ch.sid, segment)
        except asyncio.TimeoutError:
            logger.info("[%s] segment #%d süre doldu (%ss), yenileniyor", ch.sid, segment, ASSIST_SEGMENT_SECONDS)
        if time.monotonic() - seg_started < 5:
            # Hemen kapanan oturumu sonsuz döngüde yenileme.
            raise RuntimeError("Transkripsiyon oturumu açıldıktan hemen sonra kapandı.")
        # Yeni kuyruğa geç; eski segmente son cümlesini bitirmesi için kısa süre tanı.
        state.queue = LiveRequestQueue()
        await ch.send({"type": "info", "message": f"Transkripsiyon oturumu yenilendi (#{segment + 1})."})
        await asyncio.sleep(2.5)
        queue.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runner.close()


async def _transcribe_segment(ch, runner, session_id, user_id, queue, run_config, on_customer_turn) -> None:
    async for event in runner.run_live(
        user_id=user_id, session_id=session_id, live_request_queue=queue, run_config=run_config
    ):
        if event.error_code:
            await ch.error(f"Transkripsiyon hatası: {event.error_code} {event.error_message or ''}", "model_error")
            continue
        tr = event.input_transcription
        if tr and tr.text:
            await ch.send({"type": "telemetry", "kinds": ["input_transcription"], "t": time.time() * 1000})
            if event.partial:
                await ch.send({"type": "transcript", "role": "customer", "text": tr.text, "partial": True})
                continue
            text = tr.text.strip()
            logger.info("[%s] müşteri: %s", ch.sid, text)
            await ch.send({"type": "transcript", "role": "customer", "text": text, "partial": False})
            on_customer_turn(text)
        elif event.content and event.content.parts and any(p.text for p in event.content.parts):
            # Transcribe modeli cevap üretmemeli; üretirse görünür olsun.
            logger.warning("[%s] beklenmeyen model metni yok sayıldı", ch.sid)


async def _insight_task(ch, turn: int, text: str, audio: bytes, history: list[str]) -> None:
    started = time.monotonic()
    await ch.send({"type": "assist_pending", "turn": turn, "text": text})
    emotion_res, insight_res = await asyncio.gather(
        asyncio.wait_for(analyze_emotion(text, audio or None), INSIGHT_TIMEOUT_S),
        asyncio.wait_for(analyze_turn(text, history), INSIGHT_TIMEOUT_S),
        return_exceptions=True,
    )
    ms = round((time.monotonic() - started) * 1000)
    data: dict = {}
    errors: list[str] = []
    if isinstance(emotion_res, TimeoutError):
        logger.warning("[%s] duygu analizi %ss içinde bitmedi", ch.sid, INSIGHT_TIMEOUT_S)
        errors.append("duygu: zaman aşımı")
    elif isinstance(emotion_res, Exception):
        logger.error("[%s] duygu analizi hatası: %s", ch.sid, emotion_res, exc_info=emotion_res)
        errors.append(f"duygu: {type(emotion_res).__name__}")
    else:
        data.update(emotion_res.model_dump())
    if isinstance(insight_res, TimeoutError):
        logger.warning("[%s] niyet analizi %ss içinde bitmedi", ch.sid, INSIGHT_TIMEOUT_S)
        errors.append("niyet: zaman aşımı")
    elif isinstance(insight_res, Exception):
        logger.error("[%s] niyet analizi hatası: %s", ch.sid, insight_res, exc_info=insight_res)
        errors.append(f"niyet: {type(insight_res).__name__}")
    else:
        data.update(insight_res.model_dump())
    logger.info("[%s] tur %d içgörü (%d ms): %s", ch.sid, turn, ms, {k: data.get(k) for k in ("sentiment_score", "intent", "risk_flags")})
    await ch.send({"type": "assist_insight", "turn": turn, "text": text, "data": data, "errors": errors, "ms": ms})
