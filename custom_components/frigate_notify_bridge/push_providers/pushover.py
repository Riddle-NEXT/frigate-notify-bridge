"""Pushover push notification provider."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import PushProvider, NotificationPayload, SendResult

_LOGGER = logging.getLogger(__name__)

PUSHOVER_API_URL = "https://api.pushover.net/1/messages.json"

# Pushover priority mapping
# -2: no notification, -1: quiet, 0: normal, 1: high, 2: emergency
PRIORITY_MAP = {
    "low": -1,
    "normal": 0,
    "high": 1,
}


class PushoverProvider(PushProvider):
    """Pushover push notification provider."""

    def __init__(
        self,
        hass: HomeAssistant,
        user_key: str,
        api_token: str,
    ) -> None:
        """Initialize Pushover provider.

        Args:
            hass: Home Assistant instance
            user_key: Pushover user key
            api_token: Pushover API token (application token)
        """
        super().__init__(hass)
        self._user_key = user_key
        self._api_token = api_token

    async def async_initialize(self) -> bool:
        """Initialize and validate Pushover credentials."""
        if not self._user_key or not self._api_token:
            _LOGGER.error("Pushover credentials not configured")
            return False

        # Validate credentials with Pushover API
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
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
                    else:
                        _LOGGER.error(
                            "Pushover validation failed: %s",
                            result.get("errors", ["Unknown error"]),
                        )
                        return False
                else:
                    _LOGGER.error(
                        "Pushover validation request failed: %d",
                        response.status,
                    )
                    return False

        except Exception as e:
            _LOGGER.exception("Failed to validate Pushover credentials: %s", e)
            return False

    async def async_send(
        self,
        device_token: str,
        payload: NotificationPayload,
    ) -> SendResult:
        """Send notification via Pushover.

        Note: Pushover doesn't use device tokens in the same way.
        The device_token parameter here can be used as a device name
        to target specific devices registered with the user's Pushover account.
        """
        if not self._initialized:
            return SendResult(
                success=False,
                device_id=device_token,
                error="Pushover provider not initialized",
            )

        try:
            session = async_get_clientsession(self.hass)

            # Build request data
            data: dict[str, Any] = {
                "token": self._api_token,
                "user": self._user_key,
                "title": payload.title,
                "message": payload.body,
                "priority": PRIORITY_MAP.get(payload.priority, 0),
                "sound": payload.sound or "pushover",
            }

            # Target specific device if specified
            if device_token and device_token != "all":
                data["device"] = device_token

            # Add image attachment
            if payload.image_url:
                data["url"] = payload.image_url
                data["url_title"] = "View Image"

            # Add supplementary URL for event
            if payload.event_id:
                # Pushover supports a supplementary URL
                data["url"] = f"frigate-mobile://event/{payload.event_id}"
                data["url_title"] = "View Event"

            # Add HTML formatting
            data["html"] = 1

            # Format message with Frigate details
            message_parts = [payload.body]
            if payload.camera:
                message_parts.append(f"<b>Camera:</b> {payload.camera}")
            if payload.label:
                message_parts.append(f"<b>Detected:</b> {payload.label}")
            if payload.zones:
                message_parts.append(f"<b>Zones:</b> {', '.join(payload.zones)}")

            data["message"] = "\n".join(message_parts)

            async with session.post(
                PUSHOVER_API_URL,
                data=data,
                timeout=30,
            ) as response:
                result = await response.json()

                if response.status == 200 and result.get("status") == 1:
                    request_id = result.get("request")
                    _LOGGER.debug("Pushover message sent: %s", request_id)

                    return SendResult(
                        success=True,
                        device_id=device_token,
                        message_id=request_id,
                    )
                else:
                    errors = result.get("errors", ["Unknown error"])
                    error_msg = ", ".join(errors)
                    _LOGGER.error("Pushover send failed: %s", error_msg)

                    return SendResult(
                        success=False,
                        device_id=device_token,
                        error=error_msg,
                    )

        except Exception as e:
            _LOGGER.exception("Pushover send failed: %s", e)
            return SendResult(
                success=False,
                device_id=device_token,
                error=str(e),
            )

    async def async_send_to_many(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        """Send to multiple Pushover devices.

        Pushover can send to all devices at once, or target specific ones.
        If device_tokens contains "all" or is empty, sends to all devices.
        """
        if not device_tokens or device_tokens == ["all"]:
            # Send to all devices (no device parameter)
            result = await self.async_send("all", payload)
            return [result]

        # Send to each specific device
        return await super().async_send_to_many(device_tokens, payload)

    async def async_close(self) -> None:
        """Clean up Pushover provider."""
        self._initialized = False

    def get_sender_id(self) -> str | None:
        """Pushover doesn't use sender IDs."""
        return None
