"""Değiştirilebilir duygu analizi arayüzü (Agent Assist modu).

Uygulama duygu analizini yalnızca `analyze_emotion(text, audio_chunk)` üzerinden
çağırır. Varsayılan uygulama Flash'a yalnızca metni gönderir; kendi modelinizi
bağlamak için `EmotionAnalyzer` protokolüne uyan bir sınıf yazın ve .env'de
belirtin:

    EMOTION_ANALYZER=paketim.modulum:BenimAnalizorum

Sınıf argümansız oluşturulabilmeli ve async `analyze(text, audio_chunk)` metodu
`EmotionResult` döndürmelidir. `audio_chunk`, o müşteri turunun ham sesidir:
16 kHz, 16-bit little-endian, mono PCM (en fazla ASSIST_MAX_TURN_AUDIO_SECONDS).
Ses tabanlı bir model (ör. prozodi / ses tonu) bu baytları doğrudan kullanabilir.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from functools import lru_cache
from typing import Protocol, runtime_checkable

from google.genai import types
from pydantic import BaseModel, Field

from app import config

logger = logging.getLogger("emotion")


class EmotionResult(BaseModel):
    """Duygu analizi sonucu. Tüm analizörler bu biçimi döndürür."""

    sentiment_score: int = Field(
        ge=0, le=100, description="0 = çok olumsuz/öfkeli, 50 = nötr, 100 = çok olumlu/memnun"
    )
    emotion: str = Field(description="Baskın duygu, tek kelime: öfkeli, hayal kırıklığı, endişeli, nötr, memnun…")
    confidence: float = Field(ge=0, le=1, description="0-1 arası güven")
    analyzer: str = Field(default="", description="Sonucu üreten analizörün adı")


@runtime_checkable
class EmotionAnalyzer(Protocol):
    name: str

    async def analyze(self, text: str, audio_chunk: bytes | None) -> EmotionResult: ...


class FlashEmotionAnalyzer:
    """Varsayılan: gemini-3.8-flash ile metin tabanlı duygu analizi (ses kullanılmaz)."""

    name = f"{config.FLASH_MODEL} (metin)"

    _INSTRUCTION = (
        "Bir çağrı merkezi görüşmesinde müşterinin son söylediği cümlenin duygusunu "
        "değerlendir. Yalnızca müşterinin bu cümledeki duygusunu puanla; kibar ama sert "
        "şikayetleri olumsuz say. Türkçe tek kelimelik duygu etiketi kullan."
    )

    async def analyze(self, text: str, audio_chunk: bytes | None) -> EmotionResult:
        from app.tools import _flash_client  # döngüsel içe aktarmayı önlemek için

        response = await _flash_client().aio.models.generate_content(
            model=config.FLASH_MODEL,
            contents=f"Müşterinin cümlesi: {text}",
            config=types.GenerateContentConfig(
                system_instruction=self._INSTRUCTION,
                response_mime_type="application/json",
                response_schema=_EmotionOut,
                # gemini-3.8-flash MINIMAL'ı desteklemiyor (400); en düşük: LOW.
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                temperature=0.2,
            ),
        )
        out = _EmotionOut.model_validate_json(response.text)
        return EmotionResult(**out.model_dump(), analyzer=self.name)


class _EmotionOut(BaseModel):
    sentiment_score: int = Field(ge=0, le=100)
    emotion: str
    confidence: float = Field(ge=0, le=1)


@lru_cache(maxsize=1)
def get_analyzer() -> EmotionAnalyzer:
    """EMOTION_ANALYZER ortam değişkenine göre analizörü yükler (yoksa Flash)."""
    spec = os.getenv("EMOTION_ANALYZER", "").strip()
    if not spec:
        return FlashEmotionAnalyzer()
    module_name, _, class_name = spec.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    analyzer = cls()
    if not isinstance(analyzer, EmotionAnalyzer):
        raise TypeError(f"{spec} EmotionAnalyzer protokolüne uymuyor (name + async analyze).")
    logger.info("Özel duygu analizörü yüklendi: %s", spec)
    return analyzer


async def analyze_emotion(text: str, audio_chunk: bytes | None = None) -> EmotionResult:
    """Uygulamanın tek giriş noktası: seçili analizörle duygu analizi yapar."""
    analyzer = get_analyzer()
    started = time.monotonic()
    result = await analyzer.analyze(text, audio_chunk)
    if not result.analyzer:
        result.analyzer = getattr(analyzer, "name", type(analyzer).__name__)
    logger.info(
        "analyze_emotion: skor=%d duygu=%s (%s, %d ms, ses=%s KB)",
        result.sentiment_score, result.emotion, result.analyzer,
        (time.monotonic() - started) * 1000, len(audio_chunk) // 1024 if audio_chunk else 0,
    )
    return result
