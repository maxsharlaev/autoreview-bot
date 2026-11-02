#!/usr/bin/env bash
# Manage the autoreview-bot Compose stack (base + optional overlays).
# Usage:
#   ./scripts/stack.sh up
#   ./scripts/stack.sh --observability up
#   ./scripts/stack.sh up --observability
#   ./scripts/stack.sh --in-process up
#   ./scripts/stack.sh up --in-process
#   ./scripts/stack.sh compose config
#   ./scripts/stack.sh compose -- config
#   ./scripts/stack.sh compose up -d --scale worker=2
#   ./scripts/stack.sh compose -- up -d --scale worker=2
#   ./scripts/stack.sh status
# OPR_STACK_ROOT is a test hook so behavioral tests can isolate .env / docker.
# ARB36-008/009: first positional non-option is the command. Helper switches may
# appear before/after non-compose commands; after `compose` args are verbatim
# (one optional leading `--` is consumed, ARB36-004).
set -euo pipefail

if [[ -n "${OPR_STACK_ROOT:-}" ]]; then
  ROOT="$(cd "${OPR_STACK_ROOT}" && pwd)"
else
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
cd "$ROOT"

OBSERVABILITY=0
# M4: container-runner overlay on by default; --in-process opts out.
CONTAINER=1
EXPLICIT_CONTAINER=0
EXPLICIT_IN_PROCESS=0

usage() {
  cat <<'EOF'
Usage: stack.sh [options] <up|down|restart|logs|status|compose> [args...]
Helper options (before or after non-compose commands; before `compose` only):
  --observability   Include observability overlay.
  --container       Include container-runner overlay (default since M4; back-compat).
  --in-process      Omit container-runner overlay (in_process reviews only).
  compose           Passthrough to `docker compose` with the same -f files
                    (no token check / image build); remaining args are verbatim
                    (one optional leading `--` after `compose` is consumed).
EOF
}

is_helper_opt() {
  case "$1" in
    --observability|--container|--in-process|-h|--help) return 0 ;;
    *) return 1 ;;
  esac
}

apply_helper_opt() {
  case "$1" in
    --observability) OBSERVABILITY=1 ;;
    --container) CONTAINER=1; EXPLICIT_CONTAINER=1 ;;
    --in-process) CONTAINER=0; EXPLICIT_IN_PROCESS=1 ;;
    -h|--help) usage; exit 0 ;;
  esac
}

COMMAND=""
REST=()
seen_command=0
while [[ $# -gt 0 ]]; do
  arg="$1"
  shift
  if [[ "$seen_command" -eq 0 ]]; then
    if [[ "$arg" == "--" ]]; then
      continue
    fi
    if is_helper_opt "$arg"; then
      apply_helper_opt "$arg"
      continue
    fi
    if [[ "$arg" == -* ]]; then
      echo "Unknown option before command: $arg" >&2
      usage
      exit 2
    fi
    COMMAND="$arg"
    seen_command=1
    continue
  fi

  # After the command.
  if [[ "$COMMAND" == "compose" ]]; then
    # ARB36-004: optional leading `--` after `compose` is a helper separator only;
    # do not forward it to docker compose (matches stack.ps1 / documented forms).
    if [[ ${#REST[@]} -eq 0 && "$arg" == "--" ]]; then
      continue
    fi
    REST+=("$arg")
    continue
  fi
  if is_helper_opt "$arg"; then
    apply_helper_opt "$arg"
    continue
  fi
  REST+=("$arg")
done

if [[ -z "$COMMAND" ]]; then
  usage
  exit 2
fi

case "$COMMAND" in
  up|down|restart|logs|status|compose) ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    usage
    exit 2
    ;;
esac

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

Or opt out of the overlay: ./scripts/stack.sh --in-process ${COMMAND}
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

# ARB36-007: build review image via compose profile service (no host Python / docker build -f).
ensure_review_image() {
  echo "Building review-image (Dockerfile.review → \${OPR_REVIEW_IMAGE:-opr-review:local})..."
  docker compose "${compose_args[@]}" --profile build-review build review-image
}

# Credential check + review image build only for helper `up` (not compose up).
if [[ "$CONTAINER" -eq 1 && "$COMMAND" == "up" ]]; then
  assert_launcher_token
  ensure_review_image
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
  compose)
    docker compose "${compose_args[@]}" "${REST[@]}"
    ;;
esac