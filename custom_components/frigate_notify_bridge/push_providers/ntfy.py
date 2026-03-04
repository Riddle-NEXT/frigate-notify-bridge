"""ntfy push notification provider."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import PushProvider, NotificationPayload, SendResult

_LOGGER = logging.getLogger(__name__)

# ntfy priority mapping
PRIORITY_MAP = {
    "low": "2",
    "normal": "3",
    "high": "5",
}


class NtfyProvider(PushProvider):
    """ntfy.sh push notification provider."""

    def __init__(
        self,
        hass: HomeAssistant,
        server_url: str | None = None,
        topic: str | None = None,
        token: str | None = None,
    ) -> None:
        """Initialize ntfy provider.

        Args:
            hass: Home Assistant instance
            server_url: ntfy server URL (default: https://ntfy.sh)
            topic: ntfy topic name
            token: Optional access token for authentication
        """
        super().__init__(hass)
        self._server_url = (server_url or "https://ntfy.sh").rstrip("/")
        self._topic = topic
        self._token = token

    async def async_initialize(self) -> bool:
        """Initialize the ntfy provider."""
        if not self._topic:
            _LOGGER.error("ntfy topic not configured")
            return False

        # Validate connection to ntfy server
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{self._server_url}/v1/health",
                timeout=10,
            ) as response:
                if response.status == 200:
                    self._initialized = True
                    _LOGGER.info(
                        "ntfy provider initialized: %s/%s",
                        self._server_url,
                        self._topic,
                    )
                    return True
                else:
                    _LOGGER.warning(
                        "ntfy health check returned %d, proceeding anyway",
                        response.status,
                    )
                    # Still initialize - server might not have health endpoint
                    self._initialized = True
                    return True

        except Exception as e:
            _LOGGER.warning(
                "Could not verify ntfy server, proceeding anyway: %s", e
            )
            # Initialize anyway - might be a self-hosted server
            self._initialized = True
            return True

    async def async_send(
        self,
        device_token: str,
        payload: NotificationPayload,
    ) -> SendResult:
        """Send notification via ntfy.

        Note: For ntfy, the device_token is actually the topic name.
        Each device subscribes to its own unique topic for targeted delivery,
        or all devices share the same topic for broadcast.
        """
        if not self._initialized:
            return SendResult(
                success=False,
                device_id=device_token,
                error="ntfy provider not initialized",
            )

        try:
            session = async_get_clientsession(self.hass)

            # Build headers
            headers: dict[str, str] = {
                "Title": payload.title,
                "Priority": PRIORITY_MAP.get(payload.priority, "3"),
                "Tags": self._build_tags(payload),
            }

            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            # Add action buttons for Frigate events
            if payload.event_id:
                # Add click action to open event in app
                headers["Click"] = f"frigate-mobile://event/{payload.event_id}"

                # Add action buttons
                actions = []
                if payload.camera:
                    actions.append(
                        f"view, View Live, frigate-mobile://camera/{payload.camera}"
                    )
                if payload.event_id:
                    actions.append(
                        f"http, View Event, frigate-mobile://event/{payload.event_id}"
                    )
                if actions:
                    headers["Actions"] = "; ".join(actions)

            # Add image if available
            if payload.image_url:
                headers["Attach"] = payload.image_url

            # Use device_token as topic for per-device delivery
            # or fall back to configured topic for broadcast
            topic = device_token if device_token else self._topic
            url = f"{self._server_url}/{topic}"

            async with session.post(
                url,
                data=payload.body,
                headers=headers,
                timeout=30,
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    message_id = result.get("id")

                    _LOGGER.debug("ntfy message sent: %s", message_id)

                    return SendResult(
                        success=True,
                        device_id=device_token,
                        message_id=message_id,
                    )
                else:
                    error_text = await response.text()
                    _LOGGER.error(
                        "ntfy send failed (%d): %s",
                        response.status,
                        error_text,
                    )
                    return SendResult(
                        success=False,
                        device_id=device_token,
                        error=f"HTTP {response.status}: {error_text}",
                    )

        except Exception as e:
            _LOGGER.exception("ntfy send failed: %s", e)
            return SendResult(
                success=False,
                device_id=device_token,
                error=str(e),
            )

    def _build_tags(self, payload: NotificationPayload) -> str:
        """Build ntfy tags from payload."""
        tags = []

        # Add emoji based on label
        label_emojis = {
            "person": "walking",
            "car": "car",
            "dog": "dog",
            "cat": "cat",
            "bird": "bird",
            "bicycle": "bike",
            "motorcycle": "motorcycle",
            "truck": "truck",
            "boat": "sailboat",
            "airplane": "airplane",
        }

        if payload.label:
            emoji = label_emojis.get(payload.label.lower(), "eyes")
            tags.append(emoji)

        # Add camera icon
        tags.append("camera")

        # Add priority indicator
        if payload.priority == "high":
            tags.append("warning")

        return ",".join(tags)

    async def async_close(self) -> None:
        """Clean up ntfy provider."""
        self._initialized = False

    def get_sender_id(self) -> str | None:
        """Get the ntfy topic as sender ID."""
        return self._topic


class NtfyDeviceProvider(NtfyProvider):
    """ntfy provider that uses per-device topics.

    Each device gets a unique topic for targeted notifications.
    The topic format is: {base_topic}_{device_id}
    """

    def __init__(
        self,
        hass: HomeAssistant,
        server_url: str | None = None,
        base_topic: str | None = None,
        token: str | None = None,
    ) -> None:
        """Initialize per-device ntfy provider."""
        super().__init__(hass, server_url, base_topic, token)
        self._base_topic = base_topic

    def get_device_topic(self, device_id: str) -> str:
        """Get the topic for a specific device."""
        return f"{self._base_topic}_{device_id}"

    async def async_send(
        self,
        device_token: str,
        payload: NotificationPayload,
    ) -> SendResult:
        """Send to device-specific topic."""
        # device_token here is the device ID, convert to topic
        device_topic = self.get_device_topic(device_token)
        return await super().async_send(device_topic, payload)
