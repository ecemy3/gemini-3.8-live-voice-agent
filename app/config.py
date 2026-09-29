"""Uygulama yapılandırması.

Tüm model adları ve ortam değişkenleri tek yerde tutulur. Gizli bilgi yoktur;
kimlik doğrulama Application Default Credentials (ADC) ile yapılır.

Model adları Vertex AI dokümantasyonundan doğrulandı (2026-09-29):
  - gemini-3.8-live  : GA, 24 Eyl 2026. Bölge: us-central1 (+ us/eu multi-region).
                       Giriş 16 kHz PCM, çıkış 24 kHz PCM.
  - gemini-3.8-flash : GA, 2 Eyl 2026. Bölge: us (multi-region; global da var ama
                       ölçümde 5-99 sn dalgalandı, us 1,8-2,8 sn).
                       Google Search grounding destekli.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
# override=True: run.sh .env'yi ortama da yükler; --reload sonrası .env'deki
# değişikliklerin eski ortam değerlerinin önüne geçmesi için.
load_dotenv(ROOT_DIR / ".env", override=True)

# --- Model sabitleri --------------------------------------------------------
LIVE_MODEL = "gemini-3.8-live"
FLASH_MODEL = "gemini-3.8-flash"
# Agent Assist: yalnızca yazıya döken Live modeli (Preview, yalnızca global,
# oturum başına en fazla 10 dk ses; cevap üretmez).
TRANSCRIBE_MODEL = "gemini-3.5-transcribe-live-preview"

# --- Ses sabitleri -----------------------------------------------------------
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
INPUT_AUDIO_MIME = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"

# --- Görüntü sabitleri -------------------------------------------------------
IMAGE_MIME = "image/jpeg"
MAX_IMAGE_BYTES = 1_500_000  # 1024 px / kalite 0.6 JPEG tipik olarak 50-250 KB

# Gemini Live prebuilt sesleri (arayüzde seçilebilir).
VOICES = ["Aoede", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Zephyr"]
DEFAULT_VOICE = "Aoede"


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    project_id: str
    # Live ve Flash farklı bölgelerde olabilir; her biri kendi client'ını
    # kendi location'ı ile oluşturur. GOOGLE_CLOUD_LOCATION global olarak
    # kullanılmaz, böylece iki model arasında ortam değişkeni çakışması olmaz.
    live_location: str
    flash_location: str
    transcribe_location: str
    allowed_origins: list[str] = field(default_factory=list)
    log_level: str = "INFO"
    app_name: str = "live-voice-assistant"


def load_settings() -> Settings:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project_id:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT tanımlı değil. .env.example dosyasını .env "
            "olarak kopyalayıp doldurun."
        )
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").upper()
    if use_vertex not in ("TRUE", "1"):
        logging.getLogger(__name__).warning(
            "GOOGLE_GENAI_USE_VERTEXAI=TRUE değil; bu uygulama Vertex AI için yazıldı."
        )
    return Settings(
        project_id=project_id,
        live_location=os.getenv("LIVE_LOCATION", "us-central1").strip(),
        flash_location=os.getenv("FLASH_LOCATION", "us").strip(),
        transcribe_location=os.getenv("TRANSCRIBE_LOCATION", "global").strip(),
        allowed_origins=_split_csv(
            os.getenv("ALLOWED_ORIGINS", "http://localhost:8765,http://127.0.0.1:8765")
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


def live_client_kwargs(settings: Settings) -> dict:
    """Live modeli için google.genai.Client argümanları."""
    return {
        "vertexai": True,
        "project": settings.project_id,
        "location": settings.live_location,
    }


def flash_client_kwargs(settings: Settings) -> dict:
    """Flash modeli için google.genai.Client argümanları."""
    return {
        "vertexai": True,
        "project": settings.project_id,
        "location": settings.flash_location,
    }


def transcribe_client_kwargs(settings: Settings) -> dict:
    """Agent Assist transkripsiyon modeli için client argümanları."""
    return {
        "vertexai": True,
        "project": settings.project_id,
        "location": settings.transcribe_location,
    }
