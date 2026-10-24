"""Review phases on the progress status: one write per phase change, timestamped, within 140 chars."""

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.config import AppConfig
from app.services import progress_status
from app.services.progress_status import (
    PHASE_ANALYZING,
    PHASE_TEXT,
    final_outcome,
    phase_line,
    phase_outcome,
)
from app.services.review_time import analysis_record
from tests.test_progress_status import HEAD, Harness, _finding, _verified

STAMP = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\] "
SEQUENCE = [
    "Queued, waiting for a worker…",
    "Fetching pull request and diff…",
    "Reading repository context…",
    "Running model analysis…",
    "Publishing results…",
]


def _stamp(text: str) -> datetime:
    return datetime.strptime(text[1:24], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)


def test_phase_line_format() -> None:
    moment = datetime(2026, 10, 24, 21, 20, 5, tzinfo=UTC)
    assert phase_line(PHASE_ANALYZING, moment) == "[2026-10-24 21:20:05 UTC] Running model analysis…"


def test_phase_line_long_text_fits_and_keeps_timestamp(monkeypatch) -> None:
    monkeypatch.setitem(PHASE_TEXT, "long", "x" * 500)
    line = phase_line("long", datetime(2026, 10, 24, 21, 20, 5, tzinfo=UTC))
    assert len(line) == 140
    assert line.startswith("[2026-10-24 21:20:05 UTC] x")
    assert line.endswith("…")
    outcome = phase_outcome("long", datetime(2026, 10, 24, tzinfo=UTC))
    assert len(progress_status._one_line(outcome)) <= 140


@pytest.mark.asyncio
async def test_pat_phase_sequence_with_timestamps(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, kind="pat", verified=_verified([_finding("P2")]))
    await h.queue()
    result = await h.review()
    assert result.status == "completed"

    statuses = h.github.payloads("status")
    assert [item["state"] for item in statuses] == [*["pending"] * 5, "success"]
    descriptions = [item["description"] for item in statuses[:-1]]
    for text, expected in zip(descriptions, SEQUENCE, strict=True):
        assert re.fullmatch(STAMP + re.escape(expected), text), text
        assert len(text) <= 140
    stamps = [_stamp(text) for text in descriptions]
    assert stamps == sorted(stamps)
    assert all(len(item["description"]) <= 140 for item in statuses)
    assert "Reviewed" not in statuses[-1]["description"]
    assert all(call[1] == HEAD for call in h.github.calls if call[0] == "status")


@pytest.mark.asyncio
async def test_check_run_phase_summaries_and_final_time(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, kind="app", verified=_verified([_finding("P1")]))
    await h.queue()
    await h.review()
    writes = [call[-1] for call in h.github.calls if call[0] in {"create", "update"}]
    summaries = [item["output"]["summary"] for item in writes[:-1]]
    for text, expected in zip(summaries, SEQUENCE, strict=True):
        assert re.fullmatch(STAMP + re.escape(expected), text), text
    assert writes[0]["status"] == "queued"
    assert {item["status"] for item in writes[1:-1]} == {"in_progress"}
    assert "Reviewed commit" in writes[-1]["output"]["summary"]
    assert h.run.progress_status["phase"] == "publishing"


@pytest.mark.asyncio
async def test_repeated_phase_writes_nothing(monkeypatch, tmp_path) -> None:
    h = Harness(monkeypatch, tmp_path, kind="pat")
    reporter = progress_status.ProgressReporter(h.github, "commit_status", AppConfig())
    run = h.run
    await reporter.phase(run, "org/repo", PHASE_ANALYZING)
    await reporter.phase(run, "org/repo", PHASE_ANALYZING)
    assert len(h.github.payloads("status")) == 1
    assert run.progress_status["phase"] == PHASE_ANALYZING
    assert run.progress_status["phase_started_at"]


def test_final_commit_status_drops_time_and_fits_with_jira_task() -> None:
    finished = datetime(2026, 10, 24, 21, 20, 5, tzinfo=UTC)
    summary = {
        "finding_counts": {"P0": 1, "P1": 12, "P2": 3, "P3": 0},
        "task_alignment": "unclear",
        "analysis": analysis_record(HEAD, finished, 3 * 3600 * 1000 + 12_000),
    }
    outcome = final_outcome(
        status="completed",
        error_code=None,
        skip_reason=None,
        summary=summary,
        comment_url="https://example.test/c",
        issue_key="LONGPROJECT-123456",
        visibility="private",
        config=AppConfig(),
    )
    description = progress_status._one_line(outcome)
    assert description == (
        "Review published. Findings: P0=1 P1=12 P2=3 P3=0. Task: LONGPROJECT-123456 (alignment: unclear)"
    )
    assert len(description) <= 140
    assert "Reviewed" not in description
    # The check run summary keeps the full review time.
    assert "Reviewed commit" in outcome.summary and "(took 3h 00m 12s)" in outcome.summary


@pytest.mark.asyncio
async def test_failed_phase_update_does_not_break_review(monkeypatch, tmp_path) -> None:
    async def boom(self, full_name, sha, run_id, state, phase):
        raise RuntimeError("status write exploded")

    monkeypatch.setattr(progress_status.ProgressReporter, "phase_state", boom)
    h = Harness(monkeypatch, tmp_path, kind="pat", verified=_verified([_finding("P2")]))
    await h.queue()
    result = await h.review()
    assert result.status == "completed"
    h.publisher.publish.assert_awaited_once()
    assert h.github.payloads("status")[-1]["state"] == "success"


class _PhaseSession:
    """Isolated phase session fake: the first commit fails, later ones store the state."""

    def __init__(self, store: dict) -> None:
        self.store = store
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_update", False) or str(statement).lstrip().upper().startswith("UPDATE"):
            self.store["pending"] = statement.compile().params.get("progress_status")
        return SimpleNamespace(first=lambda: ("running", self.store.get("state")))

    async def commit(self):
        self.store["commits"] += 1
        if self.store["commits"] == 1:
            raise RuntimeError("database rejected the phase write")
        self.store["state"] = self.store.pop("pending", self.store.get("state"))

    async def rollback(self):
        self.store.pop("pending", None)


@pytest.mark.asyncio
async def test_failed_phase_commit_keeps_main_session_and_pending_changes(monkeypatch, tmp_path) -> None:
    from app.services import orchestrator

    h = Harness(monkeypatch, tmp_path, kind="pat", verified=_verified([_finding("P2")]))
    store: dict = {"commits": 0, "state": None}
    monkeypatch.setattr(orchestrator, "_isolated_factory", lambda session: lambda: _PhaseSession(store))
    await h.queue()
    store["state"] = dict(h.run.progress_status)
    main_commits_before = h.session.commit.await_count

    result = await h.review()

    assert result.status == "completed"
    h.publisher.publish.assert_awaited_once()
    assert h.run.comment_url is not None
    assert h.pr.issue_key == "ABC-12"  # set before the first phase, kept after the failed phase commit
    h.session.rollback.assert_not_awaited()
    assert store["commits"] == 3  # context (failed), analyzing, publishing
    assert store["state"]["phase"] == "publishing"
    # The main session is committed only by the review itself, never by the phase path.
    assert h.session.commit.await_count > main_commits_before
    assert h.github.payloads("status")[-1]["state"] == "success"


@pytest.mark.asyncio
async def test_phase_not_written_over_a_closed_status(monkeypatch, tmp_path) -> None:
    from app.services import orchestrator

    h = Harness(monkeypatch, tmp_path, kind="pat", verified=_verified([_finding("P2")]))
    store: dict = {"commits": 1, "state": {"state": "completed", "conclusion": "cancelled"}}
    monkeypatch.setattr(orchestrator, "_isolated_factory", lambda session: lambda: _PhaseSession(store))
    await h.queue()
    await h.review()
    texts = [item["description"] for item in h.github.payloads("status")]
    assert not any("Reading repository context" in t or "Running model analysis" in t for t in texts)
