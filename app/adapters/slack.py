"""Slack adapter. Disabled unless config.slack.enabled is true."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from app.config import AppConfig, get_app_config, get_settings

if TYPE_CHECKING:
    from app.owners.context import SlackBinding

logger = logging.getLogger(__name__)


class SlackError(RuntimeError):
    pass


class SlackClient:
    """Slack API client supporting both legacy config and owner bindings.

    Can be constructed in two ways:
    1. Legacy: SlackClient() or SlackClient(config) - uses global settings
    2. Owner context: SlackClient.from_binding(binding) - uses owner-specific Slack binding
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.settings = get_settings()
        self.config = config or get_app_config()
        self._enabled = self.config.slack.enabled
        self._channel = self.config.slack.channel
        self._token = self.settings.slack_bot_token

    @classmethod
    def from_binding(cls, binding: SlackBinding, config: AppConfig) -> SlackClient:
        """Create a client from an owner's Slack binding.

        Does NOT read global settings - uses only the binding values.
        """
        instance = cls.__new__(cls)
        instance.config = config
        instance._enabled = binding.enabled
        instance._channel = binding.channel
        instance._token = binding.bot_token
        return instance

    @classmethod
    def disabled(cls, config: AppConfig) -> SlackClient:
        """Create a disabled client that never reads settings or makes HTTP calls.

        Used when an owner context has no Slack binding - ensures we never
        accidentally inherit the global owner's Slack credentials.
        """
        instance = cls.__new__(cls)
        instance.config = config
        instance._enabled = False
        instance._channel = ""
        instance._token = ""
        return instance

    def enabled(self) -> bool:
        return bool(self._enabled and self._token and self._channel)

    async def post_message(self, text: str, channel: str | None = None) -> bool:
        if not self.enabled():
            logger.info("Slack disabled; message not sent")
            return False
        target = channel or self._channel
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"channel": target, "text": text, "mrkdwn": True},
            )
        data = response.json()
        if not data.get("ok"):
            raise SlackError(data.get("error") or f"HTTP {response.status_code}")
        return True
