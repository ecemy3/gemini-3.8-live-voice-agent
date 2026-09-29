"""Herkese açık yayın için erişim ve kötüye kullanım korumaları.

- Erişim kodu: DEMO_ACCESS_CODE tanımlıysa, `public` olmayan senaryolar (tam demo)
  yalnızca /api/unlock ile kod girilmiş tarayıcılara açılır. Kod HttpOnly çerezde
  hash olarak tutulur; URL'ye veya loglara düşmez. Tanımlı değilse (yerel geliştirme)
  her şey açıktır.
- Eşzamanlı oturum sınırı (MAX_CONCURRENT_SESSIONS) ve herkese açık senaryolar için IP
  başına günlük oturum sınırı (PUBLIC_DAILY_SESSIONS_PER_IP).
- Sayaçlar süreç belleğindedir: Cloud Run'da --max-instances=1 ile tek örnek
  çalıştırıldığında global sınır gibi davranır. Birden çok örnek için Redis/Firestore
  gibi paylaşılan bir depo gerekir.

Not: Bu korumalar maliyeti sınırlar ama gerçek kimlik doğrulama değildir; Origin
başlığı tarayıcı dışı istemcilerce taklit edilebilir. Bkz. README "Güvenlik notları".
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import Request, WebSocket

logger = logging.getLogger("security")

ACCESS_COOKIE = "demo_access"


@dataclass(frozen=True)
class SecurityConfig:
    demo_access_code: str
    require_origin: bool
    max_concurrent_sessions: int
    public_daily_sessions_per_ip: int


def load_security_config() -> SecurityConfig:
    return SecurityConfig(
        demo_access_code=os.getenv("DEMO_ACCESS_CODE", "").strip(),
        require_origin=os.getenv("REQUIRE_ORIGIN", "false").lower() in ("1", "true", "yes"),
        max_concurrent_sessions=int(os.getenv("MAX_CONCURRENT_SESSIONS", "10")),
        public_daily_sessions_per_ip=int(os.getenv("PUBLIC_DAILY_SESSIONS_PER_IP", "15")),
    )


def _code_digest(code: str) -> str:
    return hashlib.sha256(f"live-assistant:{code}".encode()).hexdigest()


def access_locked(cfg: SecurityConfig) -> bool:
    return bool(cfg.demo_access_code)


def check_code(cfg: SecurityConfig, code: str) -> str | None:
    """Kod doğruysa çereze yazılacak değeri döndürür."""
    if not cfg.demo_access_code:
        return None
    if hmac.compare_digest(code.strip(), cfg.demo_access_code):
        return _code_digest(cfg.demo_access_code)
    return None


def has_access(cfg: SecurityConfig, conn: Request | WebSocket) -> bool:
    if not cfg.demo_access_code:
        return True
    cookie = conn.cookies.get(ACCESS_COOKIE, "")
    return hmac.compare_digest(cookie, _code_digest(cfg.demo_access_code))


def client_ip(conn: Request | WebSocket) -> str:
    # Cloud Run / yük dengeleyici gerçek istemci IP'sini X-Forwarded-For'un başına koyar.
    fwd = conn.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return conn.client.host if conn.client else "?"


class SessionLimiter:
    def __init__(self) -> None:
        self.active = 0
        self._daily: dict[tuple[str, str], int] = defaultdict(int)

    def try_acquire(self, cfg: SecurityConfig, ip: str, public: bool) -> str | None:
        """Oturum açılabiliyorsa None, değilse kullanıcıya gösterilecek nedeni döndürür."""
        if self.active >= cfg.max_concurrent_sessions:
            return "Şu an çok fazla aktif oturum var, lütfen birazdan tekrar deneyin. / Too many active sessions, please try again shortly."
        if public:
            day = time.strftime("%Y-%m-%d")
            key = (day, ip)
            if self._daily[key] >= cfg.public_daily_sessions_per_ip:
                return "Bugünlük oturum sınırına ulaştınız. / Daily session limit reached."
            self._daily[key] += 1
            # Eski günlerin sayaçlarını temizle
            for k in [k for k in self._daily if k[0] != day]:
                del self._daily[k]
        self.active += 1
        return None

    def release(self) -> None:
        self.active = max(0, self.active - 1)


limiter = SessionLimiter()
