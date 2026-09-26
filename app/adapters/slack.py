"""Slack adapter. Disabled unless config.slack.enabled is true."""

from __future__ import annotations

import logging

import httpx

from app.config import AppConfig, get_app_config, get_settings

logger = logging.getLogger(__name__)


class SlackError(RuntimeError):
    pass


class SlackClient:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.settings = get_settings()
        self.config = config or get_app_config()

    def enabled(self) -> bool:
        return bool(self.config.slack.enabled and self.settings.slack_bot_token and self.config.slack.channel)

    async def post_message(self, text: str, channel: str | None = None) -> bool:
        if not self.enabled():
            logger.info("Slack disabled; message not sent")
            return False
        target = channel or self.config.slack.channel
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.settings.slack_bot_token}"},
                json={"channel": target, "text": text, "mrkdwn": True},
            )
        data = response.json()
        if not data.get("ok"):
            raise SlackError(data.get("error") or f"HTTP {response.status_code}")
        return True
