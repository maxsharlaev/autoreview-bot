#!/bin/sh
# Keep LF line endings (see .gitattributes); required for Docker/Alpine images.
# Launch the FastAPI process.
# Default: --no-proxy-headers (do not trust X-Forwarded-* from the peer).
# Enable only with UVICORN_PROXY_HEADERS=1 and UVICORN_FORWARDED_ALLOW_IPS=<proxy>.
set -eu
EXTRA="--no-proxy-headers"
case "${UVICORN_PROXY_HEADERS:-0}" in
  1|true|TRUE|yes|YES)
    EXTRA="--proxy-headers"
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
