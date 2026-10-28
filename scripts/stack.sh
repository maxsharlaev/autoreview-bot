#!/usr/bin/env bash
# Manage the autoreview-bot Compose stack (base + optional overlays).
# Usage:
#   ./scripts/stack.sh up
#   ./scripts/stack.sh up --observability --container
#   ./scripts/stack.sh status
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMMAND=""
OBSERVABILITY=0
CONTAINER=0
REST=()

usage() {
  cat <<'EOF'
Usage: stack.sh <up|down|restart|logs|status> [--observability] [--container] [--] [compose-args...]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    up|down|restart|logs|status)
      COMMAND="$1"
      shift
      ;;
    --observability)
      OBSERVABILITY=1
      shift
      ;;
    --container)
      CONTAINER=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      REST+=("$@")
      break
      ;;
    *)
      REST+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$COMMAND" ]]; then
  usage
  exit 2
fi

dotenv_get() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
    return 0
  fi
  if [[ ! -f .env ]]; then
    return 0
  fi
  # shellcheck disable=SC2162
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    local key="${line%%=*}"
    local val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    if [[ "$key" == "$name" ]]; then
      val="${val%\"}"
      val="${val#\"}"
      val="${val%\'}"
      val="${val#\'}"
      printf '%s' "$val"
      return 0
    fi
  done < .env
}

assert_launcher_token() {
  local token
  token="$(dotenv_get LAUNCHER_TOKEN || true)"
  if [[ -z "$token" || ${#token} -lt 16 ]]; then
    cat <<'EOF'
LAUNCHER_TOKEN is missing or shorter than 16 characters (required for --container).

Generate one (do not commit it; put it in .env):

  PowerShell:
    -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })

  openssl:
    openssl rand -hex 32

Then set in .env:
  LAUNCHER_TOKEN=<paste value>
EOF
    exit 1
  fi
}

compose_args=(-f docker-compose.yml)
if [[ "$OBSERVABILITY" -eq 1 ]]; then
  compose_args+=(-f docker-compose.observability.yml)
fi
if [[ "$CONTAINER" -eq 1 ]]; then
  assert_launcher_token
  compose_args+=(-f docker-compose.container-runner.yml)
fi

case "$COMMAND" in
  up)
    docker compose "${compose_args[@]}" up -d --build "${REST[@]}"
    ;;
  down)
    docker compose "${compose_args[@]}" down "${REST[@]}"
    ;;
  restart)
    docker compose "${compose_args[@]}" restart "${REST[@]}"
    ;;
  logs)
    docker compose "${compose_args[@]}" logs -f --tail=200 "${REST[@]}"
    ;;
  status)
    docker compose "${compose_args[@]}" ps "${REST[@]}"
    ;;
esac
