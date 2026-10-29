#!/bin/bash
# Run Codex against /checkout (read-only mount) with policy from /opt/opr-policy.
# Expects: CODEX_API_KEY (and OPENAI_API_KEY), OPR_MODEL, optional OPR_* tunables.
# Writes JSON result to /work/out.json. Fail closed if Linux sandbox cannot start.
set -euo pipefail

CHECKOUT="${OPR_CHECKOUT_DIR:-/checkout}"
WORK="${OPR_WORK_DIR:-/work}"
SCHEMA="${OPR_SCHEMA_PATH:-/job/schema.json}"
PROMPT_FILE="${OPR_PROMPT_PATH:-/job/prompt.txt}"
OUT="${OPR_OUT_PATH:-/work/out.json}"
MODEL="${OPR_MODEL:?OPR_MODEL is required}"
TIMEOUT="${OPR_TIMEOUT_SECONDS:-600}"
EFFORT="${OPR_REASONING_EFFORT:-medium}"
SANDBOX="${OPR_SANDBOX:-read-only}"
APPROVAL="${OPR_APPROVAL_POLICY:-never}"
ALLOW_UNSANDBOXED="${OPR_ALLOW_UNSANDBOXED:-0}"

mkdir -p "$WORK"
CODEX_HOME="$WORK/codex-home"
rm -rf "$CODEX_HOME"
mkdir -p "$CODEX_HOME/rules"
cp /opt/opr-policy/config.toml "$CODEX_HOME/config.toml"
cp /opt/opr-policy/rules/review.rules "$CODEX_HOME/rules/review.rules"
export CODEX_HOME
export HOME="${HOME:-/home/review}"

if [[ -z "${CODEX_API_KEY:-}" && -n "${OPENAI_API_KEY:-}" ]]; then
  export CODEX_API_KEY="$OPENAI_API_KEY"
fi
if [[ -z "${OPENAI_API_KEY:-}" && -n "${CODEX_API_KEY:-}" ]]; then
  export OPENAI_API_KEY="$CODEX_API_KEY"
fi
if [[ -z "${CODEX_API_KEY:-}" ]]; then
  echo "opr-review: CODEX_API_KEY/OPENAI_API_KEY missing" >&2
  exit 41
fi

BINARY="$(command -v codex || true)"
if [[ -z "$BINARY" ]]; then
  echo "opr-review: codex CLI not installed" >&2
  exit 42
fi

set +e
PROBE_OUT="$("$BINARY" sandbox -c "sandbox_mode=\"$SANDBOX\"" -- /bin/true 2>&1)"
PROBE_RC=$?
set -e
if [[ "$PROBE_RC" -ne 0 ]]; then
  echo "opr-review: linux sandbox probe failed (rc=$PROBE_RC): $PROBE_OUT" >&2
  if [[ "$ALLOW_UNSANDBOXED" != "1" && "$ALLOW_UNSANDBOXED" != "true" ]]; then
    exit 43
  fi
  echo "opr-review: continuing because OPR_ALLOW_UNSANDBOXED is set" >&2
fi

CMD=(
  "$BINARY"
  --ask-for-approval "$APPROVAL"
  exec
  --sandbox "$SANDBOX"
  --ephemeral
  --output-schema "$SCHEMA"
  -o "$OUT"
  -m "$MODEL"
  -c "approval_policy=\"$APPROVAL\""
  -c 'shell_environment_policy.inherit="core"'
  -c "shell_environment_policy.ignore_default_excludes=false"
)
if [[ -n "$EFFORT" ]]; then
  CMD+=(-c "model_reasoning_effort=\"$EFFORT\"")
fi
CMD+=(-)

cd "$CHECKOUT"
export LANG="${LANG:-C.UTF-8}"
export TERM="${TERM:-dumb}"

set +e
timeout --signal=KILL "$TIMEOUT" "${CMD[@]}" <"$PROMPT_FILE"
RC=$?
set -e
if [[ "$RC" -eq 124 || "$RC" -eq 137 ]]; then
  echo "opr-review: timed out after ${TIMEOUT}s" >&2
  exit 44
fi
if [[ "$RC" -ne 0 ]]; then
  echo "opr-review: codex exit $RC" >&2
  exit "$RC"
fi
if [[ ! -f "$OUT" ]]; then
  echo "opr-review: missing output file $OUT" >&2
  exit 45
fi
exit 0
