#!/bin/sh
# Launch the FastAPI process. Proxy headers stay off unless explicitly enabled.
set -eu
EXTRA=""
case "${UVICORN_PROXY_HEADERS:-0}" in
  1|true|TRUE|yes|YES)
    EXTRA="$EXTRA --proxy-headers"
    # Empty allow-list would trust all; require an explicit allow list when enabling.
    if [ -n "${UVICORN_FORWARDED_ALLOW_IPS:-}" ]; then
      EXTRA="$EXTRA --forwarded-allow-ips=${UVICORN_FORWARDED_ALLOW_IPS}"
    else
      echo "UVICORN_PROXY_HEADERS is set but UVICORN_FORWARDED_ALLOW_IPS is empty" >&2
      echo "Set UVICORN_FORWARDED_ALLOW_IPS to your reverse-proxy address(es)." >&2
      exit 1
    fi
    ;;
esac
# shellcheck disable=SC2086
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 $EXTRA
