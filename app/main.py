"""FastAPI sunucusu: statik arayüz + /ws/live WebSocket köprüsü.

Her WebSocket bağlantısı kendi ADK Agent + InMemoryRunner + LiveRequestQueue
üçlüsünü alır. İki eşzamanlı görev çalışır:
  (a) upstream  : tarayıcı -> LiveRequestQueue (PCM ses, metin)
  (b) downstream: runner.run_live event'leri -> tarayıcı (ses, transkript, durum)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents import Agent, LiveRequestQueue, RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.events import Event
from google.adk.models import Gemini
from google.adk.runners import InMemoryRunner
from google.genai import types

from app import config, security, tools
from app.assist import build_crm_summary, run_assist_session
from app.scenarios import DEFAULT_SCENARIO, SCENARIOS, Scenario, get_scenario

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("live")

STATIC_DIR = config.ROOT_DIR / "app" / "static"

# Ayarlar başlangıçta yüklenir; eksikse sunucu yine açılır ama WebSocket
# bağlantısında hata kullanıcıya gösterilir.
settings: config.Settings | None = None
settings_error: str | None = None
sec = security.load_security_config()
try:
    settings = config.load_settings()
    logging.getLogger().setLevel(settings.log_level)
except RuntimeError as exc:  # pragma: no cover - yapılandırma hatası
    settings_error = str(exc)
    logger.error("Yapılandırma hatası: %s", exc)


def check_adk_image_routing() -> None:
    """ADK'nın görüntü karelerini doğru alanla ileteceğini doğrular.

    ADK 2.10, Gemini 3.x Live modellerinde image/* blob'larını
    send_realtime_input(video=...) ile gönderir; modeli 3.x Live olarak
    tanımazsa eski `media=` alanına düşer. Model adı değişirse bunu erken görmek
    için açılışta kontrol ediyoruz (özel bir ADK fonksiyonu olduğu için
    bulunamazsa yalnızca uyarı verir).
    """
    try:
        from google.adk.utils.model_name_utils import _is_gemini_3_x_live
    except ImportError:
        logger.warning("ADK görüntü yönlendirmesi doğrulanamadı (iç API değişmiş).")
        return
    if _is_gemini_3_x_live(config.LIVE_MODEL):
        logger.info("Görüntü kareleri %s için realtime 'video' alanıyla gönderilecek.", config.LIVE_MODEL)
    else:
        logger.warning(
            "ADK %s modelini 3.x Live olarak tanımıyor; görüntüler 'media' alanıyla "
            "gönderilecek ve model tarafından yok sayılabilir.", config.LIVE_MODEL,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings:
        logger.info(
            "Başlıyor | proje=%s live=%s@%s flash=%s@%s origins=%s",
            settings.project_id,
            config.LIVE_MODEL,
            settings.live_location,
            config.FLASH_MODEL,
            settings.flash_location,
            settings.allowed_origins,
        )
        logger.info(
            "Güvenlik | erişim kodu=%s origin zorunlu=%s eşzamanlı=%d açık senaryo IP/gün=%d",
            "açık" if security.access_locked(sec) else "yok (yerel)", sec.require_origin,
            sec.max_concurrent_sessions, sec.public_daily_sessions_per_ip,
        )
    check_adk_image_routing()
    yield
    logger.info("Kapanıyor")


app = FastAPI(title="Gerçek Zamanlı Sesli Asistan", lifespan=lifespan)

# CORS yalnızca HTTP uçlarını kapsar; WebSocket için Origin kontrolü
# aşağıda ayrıca yapılır.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins if settings else [],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def api_config(request: Request) -> dict:
    """Arayüzün seçim listelerini doldurması için."""
    return {
        "voices": config.VOICES,
        "default_voice": config.DEFAULT_VOICE,
        "default_scenario": DEFAULT_SCENARIO,
        "scenarios": [
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "tools": [t.__name__ for t in s.tools],
                "examples": s.examples,
                "mode": s.mode,
                "public": s.public,
                "languages": list(s.languages),
                "max_session_seconds": s.max_session_seconds,
            }
            for s in SCENARIOS.values()
        ],
        "live_model": config.LIVE_MODEL,
        "flash_model": config.FLASH_MODEL,
        "transcribe_model": config.TRANSCRIBE_MODEL,
        "config_error": settings_error,
        "locked": security.access_locked(sec) and not security.has_access(sec, request),
    }


@app.post("/api/unlock")
async def api_unlock(request: Request) -> JSONResponse:
    """Tam demo için erişim kodunu doğrular ve HttpOnly çerez bırakır."""
    try:
        code = str((await request.json()).get("code", ""))
    except (ValueError, AttributeError):
        code = ""
    token = security.check_code(sec, code)
    if not token:
        logger.warning("Hatalı erişim kodu denemesi (%s)", security.client_ip(request))
        return JSONResponse({"ok": False, "error": "Erişim kodu hatalı."}, status_code=403)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        security.ACCESS_COOKIE, token, max_age=30 * 24 * 3600, httponly=True,
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        samesite="strict",
    )
    return resp


@app.post("/api/assist/summary")
async def assist_summary(request: Request) -> JSONResponse:
    """Agent Assist: istemcideki transkript + içgörülerden CRM özeti üretir."""
    origin = request.headers.get("origin")
    if settings is None:
        return JSONResponse({"error": settings_error}, status_code=503)
    if origin and "*" not in settings.allowed_origins and origin not in settings.allowed_origins:
        return JSONResponse({"error": "izinsiz origin"}, status_code=403)
    body = await request.json()
    transcript = body.get("transcript") or []
    if not isinstance(transcript, list) or not transcript:
        return JSONResponse({"error": "Özet için transkript yok."}, status_code=400)
    try:
        result = await build_crm_summary(transcript[:500], (body.get("insights") or [])[:500])
    except Exception as exc:  # noqa: BLE001
        logger.exception("CRM özeti üretilemedi")
        return JSONResponse({"error": describe_error(exc)}, status_code=502)
    return JSONResponse(result)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": settings is not None, "error": settings_error}


# ---------------------------------------------------------------------------
# Oturum kurulumu
# ---------------------------------------------------------------------------


def build_agent(scenario: Scenario, lang: str | None = None) -> Agent:
    assert settings is not None
    model = Gemini(
        model=config.LIVE_MODEL,
        client_kwargs=config.live_client_kwargs(settings),
    )
    return Agent(
        name=f"asistan_{scenario.id}",
        model=model,
        instruction=scenario.full_instruction(lang=lang),
        tools=list(scenario.tools),
    )


def build_run_config(voice: str, scenario: Scenario) -> RunConfig:
    # Not: gemini-3.8-live'da enable_affective_dialog / proactivity
    # varsayılan olarak açıktır ve bu bayrakların gönderilmemesi gerekir.
    return RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        # custom_vocabulary: sipariş kodu, marka gibi alan terimlerinin doğru
        # yazıya dökülmesi için (gemini-3.8-live özelliği).
        input_audio_transcription=types.AudioTranscriptionConfig(
            custom_vocabulary=scenario.vocabulary or None
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Kamera/ekran kareleri bağlamı hızla doldurur (1 kare/sn). Kayan pencere
        # sıkıştırması eski içeriği atarak oturumun limit yüzünden kapanmasını önler.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


class ClientChannel:
    """WebSocket'e eşzamanlı güvenli JSON gönderimi."""

    def __init__(self, ws: WebSocket, sid: str) -> None:
        self.ws = ws
        self.sid = sid
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, payload: dict) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
            except (WebSocketDisconnect, RuntimeError) as exc:
                self.closed = True
                logger.info("[%s] gönderim başarısız, istemci kopmuş: %s", self.sid, exc)

    async def error(self, message: str, code: str = "server_error") -> None:
        logger.error("[%s] hata (%s): %s", self.sid, code, message)
        await self.send({"type": "error", "code": code, "message": message})


def origin_allowed(ws: WebSocket) -> bool:
    assert settings is not None
    origin = ws.headers.get("origin")
    if origin is None:  # tarayıcı dışı istemci (ör. test scripti)
        return not sec.require_origin
    return "*" in settings.allowed_origins or origin in settings.allowed_origins


# ---------------------------------------------------------------------------
# Görevler
# ---------------------------------------------------------------------------


async def upstream(
    ws: WebSocket, queue: LiveRequestQueue, ch: ClientChannel, allow_video: bool = True
) -> None:
    """Tarayıcıdan gelen mesajları LiveRequestQueue'ya yazar."""
    audio_chunks = 0
    audio_bytes = 0
    image_frames = 0
    image_bytes = 0
    last_report = time.monotonic()
    while True:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ch.error("Geçersiz JSON mesajı alındı.", "bad_request")
            continue

        mtype = msg.get("type")
        if mtype == "audio":
            try:
                pcm = base64.b64decode(msg["data"])
            except (KeyError, ValueError):
                await ch.error("Ses verisi çözülemedi.", "bad_request")
                continue
            queue.send_realtime(types.Blob(data=pcm, mime_type=config.INPUT_AUDIO_MIME))
            audio_chunks += 1
            audio_bytes += len(pcm)
            now = time.monotonic()
            if now - last_report >= 5:
                logger.debug(
                    "[%s] upstream ses: %d parça, %.1f KB (son 5 sn)",
                    ch.sid, audio_chunks, audio_bytes / 1024,
                )
                if image_frames:
                    logger.info(
                        "[%s] upstream görüntü: %d kare, ort. %.0f KB (son 5 sn)",
                        ch.sid, image_frames, image_bytes / image_frames / 1024,
                    )
                audio_chunks = audio_bytes = image_frames = image_bytes = 0
                last_report = now
        elif mtype == "image":
            if not allow_video:
                continue  # bu senaryoda görüntü kapalı (ör. herkese açık site asistanı)
            try:
                jpeg = base64.b64decode(msg["data"])
            except (KeyError, ValueError):
                await ch.error("Görüntü verisi çözülemedi.", "bad_request")
                continue
            if len(jpeg) > config.MAX_IMAGE_BYTES:
                await ch.error(
                    f"Görüntü karesi çok büyük ({len(jpeg) // 1024} KB), atlandı.", "bad_request"
                )
                continue
            if not jpeg.startswith(b"\xff\xd8"):
                await ch.error("Görüntü karesi JPEG değil, atlandı.", "bad_request")
                continue
            queue.send_realtime(types.Blob(data=jpeg, mime_type=config.IMAGE_MIME))
            image_frames += 1
            image_bytes += len(jpeg)
            if image_frames == 1 and msg.get("source"):
                logger.info("[%s] görüntü akışı: %s", ch.sid, msg["source"])
        elif mtype == "text":
            text = str(msg.get("text", "")).strip()
            if text:
                logger.info("[%s] metin girdi: %s", ch.sid, text)
                queue.send_content(
                    types.Content(role="user", parts=[types.Part(text=text)])
                )
        elif mtype == "ping":
            await ch.send({"type": "pong", "t": msg.get("t")})
        else:
            logger.warning("[%s] bilinmeyen mesaj tipi: %s", ch.sid, mtype)


def event_kinds(event: Event) -> list[str]:
    """Telemetri için event'in içerdiği sinyallerin kısa listesi."""
    kinds: list[str] = []
    if event.content and event.content.parts:
        for p in event.content.parts:
            if p.inline_data:
                kinds.append("audio")
            elif p.text:
                kinds.append("text")
            elif p.function_call:
                kinds.append("function_call")
            elif p.function_response:
                kinds.append("function_response")
    if event.input_transcription:
        kinds.append("input_transcription")
    if event.output_transcription:
        kinds.append("output_transcription")
    if event.interrupted:
        kinds.append("interrupted")
    if event.turn_complete:
        kinds.append("turn_complete")
    if event.error_code:
        kinds.append("error")
    if event.usage_metadata:
        kinds.append("usage")
    if event.go_away:
        kinds.append("go_away")
    if event.voice_activity:
        kinds.append("voice_activity")
    if event.live_session_resumption_update:
        kinds.append("session_resumption")
    if event.grounding_metadata:
        kinds.append("grounding")
    return kinds or ["other"]


async def downstream(
    runner: InMemoryRunner,
    session_id: str,
    user_id: str,
    queue: LiveRequestQueue,
    run_config: RunConfig,
    ch: ClientChannel,
) -> None:
    """run_live event'lerini tarayıcıya iletir."""
    out_audio_chunks = 0
    tool_started: dict[str, float] = {}  # çağrı id -> başlangıç (gecikme ölçümü)
    async for event in runner.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=queue,
        run_config=run_config,
    ):
        kinds = event_kinds(event)
        if kinds != ["audio"]:
            # Ses parçaları çok sık gelir; onları sayarak özetliyoruz.
            logger.info("[%s] event: %s", ch.sid, ",".join(kinds))
            await ch.send({"type": "telemetry", "kinds": kinds, "t": time.time() * 1000})

        if event.error_code:
            await ch.error(
                f"Model hatası: {event.error_code} {event.error_message or ''}".strip(),
                "model_error",
            )
            continue

        # Barge-in: kullanıcı modelin sözünü kesti. İstemci çalan ve kuyruktaki
        # tüm sesi hemen durdurmalı; model o turun üretimini zaten keser, asıl
        # sorun tarayıcıda önceden kuyruğa alınmış saniyelerce sestir.
        if event.interrupted:
            logger.info("[%s] model kesildi (barge-in), %d ses parçası gönderilmişti", ch.sid, out_audio_chunks)
            out_audio_chunks = 0
            await ch.send({"type": "interrupted"})

        for call in event.get_function_calls():
            call_id = call.id or f"{call.name}-{time.monotonic_ns()}"
            tool_started[call_id] = time.monotonic()
            args = dict(call.args or {})
            logger.info("[%s] araç çağrısı %s(%s) id=%s", ch.sid, call.name, args, call_id)
            await ch.send({"type": "tool_call", "id": call_id, "name": call.name, "args": args})

        for resp in event.get_function_responses():
            started = tool_started.pop(resp.id or "", None)
            ms = round((time.monotonic() - started) * 1000) if started else None
            data = to_jsonable(resp.response)
            logger.info(
                "[%s] araç sonucu %s id=%s status=%s (%s ms)",
                ch.sid, resp.name, resp.id, (data or {}).get("status") if isinstance(data, dict) else "?", ms,
            )
            payload = {"type": "tool_result", "id": resp.id, "name": resp.name, "data": data, "ms": ms}
            # Yalnızca arayüze giden ekler (ör. araştırma kaynak linkleri); modele gitmedi.
            ui = tools.pop_ui_extras(resp.id)
            if ui:
                payload["ui"] = ui
            await ch.send(payload)

        if event.content and event.content.parts:
            for part in event.content.parts:
                blob = part.inline_data
                if blob and blob.data and (blob.mime_type or "").startswith("audio/"):
                    out_audio_chunks += 1
                    await ch.send(
                        {
                            "type": "audio",
                            "mime": blob.mime_type,
                            "data": base64.b64encode(blob.data).decode("ascii"),
                        }
                    )

        if event.input_transcription and event.input_transcription.text:
            await ch.send(
                {
                    "type": "transcript",
                    "role": "user",
                    "text": event.input_transcription.text,
                    "partial": bool(event.partial),
                }
            )
        if event.output_transcription and event.output_transcription.text:
            await ch.send(
                {
                    "type": "transcript",
                    "role": "model",
                    "text": event.output_transcription.text,
                    "partial": bool(event.partial),
                }
            )

        if event.turn_complete:
            logger.info("[%s] tur tamamlandı, %d ses parçası gönderildi", ch.sid, out_audio_chunks)
            out_audio_chunks = 0
            await ch.send({"type": "turn_complete"})

        if event.go_away:
            await ch.send(
                {
                    "type": "warning",
                    "message": "Sunucu oturumu yakında kapatacak (go_away). Yeni oturum başlatın.",
                }
            )


# ---------------------------------------------------------------------------
# WebSocket ucu
# ---------------------------------------------------------------------------


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    sid = uuid.uuid4().hex[:8]

    if settings is None:
        await ws.accept()
        ch = ClientChannel(ws, sid)
        await ch.error(settings_error or "Sunucu yapılandırılmamış.", "config_error")
        await ws.close(code=1011)
        return

    if not origin_allowed(ws):
        logger.warning("[%s] reddedildi, izinsiz origin: %s", sid, ws.headers.get("origin"))
        await ws.close(code=1008)
        return

    await ws.accept()
    ch = ClientChannel(ws, sid)

    voice = ws.query_params.get("voice") or config.DEFAULT_VOICE
    if voice not in config.VOICES:
        voice = config.DEFAULT_VOICE
    scenario = get_scenario(ws.query_params.get("scenario"))
    lang = ws.query_params.get("lang")
    user_id = f"user-{sid}"
    ip = security.client_ip(ws)

    if not scenario.public and not security.has_access(sec, ws):
        await ch.error("Bu demo için erişim kodu gerekiyor.", "access_denied")
        await ws.close(code=1008)
        return
    reason = security.limiter.try_acquire(sec, ip, scenario.public)
    if reason:
        logger.warning("[%s] sınır: %s (ip=%s)", sid, reason, ip)
        await ch.error(reason, "rate_limited")
        await ws.close(code=1013)
        return

    logger.info(
        "[%s] bağlandı | senaryo=%s ses=%s dil=%s ip=%s aktif=%d",
        sid, scenario.id, voice, lang or "-", ip, security.limiter.active,
    )
    try:
        if scenario.mode == "assist":
            await ws_assist(ws, ch, scenario, user_id)
        else:
            await ws_voice(ws, ch, scenario, voice, lang, user_id)
    finally:
        security.limiter.release()


async def session_watchdog(ch: ClientChannel, max_seconds: int) -> None:
    """Oturum süresini sınırlar; bitmeden 20 sn önce uyarır."""
    warn_at = max(0, max_seconds - 20)
    await asyncio.sleep(warn_at)
    await ch.send({"type": "session_limit", "remaining": max_seconds - warn_at})
    await asyncio.sleep(max_seconds - warn_at)
    logger.info("[%s] oturum süresi doldu (%ss)", ch.sid, max_seconds)
    await ch.send({"type": "session_ended", "reason": "time_limit", "max_seconds": max_seconds})


async def ws_voice(
    ws: WebSocket, ch: ClientChannel, scenario: Scenario, voice: str, lang: str | None, user_id: str
) -> None:
    assert settings is not None
    sid = ch.sid

    queue = LiveRequestQueue()
    runner: InMemoryRunner | None = None
    tasks: list[asyncio.Task] = []
    try:
        agent = build_agent(scenario, lang)
        runner = InMemoryRunner(agent=agent, app_name=settings.app_name)
        session = await runner.session_service.create_session(
            app_name=settings.app_name, user_id=user_id
        )
        run_config = build_run_config(voice, scenario)

        await ch.send(
            {
                "type": "session",
                "session_id": sid,
                "model": config.LIVE_MODEL,
                "location": settings.live_location,
                "voice": voice,
                "scenario": scenario.id,
                "scenario_title": scenario.title,
                "tools": [t.__name__ for t in scenario.tools],
                "lang": lang if scenario.languages else None,
                "max_session_seconds": scenario.max_session_seconds,
            }
        )

        tasks = [
            asyncio.create_task(upstream(ws, queue, ch, scenario.allow_video), name=f"up-{sid}"),
            asyncio.create_task(session_watchdog(ch, scenario.max_session_seconds), name=f"limit-{sid}"),
            asyncio.create_task(
                downstream(runner, session.id, user_id, queue, run_config, ch),
                name=f"down-{sid}",
            ),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is None:
                if task.get_name().startswith("down"):
                    await ch.send({"type": "info", "message": "Model oturumu sona erdi."})
                continue
            if isinstance(exc, WebSocketDisconnect):
                ch.closed = True  # istemci zaten kapattı; tekrar close() gönderme
                logger.info("[%s] istemci bağlantıyı kapattı (code=%s)", sid, exc.code)
            else:
                logger.exception("[%s] görev hatası", sid, exc_info=exc)
                await ch.error(describe_error(exc), "model_error")
    except Exception as exc:  # noqa: BLE001 - kullanıcıya gösterilecek
        logger.exception("[%s] oturum kurulamadı", sid)
        await ch.error(describe_error(exc), "session_error")
    finally:
        queue.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if runner is not None:
            try:
                await runner.close()
            except Exception:  # noqa: BLE001
                logger.debug("[%s] runner kapatılırken hata", sid, exc_info=True)
        if not ch.closed:
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass  # istemci bu arada kopmuş olabilir
        logger.info("[%s] oturum kapandı", sid)


async def ws_assist(ws: WebSocket, ch: ClientChannel, scenario: Scenario, user_id: str) -> None:
    """Agent Assist modu: transkripsiyon + tur bazlı içgörü (bkz. app/assist.py)."""
    assert settings is not None
    try:
        await run_assist_session(ws, ch, settings, scenario, user_id)
    except WebSocketDisconnect as exc:
        ch.closed = True
        logger.info("[%s] istemci bağlantıyı kapattı (code=%s)", ch.sid, exc.code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] assist oturum hatası", ch.sid)
        await ch.error(describe_error(exc), "session_error")
    finally:
        if not ch.closed:
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
        logger.info("[%s] assist oturumu kapandı", ch.sid)


def to_jsonable(value):
    """Araç yanıtını JSON'a güvenle çevirir (tarih vb. değerler metne döner)."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def describe_error(exc: BaseException) -> str:
    """Sık karşılaşılan hataları anlaşılır Türkçe mesajlara çevirir."""
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "default credentials" in lowered or "defaultcredentialserror" in lowered:
        return (
            "Google Cloud kimlik bilgisi bulunamadı. "
            "`gcloud auth application-default login` çalıştırın. (" + text + ")"
        )
    if "permission" in lowered or "403" in lowered:
        return "Vertex AI erişim izni reddedildi (IAM / API etkin mi?). (" + text + ")"
    if "not found" in lowered or "404" in lowered:
        return "Model veya bölge bulunamadı; LIVE_LOCATION ve model adını kontrol edin. (" + text + ")"
    if "resource_exhausted" in lowered or "429" in lowered:
        return "Kota aşıldı (429). Biraz bekleyip tekrar deneyin. (" + text + ")"
    return text
