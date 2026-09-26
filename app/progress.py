"""INFO-level progress lines for docker compose / worker logs."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("autoreview_bot.review")


class ReviewProgress:
    def __init__(self, run_id: uuid.UUID, repository: str = "", number: int | None = None) -> None:
        self.run_id = str(run_id)
        self.repository = repository
        self.number = number

    def bind(self, repository: str, number: int) -> None:
        self.repository = repository
        self.number = number

    def _prefix(self) -> str:
        loc = f"{self.repository}#{self.number}" if self.repository and self.number is not None else "review"
        return f"[{self.run_id[:8]} {loc}]"

    def event(self, message: str) -> None:
        extra: dict[str, object] = {"review_run_id": self.run_id}
        if self.repository:
            extra["repository"] = self.repository
        if self.number is not None:
            extra["pr_number"] = self.number
        logger.info("%s %s", self._prefix(), message, extra=extra)
