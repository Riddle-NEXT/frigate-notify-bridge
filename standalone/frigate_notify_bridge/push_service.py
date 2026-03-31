"""Push notification service for standalone mode.

Delegates to frigate_notify_bridge_core push providers.
Supports relay (with E2E encryption), FCM (JWT auth), ntfy, and Pushover.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .config import Config

# Core providers (no HA dependency).
# In Docker: frigate_notify_bridge_core/ sits alongside this package under /app.
# In development: add the repo root (frigate-notify-bridge/) to PYTHONPATH.
from frigate_notify_bridge_core.push_providers import (
    FCMProvider,
    NtfyProvider,
    PushoverProvider,
    RelayPushProvider,
    NotificationPayload,
    SendResult,
)

logger = logging.getLogger(__name__)


def _dict_to_payload(notification: dict[str, Any]) -> NotificationPayload:
    """Convert a notification dict to NotificationPayload."""
    data = notification.get("data", {})
    return NotificationPayload(
        title=notification.get("title", ""),
        body=notification.get("body", ""),
        data=data if data else None,
        image_url=notification.get("image_url"),
        thumbnail_url=notification.get("thumbnail_url"),
        priority=notification.get("priority", "high"),
        event_id=data.get("event_id") if isinstance(data, dict) else None,
        camera=data.get("camera") if isinstance(data, dict) else None,
        label=data.get("label") if isinstance(data, dict) else None,
        zones=(
            data.get("zones", "").split(",")
            if isinstance(data, dict) and data.get("zones")
            else None
        ),
    )


class PushService:
    """Push service wrapping core providers."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._provider_name = config.push_provider
        self._session: aiohttp.ClientSession | None = None
        self._provider: Any = None  # core PushProvider instance

    async def initialize(self) -> bool:
        """Initialize the push service."""
        self._session = aiohttp.ClientSession()

        if self._provider_name == "relay":
            return await self._init_relay()
        elif self._provider_name == "fcm":
            return await self._init_fcm()
        elif self._provider_name == "ntfy":
            return await self._init_ntfy()
        elif self._provider_name == "pushover":
            return await self._init_pushover()
        else:
            logger.error("Unknown push provider: %s", self._provider_name)
            return False

    async def _init_relay(self) -> bool:
        try:
            self._provider = RelayPushProvider(
                session=self._session,
                relay_url=self.config.relay_url,
                bridge_id=self.config.relay_bridge_id,
                bridge_secret=self.config.relay_bridge_secret,
                e2e_key=self.config.relay_e2e_key_bytes,
            )
            ok = await self._provider.async_initialize()
            if ok:
                logger.info("Relay provider initialized (bridge_id=%s)", self.config.relay_bridge_id)
            return ok
        except Exception as e:
            logger.exception("Failed to initialize relay provider: %s", e)
            return False

    async def _init_fcm(self) -> bool:
        try:
            credentials_json = json.dumps(self.config.fcm_credentials)
            self._provider = FCMProvider(
                session=self._session,
                credentials_json=credentials_json,
            )
            ok = await self._provider.async_initialize()
            if ok:
                logger.info("FCM provider initialized")
            return ok
        except Exception as e:
            logger.exception("Failed to initialize FCM provider: %s", e)
            return False

    async def _init_ntfy(self) -> bool:
        try:
            self._provider = NtfyProvider(
                session=self._session,
                server_url=self.config.ntfy_url,
                topic=self.config.ntfy_topic,
                token=self.config.ntfy_token or None,
            )
            ok = await self._provider.async_initialize()
            if ok:
                logger.info("ntfy provider initialized")
            return ok
        except Exception as e:
            logger.exception("Failed to initialize ntfy provider: %s", e)
            return False

    async def _init_pushover(self) -> bool:
        try:
            self._provider = PushoverProvider(
                session=self._session,
                user_key=self.config.pushover_user_key,
                api_token=self.config.pushover_api_token,
            )
            ok = await self._provider.async_initialize()
            if ok:
                logger.info("Pushover provider initialized")
            return ok
        except Exception as e:
            logger.exception("Failed to initialize Pushover provider: %s", e)
            return False

    async def send(
        self,
        device_token: str,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a notification to a single device."""
        if not self._provider:
            return {"success": False, "error": "Service not initialized"}

        payload = _dict_to_payload(notification)
        result: SendResult = await self._provider.async_send(device_token, payload)
        return {
            "success": result.success,
            "device_id": result.device_id,
            "message_id": result.message_id,
            "error": result.error,
        }

    async def send_to_many(
        self,
        device_tokens: list[str],
        notification: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Send notification to multiple devices."""
        if not self._provider:
            return [{"success": False, "error": "Service not initialized"}] * len(device_tokens)

        payload = _dict_to_payload(notification)
        results: list[SendResult] = await self._provider.async_send_to_many(device_tokens, payload)
        return [
            {
                "success": r.success,
                "device_id": r.device_id,
                "message_id": r.message_id,
                "error": r.error,
            }
            for r in results
        ]

    def get_sender_id(self) -> str | None:
        if self._provider:
            return self._provider.get_sender_id()
        return None

    async def close(self) -> None:
        if self._provider:
            await self._provider.async_close()
        if self._session:
            await self._session.close()
