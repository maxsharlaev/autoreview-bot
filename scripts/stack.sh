#!/usr/bin/env bash
# Manage the autoreview-bot Compose stack (base + optional overlays).
# Usage:
#   ./scripts/stack.sh up
#   ./scripts/stack.sh up --observability
#   ./scripts/stack.sh up --in-process
#   ./scripts/stack.sh status
# OPR_STACK_ROOT is a test hook so behavioral tests can isolate .env / docker.
set -euo pipefail

if [[ -n "${OPR_STACK_ROOT:-}" ]]; then
  ROOT="$(cd "${OPR_STACK_ROOT}" && pwd)"
else
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
cd "$ROOT"

COMMAND=""
OBSERVABILITY=0
# M4: container-runner overlay on by default; --in-process opts out.
CONTAINER=1
EXPLICIT_CONTAINER=0
EXPLICIT_IN_PROCESS=0
REST=()

usage() {
  cat <<'EOF'
Usage: stack.sh <up|down|restart|logs|status> [--observability] [--container|--in-process] [--] [compose-args...]
  --container     Include container-runner overlay (default since M4; kept for back-compat).
  --in-process    Omit container-runner overlay (in_process reviews only).
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
      EXPLICIT_CONTAINER=1
      shift
      ;;
    --in-process)
      CONTAINER=0
      EXPLICIT_IN_PROCESS=1
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

if [[ "$EXPLICIT_CONTAINER" -eq 1 && "$EXPLICIT_IN_PROCESS" -eq 1 ]]; then
  echo "Use either --container (default) or --in-process, not both." >&2
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
    cat <<EOF
LAUNCHER_TOKEN is missing or shorter than 16 characters (required for stack up with the container-runner overlay; default since M4).

Generate one (do not commit it; put it in .env):

  PowerShell:
    -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })

  openssl:
    openssl rand -hex 32

Then set in .env:
  LAUNCHER_TOKEN=<paste value>

Or opt out of the overlay: ./scripts/stack.sh ${COMMAND} --in-process
(and set codex.runner_backend: in_process in config.yaml).
EOF
    exit 1
  fi
}

compose_args=(-f docker-compose.yml)
if [[ "$OBSERVABILITY" -eq 1 ]]; then
  compose_args+=(-f docker-compose.observability.yml)
fi
if [[ "$CONTAINER" -eq 1 ]]; then
  compose_args+=(-f docker-compose.container-runner.yml)
fi

# Credential check only for up (ARB36-001). Lifecycle commands still use the overlay.
if [[ "$CONTAINER" -eq 1 && "$COMMAND" == "up" ]]; then
  assert_launcher_token
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
