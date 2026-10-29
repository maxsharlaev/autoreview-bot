"""Active lifecycle jobs are not reaped even when container orphan TTL is low."""

from __future__ import annotations

from typing import Any

from app.launcher.job_lifecycle import JobLifecycleRegistry
from app.launcher.reaper import reap_once
from app.launcher.spec import REVIEW_LABEL, REVIEW_LABEL_VALUE


def test_reaper_keeps_lifecycle_active_container(monkeypatch) -> None:  # noqa: ANN001
    reg = JobLifecycleRegistry()
    monkeypatch.setattr("app.launcher.reaper.lifecycle", reg)
    monkeypatch.setenv("OPR_REVIEW_ORPHAN_TTL_SECONDS", "60")
    jid = "a" * 32
    reg.accept(jid)
    reg.mark_creating(jid)
    reg.mark_running(jid, "cid-active")

    removed: list[str] = []

    class FakeDocker:
        def list_review_containers(self) -> list[dict[str, Any]]:
            return [
                {
                    "Id": "cid-active",
                    "Created": 0,  # ancient
                    "Names": ["/ai-review-a"],
                    "Labels": {
                        REVIEW_LABEL: REVIEW_LABEL_VALUE,
                        "opr.review": "1",
                        "opr.job_id": jid,
                    },
                }
            ]

        def kill_container(self, cid: str) -> None:
            removed.append(f"kill:{cid}")

        def remove_container(self, cid: str, *, force: bool = True) -> None:
            removed.append(f"remove:{cid}")

        def close(self) -> None:
            return None

    assert reap_once(client=FakeDocker()) == 0  # type: ignore[arg-type]
    assert removed == []
