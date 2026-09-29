"""Ajan araçları (sahte veriyle çalışır).

ADK, fonksiyonun adını, docstring'ini ve tip ipuçlarını modele araç tanımı olarak
gönderir; bu yüzden docstring'ler modelin okuyacağı şekilde net yazılmıştır.

gemini-3.8-live kuralı: araçlar asla boş sözlük / None döndürmez. Her yanıt
`status`, `retryable` ve `message` alanlarını içerir; aksi halde model
parametre varyasyonlarıyla tekrar tekrar arama yapabilir.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
import zlib
from datetime import date, datetime, timedelta

from google import genai
from google.adk.tools import ToolContext
from google.genai import types

from app import config

logger = logging.getLogger("tools")

# Gerçek bir arka uca gidiyormuş hissi için yapay gecikme (saniye).
FAKE_LATENCY = (0.4, 0.9)


async def _latency() -> None:
    await asyncio.sleep(random.uniform(*FAKE_LATENCY))


def _normalize_id(value: str) -> str:
    """'sp 1001', 'SP-1001', 'bin bir' gibi transkripsiyon varyasyonlarını toparlar."""
    digits = "".join(ch for ch in value if ch.isdigit())
    return f"SP-{digits}" if digits else value.strip().upper()


# ---------------------------------------------------------------------------
# Sahte veri
# ---------------------------------------------------------------------------
_today = date.today()

ORDERS: dict[str, dict] = {
    "SP-1001": {
        "musteri": "Ayşe Yılmaz",
        "urun": "Kablosuz kulaklık",
        "tutar_tl": 1899.90,
        "durum": "kargoda",
        "kargo_firmasi": "Yurtiçi Kargo",
        "tahmini_teslim": (_today + timedelta(days=1)).isoformat(),
        "siparis_tarihi": (_today - timedelta(days=2)).isoformat(),
        "iade_durumu": None,
    },
    "SP-1002": {
        "musteri": "Mehmet Kaya",
        "urun": "Akıllı saat",
        "tutar_tl": 4250.00,
        "durum": "teslim edildi",
        "kargo_firmasi": "Aras Kargo",
        "teslim_tarihi": (_today - timedelta(days=5)).isoformat(),
        "siparis_tarihi": (_today - timedelta(days=9)).isoformat(),
        "iade_durumu": None,
    },
    "SP-1003": {
        "musteri": "Zeynep Demir",
        "urun": "Kahve makinesi",
        "tutar_tl": 3120.50,
        "durum": "hazırlanıyor",
        "kargo_firmasi": None,
        "tahmini_teslim": (_today + timedelta(days=3)).isoformat(),
        "siparis_tarihi": _today.isoformat(),
        "iade_durumu": None,
    },
    "SP-1004": {
        "musteri": "Ali Çelik",
        "urun": "Koşu ayakkabısı",
        "tutar_tl": 2299.00,
        "durum": "teslim edildi",
        "kargo_firmasi": "MNG Kargo",
        "teslim_tarihi": (_today - timedelta(days=40)).isoformat(),
        "siparis_tarihi": (_today - timedelta(days=44)).isoformat(),
        "iade_durumu": None,
    },
}

RETURN_WINDOW_DAYS = 30

CARDS: dict[str, dict] = {
    "4821": {"sahibi": "Ayşe Yılmaz", "tip": "kredi kartı", "durum": "aktif"},
    "7390": {"sahibi": "Mehmet Kaya", "tip": "banka kartı", "durum": "aktif"},
    "1156": {"sahibi": "Zeynep Demir", "tip": "kredi kartı", "durum": "dondurulmuş"},
}

# Restoran: her gün için saat -> boş masa sayısı (4 kişilik masa varsayımı)
OPENING_SLOTS = ["18:00", "18:30", "19:00", "19:30", "20:00", "20:30", "21:00", "21:30"]
MAX_PARTY = 12
RESERVATIONS: list[dict] = []

SERVICES: dict[str, dict] = {
    "e-posta": {"durum": "çalışıyor", "not": None},
    "vpn": {
        "durum": "kısmi kesinti",
        "not": "Avrupa sunucularında bağlantı yavaşlığı; ekip müdahale ediyor, tahmini çözüm 1 saat.",
    },
    "muhasebe": {"durum": "çalışıyor", "not": None},
    "wifi": {"durum": "çalışıyor", "not": None},
    "yazıcı": {
        "durum": "bakımda",
        "not": "3. kat yazıcısı bakımda; 2. kattaki yazıcıyı kullanın.",
    },
}
TICKETS: list[dict] = []


# ---------------------------------------------------------------------------
# Müşteri hizmetleri
# ---------------------------------------------------------------------------


async def get_order_status(order_id: str) -> dict:
    """Bir siparişin güncel durumunu, kargo bilgisini ve tahmini teslim tarihini getirir.

    Args:
        order_id: Sipariş numarası, örneğin "SP-1001". Kullanıcı yalnızca rakamları
            söylerse ("bin bir") rakamları gönder; önek otomatik eklenir.

    Returns:
        status alanı "ok" ise sipariş bilgilerini, "not_found" ise açıklamayı içerir.
    """
    await _latency()
    oid = _normalize_id(order_id)
    order = ORDERS.get(oid)
    logger.info("get_order_status(%s) -> %s", oid, "bulundu" if order else "yok")
    if not order:
        return {
            "status": "not_found",
            "retryable": False,
            "order_id": oid,
            "message": (
                f"{oid} numaralı sipariş bulunamadı. Başka numaralarla deneme yapma; "
                "kullanıcıdan sipariş numarasını kontrol edip tekrar söylemesini iste."
            ),
        }
    return {"status": "ok", "retryable": False, "order_id": oid, **order,
            "message": f"{oid} siparişinin durumu: {order['durum']}."}


async def approve_refund(order_id: str, reason: str) -> dict:
    """Teslim edilmiş bir sipariş için iade talebini değerlendirir ve uygunsa onaylar.

    Bu işlem geri alınamaz. Çağırmadan önce kullanıcıdan sipariş numarasını ve iade
    nedenini sözlü olarak teyit et.

    Args:
        order_id: İade edilecek siparişin numarası, örneğin "SP-1002".
        reason: Kullanıcının belirttiği iade nedeni (kısa, Türkçe).

    Returns:
        "approved", "rejected" veya "not_found" durumunu ve gerekçesini içerir.
    """
    await _latency()
    oid = _normalize_id(order_id)
    order = ORDERS.get(oid)
    if not order:
        logger.info("approve_refund(%s) -> sipariş yok", oid)
        return {
            "status": "not_found", "retryable": False, "order_id": oid,
            "message": f"{oid} numaralı sipariş yok. Kullanıcıdan numarayı teyit etmesini iste.",
        }
    if order["iade_durumu"]:
        return {
            "status": "already_refunded", "retryable": False, "order_id": oid,
            "refund_id": order["iade_durumu"],
            "message": f"{oid} için iade zaten onaylanmış ({order['iade_durumu']}). Tekrar işlem yapma.",
        }
    if order["durum"] != "teslim edildi":
        return {
            "status": "rejected", "retryable": False, "order_id": oid,
            "reason_code": "not_delivered",
            "message": (
                f"Sipariş henüz teslim edilmedi (durum: {order['durum']}). İade teslimattan sonra "
                "yapılabilir; kullanıcıya siparişi iptal etme seçeneğini sun."
            ),
        }
    delivered = date.fromisoformat(order["teslim_tarihi"])
    days = (_today - delivered).days
    if days > RETURN_WINDOW_DAYS:
        return {
            "status": "rejected", "retryable": False, "order_id": oid,
            "reason_code": "return_window_expired", "days_since_delivery": days,
            "message": (
                f"Teslimattan bu yana {days} gün geçmiş; iade süresi {RETURN_WINDOW_DAYS} gün. "
                "İade yapılamaz; kullanıcıya garanti/servis seçeneğini öner."
            ),
        }
    refund_id = f"IADE-{random.randint(10000, 99999)}"
    order["iade_durumu"] = refund_id
    logger.info("approve_refund(%s, %r) -> onaylandı %s", oid, reason, refund_id)
    return {
        "status": "approved", "retryable": False, "order_id": oid, "refund_id": refund_id,
        "amount_tl": order["tutar_tl"], "reason": reason,
        "message": (
            f"İade onaylandı. İade numarası {refund_id}; {order['tutar_tl']:.2f} TL "
            "3-5 iş günü içinde karta yansır. Kargo kodu SMS ile gönderilecek."
        ),
    }


async def freeze_card(card_last4: str, reason: str) -> dict:
    """Kaybolan veya çalınan bir kartı anında dondurur (geçici kullanım kapatma).

    Çağırmadan önce kullanıcıdan kartın son 4 hanesini ve nedenini teyit et.

    Args:
        card_last4: Kart numarasının son 4 hanesi, örneğin "4821".
        reason: Dondurma nedeni: "kayıp", "çalıntı", "şüpheli işlem" veya "diğer".

    Returns:
        "frozen", "already_frozen" veya "not_found" durumunu içerir.
    """
    await _latency()
    last4 = "".join(ch for ch in card_last4 if ch.isdigit())[-4:]
    card = CARDS.get(last4)
    if not card:
        logger.info("freeze_card(%s) -> kart yok", last4)
        return {
            "status": "not_found", "retryable": False, "card_last4": last4,
            "valid_options_hint": "Kart son 4 hanesi 4 rakamdan oluşmalı.",
            "message": f"Son 4 hanesi {last4} olan kart bulunamadı. Kullanıcıdan haneleri teyit etmesini iste.",
        }
    if card["durum"] == "dondurulmuş":
        return {
            "status": "already_frozen", "retryable": False, "card_last4": last4,
            "message": "Bu kart zaten dondurulmuş. Yeni kart talebi için şubeye veya uygulamaya yönlendir.",
        }
    card["durum"] = "dondurulmuş"
    ref = f"DON-{datetime.now():%H%M%S}"
    logger.info("freeze_card(%s, %r) -> donduruldu %s", last4, reason, ref)
    return {
        "status": "frozen", "retryable": False, "card_last4": last4, "card_type": card["tip"],
        "reference": ref, "reason": reason,
        "message": (
            f"{card['tip'].capitalize()} (son 4 hane {last4}) donduruldu. Referans {ref}. "
            "Yeni kart 3 iş günü içinde adrese gönderilir."
        ),
    }


# ---------------------------------------------------------------------------
# Restoran rezervasyonu
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    v = value.strip().lower()
    if v in ("bugün", "bugun", "today"):
        return _today
    if v in ("yarın", "yarin", "tomorrow"):
        return _today + timedelta(days=1)
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def _free_tables(day: date, slot: str) -> int:
    # Sabit ama tarihe göre değişen sahte doluluk + yapılan rezervasyonlar
    base = zlib.crc32(f"{day.isoformat()} {slot}".encode()) % 4  # süreçler arası sabit
    taken = sum(1 for r in RESERVATIONS if r["date"] == day.isoformat() and r["time"] == slot)
    return max(0, base - taken)


async def check_table_availability(reservation_date: str, party_size: int) -> dict:
    """Belirli bir gün için kişi sayısına uygun boş masa saatlerini listeler.

    Args:
        reservation_date: "YYYY-AA-GG" biçiminde tarih, ya da "bugün" / "yarın".
        party_size: Kişi sayısı (1-12).

    Returns:
        "ok" ise boş saatlerin listesini, aksi halde açıklamayı içerir.
    """
    await _latency()
    day = _parse_date(reservation_date)
    if day is None:
        return {"status": "invalid_argument", "retryable": False,
                "message": "Tarih anlaşılamadı. Kullanıcıdan gün ve ayı netleştirmesini iste."}
    if day < _today:
        return {"status": "invalid_argument", "retryable": False,
                "message": "Geçmiş bir tarih için rezervasyon yapılamaz."}
    if not 1 <= party_size <= MAX_PARTY:
        return {"status": "invalid_argument", "retryable": False,
                "message": f"Kişi sayısı 1 ile {MAX_PARTY} arasında olmalı; daha büyük gruplar için etkinlik ekibine yönlendir."}
    slots = [s for s in OPENING_SLOTS if _free_tables(day, s) > 0]
    logger.info("check_table_availability(%s, %d) -> %s", day, party_size, slots)
    if not slots:
        return {"status": "no_availability", "retryable": False, "date": day.isoformat(),
                "message": "Bu gün tamamen dolu. Aynı gün için tekrar sorgulama; kullanıcıya başka bir gün öner.",
                "suggested_next_step": "offer_another_day"}
    return {"status": "ok", "retryable": False, "date": day.isoformat(), "party_size": party_size,
            "available_times": slots,
            "message": f"{day.isoformat()} için boş saatler: {', '.join(slots)}."}


async def make_reservation(guest_name: str, reservation_date: str, time: str, party_size: int) -> dict:
    """Restoranda masa rezervasyonu oluşturur.

    Çağırmadan önce isim, tarih, saat ve kişi sayısını kullanıcıya özetleyip onay al.

    Args:
        guest_name: Rezervasyon sahibinin adı soyadı.
        reservation_date: "YYYY-AA-GG" biçiminde tarih, ya da "bugün" / "yarın".
        time: "SS:DD" biçiminde saat, örneğin "19:30".
        party_size: Kişi sayısı (1-12).

    Returns:
        "confirmed" ise rezervasyon numarasını, aksi halde nedenini içerir.
    """
    await _latency()
    day = _parse_date(reservation_date)
    if day is None or time not in OPENING_SLOTS:
        return {"status": "invalid_argument", "retryable": False,
                "valid_times": OPENING_SLOTS,
                "message": "Tarih veya saat geçersiz. Saatler yarım saatlik aralıklarla 18:00-21:30 arasıdır."}
    if _free_tables(day, time) <= 0:
        return {"status": "slot_taken", "retryable": False,
                "message": f"{time} dolu. Önce check_table_availability ile boş saatleri al ve kullanıcıya sun."}
    rid = f"RZ-{random.randint(1000, 9999)}"
    RESERVATIONS.append({"id": rid, "name": guest_name, "date": day.isoformat(), "time": time, "party_size": party_size})
    logger.info("make_reservation -> %s %s %s %s kişi", rid, day, time, party_size)
    return {"status": "confirmed", "retryable": False, "reservation_id": rid, "guest_name": guest_name,
            "date": day.isoformat(), "time": time, "party_size": party_size,
            "message": f"Rezervasyon onaylandı: {rid}, {day.isoformat()} {time}, {party_size} kişi."}


# ---------------------------------------------------------------------------
# Teknik destek
# ---------------------------------------------------------------------------


async def check_service_status(service: str) -> dict:
    """Şirket içi bir BT hizmetinin çalışma durumunu kontrol eder.

    Args:
        service: Hizmet adı: "e-posta", "vpn", "muhasebe", "wifi" veya "yazıcı".

    Returns:
        "ok" ise hizmet durumunu, "invalid_argument" ise geçerli seçenekleri içerir.
    """
    await _latency()
    key = service.strip().lower().replace("eposta", "e-posta").replace("mail", "e-posta")
    info = SERVICES.get(key)
    if not info:
        return {"status": "invalid_argument", "retryable": False, "valid_options": list(SERVICES),
                "message": f"'{service}' diye bir hizmet yok. Kullanıcıya hangisini kastettiğini sor."}
    logger.info("check_service_status(%s) -> %s", key, info["durum"])
    return {"status": "ok", "retryable": False, "service": key, "service_status": info["durum"],
            "note": info["not"], "message": f"{key}: {info['durum']}." + (f" {info['not']}" if info["not"] else "")}


async def create_support_ticket(summary: str, priority: str) -> dict:
    """Kullanıcının sorunu için BT destek kaydı (ticket) açar.

    Önce check_service_status ile bilinen bir kesinti olup olmadığına bak; bilinen
    kesintiler için kayıt açma. Kayıt açmadan önce özeti kullanıcıya teyit ettir.

    Args:
        summary: Sorunun bir iki cümlelik Türkçe özeti.
        priority: "düşük", "orta" veya "yüksek".

    Returns:
        "created" ise kayıt numarasını ve tahmini yanıt süresini içerir.
    """
    await _latency()
    prio = priority.strip().lower()
    sla = {"düşük": "2 iş günü", "orta": "8 saat", "yüksek": "1 saat"}
    if prio not in sla:
        return {"status": "invalid_argument", "retryable": False, "valid_options": list(sla),
                "message": "Öncelik 'düşük', 'orta' veya 'yüksek' olmalı."}
    tid = f"BT-{random.randint(20000, 29999)}"
    TICKETS.append({"id": tid, "summary": summary, "priority": prio})
    logger.info("create_support_ticket -> %s (%s)", tid, prio)
    return {"status": "created", "retryable": False, "ticket_id": tid, "priority": prio,
            "summary": summary, "expected_response": sla[prio],
            "message": f"Destek kaydı {tid} açıldı; tahmini yanıt süresi {sla[prio]}."}


# ---------------------------------------------------------------------------
# Derin analiz alt-ajanı: Flash + Google Search grounding
# ---------------------------------------------------------------------------

FLASH_TIMEOUT_S = 40
MAX_ANSWER_CHARS = 2500
MAX_SOURCES = 6

FLASH_SYSTEM_INSTRUCTION = (
    "Sen bir sesli asistanın arka plan araştırmacısısın. Cevabın, kullanıcıyla sesli "
    "konuşan asistana verilecek. Türkçe yaz. Önce 2-4 cümlelik net bir özet ver; gerekiyorsa "
    "ardından en fazla 5 maddelik önemli ayrıntılar ekle. Güncel bilgi için Google "
    "Aramasını kullan; kaynaklarda olmayan bilgiyi uydurma, emin değilsen açıkça belirt. "
    "Sayıları, tarihleri ve birimleri kesin yaz."
)

# Arayüze gidecek ama Live modeline gönderilmeyecek ek veri (kaynak linkleri,
# Google arama önerileri HTML'i). Anahtar: ADK function_call_id. main.py,
# function_response event'inde bunu alıp tool_result mesajına ekler.
_UI_EXTRAS: dict[str, dict] = {}
_UI_EXTRAS_MAX = 100


def pop_ui_extras(call_id: str | None) -> dict | None:
    return _UI_EXTRAS.pop(call_id, None) if call_id else None


def _put_ui_extras(call_id: str | None, extras: dict) -> None:
    if not call_id:
        return
    while len(_UI_EXTRAS) >= _UI_EXTRAS_MAX:  # alınmamış eski kayıtları at
        _UI_EXTRAS.pop(next(iter(_UI_EXTRAS)))
    _UI_EXTRAS[call_id] = extras


@functools.lru_cache(maxsize=1)
def _flash_client() -> genai.Client:
    """Flash için kendi bölgesiyle (FLASH_LOCATION) ayrı bir client.

    Live modeli LIVE_LOCATION ile ayrı bir client kullanır; ikisi de bölgeyi
    açıkça aldığı için GOOGLE_CLOUD_LOCATION ortam değişkenine bağımlı değiliz.
    """
    settings = config.load_settings()
    logger.info("Flash client: %s@%s", config.FLASH_MODEL, settings.flash_location)
    return genai.Client(**config.flash_client_kwargs(settings))


def _extract_sources(response: types.GenerateContentResponse) -> tuple[list[dict], list[str], str | None]:
    cand = (response.candidates or [None])[0]
    gm = cand.grounding_metadata if cand else None
    if not gm:
        return [], [], None
    sources, seen = [], set()
    for chunk in gm.grounding_chunks or []:
        web = chunk.web
        if not web or not web.uri or web.uri in seen:
            continue
        seen.add(web.uri)
        sources.append({"title": web.title or web.domain or "kaynak", "url": web.uri})
    entry = gm.search_entry_point.rendered_content if gm.search_entry_point else None
    return sources[:MAX_SOURCES], list(gm.web_search_queries or []), entry


async def analyze_with_flash(topic: str, context: str, tool_context: ToolContext) -> dict:
    """Güncel bilgi veya derin analiz gerektiren soruları Google Aramasıyla araştırır.

    Haberler, hava durumu, döviz/fiyatlar, spor sonuçları, yakın tarihli olaylar, bilmediğin
    ya da emin olmadığın olgular ve çok adımlı karşılaştırma/analiz gerektiren sorular için
    kullan. Araştırma birkaç saniye sürer; çağırırken kullanıcıya araştırdığını söyle.

    Args:
        topic: Araştırılacak soru, tek ve net bir cümle halinde (Türkçe).
        context: Konuşmadan ilgili bağlam: kullanıcının amacı, konum, tarih aralığı,
            daha önce söylenen kısıtlar. Bağlam yoksa boş metin gönder.

    Returns:
        "ok" ise özet cevabı ve kaynak başlıklarını; aksi halde hatayı açıklar.
    """
    started = time.monotonic()
    prompt = f"Soru: {topic.strip()}"
    if context and context.strip():
        prompt += f"\n\nKonuşma bağlamı: {context.strip()}"
    prompt += f"\n\nBugünün tarihi: {date.today().isoformat()}."
    logger.info("analyze_with_flash(%r) başladı", topic)
    try:
        response = await asyncio.wait_for(
            _flash_client().aio.models.generate_content(
                model=config.FLASH_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=FLASH_SYSTEM_INSTRUCTION,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            ),
            timeout=FLASH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("analyze_with_flash zaman aşımı (%ss)", FLASH_TIMEOUT_S)
        return {
            "status": "timeout", "retryable": False, "topic": topic,
            "message": "Araştırma zaman aşımına uğradı. Kullanıcıya şu an bilgiye ulaşamadığını söyle; tekrar deneme.",
        }
    except Exception as exc:  # noqa: BLE001 - modele anlaşılır hata dön
        logger.exception("analyze_with_flash hata")
        return {
            "status": "error", "retryable": False, "topic": topic,
            "message": f"Araştırma yapılamadı ({type(exc).__name__}). Kullanıcıya bunu söyle; tekrar deneme.",
        }

    answer = (response.text or "").strip()
    sources, queries, entry_html = _extract_sources(response)
    ms = round((time.monotonic() - started) * 1000)
    usage = response.usage_metadata
    logger.info(
        "analyze_with_flash bitti: %d ms, %d kaynak, sorgular=%s, token=%s",
        ms, len(sources), queries, usage.total_token_count if usage else "?",
    )
    if not answer:
        return {
            "status": "no_results", "retryable": False, "topic": topic,
            "message": "Araştırma bir sonuç üretmedi. Kullanıcıya bilgi bulunamadığını söyle ve soruyu farklı sormasını öner.",
        }

    _put_ui_extras(tool_context.function_call_id, {
        "kind": "research",
        "answer": answer[:MAX_ANSWER_CHARS],
        "sources": sources,
        "search_queries": queries,
        "search_entry_html": entry_html,  # Google Search grounding koşulu: gösterilmeli
        "model": config.FLASH_MODEL,
        "flash_ms": ms,
    })
    return {
        "status": "ok",
        "retryable": False,
        "topic": topic,
        "answer": answer[:MAX_ANSWER_CHARS],
        "source_titles": [s["title"] for s in sources],
        "grounded": bool(sources),
        "message": (
            "Cevabı kullanıcıya kısa ve doğal cümlelerle, kendi sözlerinle aktar; listeyi "
            "okuma. Uygunsa bilginin hangi kaynaklardan geldiğini bir kez an. Ayrıntılar "
            "ekrandaki kartta gösteriliyor."
        ),
    }


# ---------------------------------------------------------------------------
# ecem.dev asistanı: sayfada gezinme (istemci tarafında uygulanır)
# ---------------------------------------------------------------------------

PAGE_TARGETS = {
    "about": "Hakkında / giriş",
    "experience": "Kariyer",
    "projects": "Projeler",
    "skills": "Beceriler",
    "speaking": "Konuşmalar",
    "education": "Eğitim ve topluluk",
    "interests": "İlgi alanları",
    "contact": "İletişim",
    "case_in_vehicle_ser": "Vaka: Araç içi konuşma duygu tanıma",
    "case_call_center": "Vaka: Çağrı merkezi duygu analitiği ve canlı koçluk",
    "case_thesis": "Vaka: Tez — müzik duygu tanıma ve Emotion-to-Art",
}


async def show_on_page(target: str) -> dict:
    """Ziyaretçinin baktığı ecem.dev sayfasında ilgili bölüme kaydırır veya vaka çalışmasını açar.

    Bir konuyu anlatmaya başlarken (ör. projeler, kariyer, tez) ziyaretçinin ekranda da
    görmesi için çağır. Aynı hedef için art arda tekrar çağırma.

    Args:
        target: Şunlardan biri: "about", "experience", "projects", "skills", "speaking",
            "education", "interests", "contact", "case_in_vehicle_ser",
            "case_call_center", "case_thesis".

    Returns:
        "ok" ise gösterilen hedefi; "invalid_argument" ise geçerli seçenekleri içerir.
    """
    key = target.strip().lower()
    if key not in PAGE_TARGETS:
        return {"status": "invalid_argument", "retryable": False, "valid_options": list(PAGE_TARGETS),
                "message": "Geçersiz hedef; listedeki bir değeri kullan ya da aracı çağırmadan anlatmaya devam et."}
    logger.info("show_on_page(%s)", key)
    return {"status": "ok", "retryable": False, "target": key, "label": PAGE_TARGETS[key],
            "message": "Ziyaretçinin ekranında gösterildi. Bunu ayrıca söylemene gerek yok; anlatmaya devam et."}
