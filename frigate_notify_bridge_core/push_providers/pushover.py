"""Pushover push notification provider — no Home Assistant dependency."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import NotificationPayload, PushProvider, SendResult

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

_PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

PRIORITY_MAP = {
    "low": -1,
    "normal": 0,
    "high": 1,
}


class PushoverProvider(PushProvider):
    """Pushover push notification provider."""

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        user_key: str,
        api_token: str,
    ) -> None:
        super().__init__(session)
        self._user_key = user_key
        self._api_token = api_token

    async def async_initialize(self) -> bool:
        if not self._user_key or not self._api_token:
            _LOGGER.error("Pushover credentials not configured")
            return False

        try:
            async with self._session.post(
                "https://api.pushover.net/1/users/validate.json",
                data={
                    "token": self._api_token,
                    "user": self._user_key,
                },
                timeout=10,
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("status") == 1:
                        self._initialized = True
                        _LOGGER.info("Pushover provider initialized")
                        return True
                    _LOGGER.error("Pushover validation failed: %s", result.get("errors", ["Unknown error"]))
                    return False
                _LOGGER.error("Pushover validation request failed: %d", response.status)
                return False
        except Exception as e:
            _LOGGER.exception("Failed to validate Pushover credentials: %s", e)
            return False

    async def async_send(self, device_token: str, payload: NotificationPayload) -> SendResult:
        if not self._initialized:
            return SendResult(success=False, device_id=device_token, error="Pushover provider not initialized")

        try:
            data: dict[str, Any] = {
                "token": self._api_token,
                "user": self._user_key,
                "title": payload.title,
                "message": payload.body,
                "priority": PRIORITY_MAP.get(payload.priority, 0),
                "sound": payload.sound or "pushover",
                "html": 1,
            }

            if device_token and device_token != "all":
                data["device"] = device_token

            if payload.event_id:
                data["url"] = f"frigate-mobile://event/{payload.event_id}"
                data["url_title"] = "View Event"
            elif payload.image_url:
                data["url"] = payload.image_url
                data["url_title"] = "View Image"

            message_parts = [payload.body]
            if payload.camera:
                message_parts.append(f"<b>Camera:</b> {payload.camera}")
            if payload.label:
                message_parts.append(f"<b>Detected:</b> {payload.label}")
            if payload.zones:
                message_parts.append(f"<b>Zones:</b> {', '.join(payload.zones)}")
            data["message"] = "\n".join(message_parts)

            async with self._session.post(_PUSHOVER_API_URL, data=data, timeout=30) as response:
                result = await response.json()
                if response.status == 200 and result.get("status") == 1:
                    return SendResult(
                        success=True,
                        device_id=device_token,
                        message_id=result.get("request"),
                    )
                errors = result.get("errors", ["Unknown error"])
                error_msg = ", ".join(errors)
                _LOGGER.error("Pushover send failed: %s", error_msg)
                return SendResult(success=False, device_id=device_token, error=error_msg)

        except Exception as e:
            _LOGGER.exception("Pushover send failed: %s", e)
            return SendResult(success=False, device_id=device_token, error=str(e))

    async def async_send_to_many(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        if not device_tokens or device_tokens == ["all"]:
            result = await self.async_send("all", payload)
            return [result]
        return await super().async_send_to_many(device_tokens, payload)

    async def async_close(self) -> None:
        self._initialized = False

    def get_sender_id(self) -> str | None:
        return None
