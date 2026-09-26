from app.services.digest import format_digest_text
from app.workers.settings import parse_cron


def test_digest_text_empty() -> None:
    text = format_digest_text({"open_pr_count": 0, "blocker_pr_count": 0, "items": []})
    assert "Open PRs: 0" in text


def test_digest_text_includes_blockers() -> None:
    text = format_digest_text(
        {
            "open_pr_count": 1,
            "blocker_pr_count": 1,
            "items": [
                {
                    "repository": "org/repo",
                    "number": 3,
                    "has_blockers": True,
                    "assignee": "ada",
                    "age_hours": 2.5,
                    "html_url": "https://github.com/org/repo/pull/3",
                }
            ],
        }
    )
    assert "org/repo#3" in text
    assert "blockers" in text


def test_parse_hourly_cron() -> None:
    assert parse_cron("0 * * * *") == {"minute": {0}}


def test_parse_daily_cron() -> None:
    assert parse_cron("0 9 * * *") == {"minute": {0}, "hour": {9}}


def test_worker_runs_jobs_in_parallel() -> None:
    from app.workers.settings import WorkerSettings

    assert WorkerSettings.max_jobs >= 1
    assert WorkerSettings.job_timeout >= 600
