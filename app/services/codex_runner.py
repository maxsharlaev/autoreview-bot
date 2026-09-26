from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
from pathlib import Path

from app.metrics import record_codex_attempt
from app.security.sandbox import sandbox_env
from app.services.constants import CODEX_AUTH, CODEX_QUOTA, CODEX_TIMEOUT, CODEX_UNAVAILABLE

logger = logging.getLogger(__name__)

_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")


class CodexRunnerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def classify_codex_error(message: str) -> str:
    text = message.lower()
    if any(
        needle in text
        for needle in (
            "no credits remaining",
            "insufficient_quota",
            "insufficient quota",
            "exceeded your current quota",
            "add credits",
        )
    ):
        return CODEX_QUOTA
    if any(
        needle in text
        for needle in (
            "missing bearer",
            "incorrect api key",
            "invalid_api_key",
            "invalid or expired api key",
            "you didn't provide an api key",
            "openai_api_key is empty",
        )
    ):
        return CODEX_AUTH
    return CODEX_UNAVAILABLE


def _redact_secrets(text: str) -> str:
    return _SECRET_RE.sub("sk-***", text)


def _error_snippet(text: str, max_len: int = 240) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        low = line.lower()
        if low.startswith("error:") or "no credits" in low or "401" in low:
            return _redact_secrets(line)[:max_len]
    if lines:
        return _redact_secrets(lines[-1])[:max_len]
    return "no output"


def build_codex_command(
    *,
    binary: str,
    schema_path: Path,
    output_path: Path,
    model: str,
    reasoning_effort: str = "medium",
    sandbox: str = "read-only",
    approval_policy: str = "never",
) -> list[str]:
    """Unattended review: sandbox blocks dangerous ops; `never` rejects prompts instead of hanging."""
    # `--ask-for-approval` is a global flag. `codex exec --ask-for-approval` is rejected
    # by current CLI; it must come before the subcommand. Exec also takes -c.
    cmd = [
        binary,
        "--ask-for-approval",
        approval_policy,
        "exec",
        "--sandbox",
        sandbox,
        "--ephemeral",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
        "-m",
        model,
        "-c",
        f'approval_policy="{approval_policy}"',
    ]
    effort = reasoning_effort.strip()
    if effort:
        cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
    cmd.append("-")
    return cmd


def _is_session_boundary(line: str) -> bool:
    low = line.lower()
    if low in {"exec", "codex", "tokens used", "user"}:
        return True
    return low.startswith(("warning:", "error:", "exited "))


class CodexLogFilter:
    """Codex writes the session, prompt echo, and tool output to stderr."""

    def __init__(self) -> None:
        self._mode = "normal"
        self._skip_count = 0
        self._exec_cmd_logged = False
        self._bwrap_path = False
        self._bwrap_userns = False

    def _flush_skip(self, what: str) -> None:
        if self._skip_count:
            logger.info("codex skipped %s (%s lines)", what, self._skip_count)
        self._skip_count = 0

    def emit(self, text: str) -> None:
        stripped = _redact_secrets(text.strip())
        if not stripped or stripped == "--------":
            return

        if self._mode == "prompt":
            if _is_session_boundary(stripped):
                self._flush_skip("prompt echo")
                self._mode = "normal"
                self.emit(stripped)
            else:
                self._skip_count += 1
            return

        if self._mode == "exec":
            if _is_session_boundary(stripped):
                self._flush_skip("exec output")
                self._mode = "normal"
                self.emit(stripped)
                return
            if not self._exec_cmd_logged:
                self._exec_cmd_logged = True
                logger.info("codex exec %s", stripped[:160])
                return
            self._skip_count += 1
            return

        if stripped == "user" or stripped.startswith("<untrusted_"):
            self._mode = "prompt"
            self._skip_count = 0 if stripped == "user" else 1
            return
        if stripped == "exec":
            self._mode = "exec"
            self._skip_count = 0
            self._exec_cmd_logged = False
            return

        low = stripped.lower()
        if "could not find bubblewrap on path" in low:
            if not self._bwrap_path:
                self._bwrap_path = True
                logger.info("codex using bundled bubblewrap (bwrap not on PATH)")
            return
        if "no permissions to create a new namespace" in low or "unprivileged_userns_clone" in low:
            if not self._bwrap_userns:
                self._bwrap_userns = True
                logger.warning(
                    "codex bubblewrap cannot create user namespaces in this container; "
                    "inner shell commands fail. The prompt diff is still used."
                )
            return
        if stripped == "codex":
            return
        if stripped.startswith("{") and len(stripped) > 80:
            logger.info("codex json payload (%s chars)", len(stripped))
            return
        if low.startswith("error:"):
            logger.warning("codex %s", stripped[:400])
            return
        if low.startswith(("warning:", "exited ")):
            logger.info("codex %s", stripped[:240])
            return
        if low == "tokens used" or stripped.replace(",", "").isdigit():
            logger.info("codex %s", stripped[:80])
            return
        if any(
            low.startswith(prefix)
            for prefix in (
                "openai codex ",
                "workdir:",
                "model:",
                "provider:",
                "approval:",
                "sandbox:",
                "reasoning effort:",
                "reasoning summaries:",
                "session id:",
            )
        ):
            logger.info("codex %s", stripped[:160])
            return


def _emit_codex_line(stream: str, text: str) -> None:
    """Back-compat for tests; production uses CodexLogFilter."""
    del stream
    CodexLogFilter().emit(text)


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    log_filter: CodexLogFilter,
    collected: list[str] | None = None,
) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        log_filter.emit(text)
        if collected is not None:
            collected.append(text)


async def run_codex(
    *,
    checkout: Path,
    prompt: str,
    schema_path: Path,
    output_path: Path,
    model: str,
    timeout_seconds: int,
    openai_api_key: str,
    reasoning_effort: str = "medium",
    sandbox: str = "read-only",
    approval_policy: str = "never",
) -> str:
    binary = shutil.which("codex")
    if not binary:
        record_codex_attempt(result=CODEX_UNAVAILABLE, duration_s=0)
        raise CodexRunnerError(CODEX_UNAVAILABLE, "codex CLI is not installed")
    if not openai_api_key.strip():
        record_codex_attempt(result=CODEX_AUTH, duration_s=0)
        raise CodexRunnerError(CODEX_AUTH, "OPENAI_API_KEY is empty")

    cmd = build_codex_command(
        binary=binary,
        schema_path=schema_path,
        output_path=output_path,
        model=model,
        reasoning_effort=reasoning_effort,
        sandbox=sandbox,
        approval_policy=approval_policy,
    )
    env = sandbox_env(openai_api_key)
    logger.info(
        "codex start model=%s effort=%s sandbox=%s timeout=%ss cwd=%s auth=CODEX_API_KEY",
        model,
        reasoning_effort,
        sandbox,
        timeout_seconds,
        checkout,
    )
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(checkout),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    started = time.monotonic()

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(20)
            logger.info("codex still running (%ss elapsed)", int(time.monotonic() - started))

    heartbeat = asyncio.create_task(_heartbeat())
    collected: list[str] = []
    log_filter = CodexLogFilter()
    try:
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        await asyncio.wait_for(
            asyncio.gather(
                _pump_stream(process.stdout, log_filter, collected),
                _pump_stream(process.stderr, log_filter, collected),
            ),
            timeout=timeout_seconds,
        )
        await asyncio.wait_for(process.wait(), timeout=30)
    except TimeoutError as exc:
        process.kill()
        record_codex_attempt(result=CODEX_TIMEOUT, duration_s=time.monotonic() - started)
        raise CodexRunnerError(CODEX_TIMEOUT, f"codex timed out after {timeout_seconds}s") from exc
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

    elapsed = int(time.monotonic() - started)
    if process.returncode != 0:
        blob = "".join(collected)[-8000:]
        code = classify_codex_error(blob)
        snippet = _error_snippet(blob)
        record_codex_attempt(result=code, duration_s=elapsed)
        raise CodexRunnerError(code, f"codex exit {process.returncode} after {elapsed}s: {snippet}")

    if not output_path.is_file():
        record_codex_attempt(result="AI_OUTPUT_INVALID", duration_s=elapsed)
        raise CodexRunnerError("AI_OUTPUT_INVALID", "codex did not write output file")
    record_codex_attempt(result="ok", duration_s=elapsed)
    logger.info("codex finished in %ss, output %s bytes", elapsed, output_path.stat().st_size)
    return output_path.read_text(encoding="utf-8")
