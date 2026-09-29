#!/usr/bin/env bash
# Geliştirme sunucusunu başlatır.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "HATA: .env yok. 'cp .env.example .env' yapıp doldurun." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Sanal ortam oluşturuluyor (.venv)..."
  PY=""
  for c in python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
  [[ -n "$PY" ]] || { echo "HATA: Python 3.10-3.12 bulunamadı." >&2; exit 1; }
  "$PY" -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

set -a; source .env; set +a

PORT="${PORT:-8765}"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "HATA: $PORT portu başka bir uygulama tarafından kullanılıyor:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  echo ".env içinde PORT ve ALLOWED_ORIGINS değerlerini değiştirin." >&2
  exit 1
fi
echo "Arayüz: http://localhost:$PORT"
exec .venv/bin/uvicorn app.main:app \
  --host "${HOST:-127.0.0.1}" --port "$PORT" \
  --reload --reload-dir app \
  --log-level "$(echo "${LOG_LEVEL:-INFO}" | tr '[:upper:]' '[:lower:]')"
