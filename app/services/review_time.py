"""When a review was done: UTC timestamp and duration, shared by the PR comment and progress status."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

SHORT_SHA = 7


def format_utc(moment: datetime) -> str:
    """`YYYY-MM-DD HH:MM:SS UTC`; a naive datetime is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(duration_ms: int, language: str = "en") -> str:
    total = max(0, int(round(duration_ms / 1000)))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    h, m, s = ("ч", "мин", "с") if language == "ru" else ("h", "m", "s")
    sep = " " if language == "ru" else ""
    if hours:
        return f"{hours}{sep}{h} {minutes:02d}{sep}{m} {seconds:02d}{sep}{s}"
    if minutes:
        return f"{minutes}{sep}{m} {seconds}{sep}{s}"
    return f"{seconds}{sep}{s}"


def reviewed_text(head_sha: str, finished_at: datetime, duration_ms: int | None, language: str = "en") -> str:
    """`commit abc1234 at 2026-10-24 09:25:40 UTC (took 3m 12s)` (or the Russian equivalent)."""
    sha = head_sha[:SHORT_SHA]
    if language == "ru":
        text = f"коммит `{sha}` в {format_utc(finished_at)}"
        return text + (f" (заняло {format_duration(duration_ms, 'ru')})" if duration_ms is not None else "")
    text = f"commit `{sha}` at {format_utc(finished_at)}"
    return text + (f" (took {format_duration(duration_ms)})" if duration_ms is not None else "")


def analysis_record(head_sha: str, finished_at: datetime, duration_ms: int) -> dict[str, Any]:
    """Stored in `review_runs.summary["analysis"]` so the progress status shows the same values."""
    return {
        "head_sha": head_sha,
        "finished_at": finished_at.astimezone(UTC).isoformat(),
        "duration_ms": duration_ms,
    }


def status_line(summary: dict[str, Any] | None) -> str | None:
    """Plain-text line for the check run / commit status, or None when the run has no analysis record."""
    record = (summary or {}).get("analysis") or {}
    try:
        finished_at = datetime.fromisoformat(str(record["finished_at"]))
        head_sha = str(record["head_sha"])
    except (KeyError, ValueError):
        return None
    duration = record.get("duration_ms")
    text = reviewed_text(head_sha, finished_at, int(duration) if duration is not None else None).replace("`", "")
    return f"Reviewed {text}."
