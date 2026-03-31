"""ntfy push notification provider — no Home Assistant dependency."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import NotificationPayload, PushProvider, SendResult

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

PRIORITY_MAP = {
    "low": "2",
    "normal": "3",
    "high": "5",
}


class NtfyProvider(PushProvider):
    """ntfy.sh push notification provider."""

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        server_url: str | None = None,
        topic: str | None = None,
        token: str | None = None,
    ) -> None:
        super().__init__(session)
        self._server_url = (server_url or "https://ntfy.sh").rstrip("/")
        self._topic = topic
        self._token = token

    async def async_initialize(self) -> bool:
        if not self._topic:
            _LOGGER.error("ntfy topic not configured")
            return False

        try:
            async with self._session.get(
                f"{self._server_url}/v1/health",
                timeout=10,
            ) as response:
                if response.status != 200:
                    _LOGGER.warning("ntfy health check returned %d, proceeding anyway", response.status)
        except Exception as e:
            _LOGGER.warning("Could not verify ntfy server, proceeding anyway: %s", e)

        self._initialized = True
        _LOGGER.info("ntfy provider initialized: %s/%s", self._server_url, self._topic)
        return True

    async def async_send(self, device_token: str, payload: NotificationPayload) -> SendResult:
        if not self._initialized:
            return SendResult(success=False, device_id=device_token, error="ntfy provider not initialized")

        try:
            headers: dict[str, str] = {
                "Title": payload.title,
                "Priority": PRIORITY_MAP.get(payload.priority, "3"),
                "Tags": self._build_tags(payload),
            }

            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"

            if payload.event_id:
                headers["Click"] = f"frigate-mobile://event/{payload.event_id}"
                actions = []
                if payload.camera:
                    actions.append(f"view, View Live, frigate-mobile://camera/{payload.camera}")
                if payload.event_id:
                    actions.append(f"http, View Event, frigate-mobile://event/{payload.event_id}")
                if actions:
                    headers["Actions"] = "; ".join(actions)

            if payload.image_url:
                headers["Attach"] = payload.image_url

            topic = device_token if device_token else self._topic
            url = f"{self._server_url}/{topic}"

            async with self._session.post(
                url,
                data=payload.body,
                headers=headers,
                timeout=30,
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return SendResult(
                        success=True,
                        device_id=device_token,
                        message_id=result.get("id"),
                    )
                error_text = await response.text()
                _LOGGER.error("ntfy send failed (%d): %s", response.status, error_text)
                return SendResult(
                    success=False,
                    device_id=device_token,
                    error=f"HTTP {response.status}: {error_text}",
                )

        except Exception as e:
            _LOGGER.exception("ntfy send failed: %s", e)
            return SendResult(success=False, device_id=device_token, error=str(e))

    def _build_tags(self, payload: NotificationPayload) -> str:
        tags = []
        label_emojis = {
            "person": "walking", "car": "car", "dog": "dog", "cat": "cat",
            "bird": "bird", "bicycle": "bike", "motorcycle": "motorcycle",
            "truck": "truck", "boat": "sailboat", "airplane": "airplane",
        }
        if payload.label:
            tags.append(label_emojis.get(payload.label.lower(), "eyes"))
        tags.append("camera")
        if payload.priority == "high":
            tags.append("warning")
        return ",".join(tags)

    async def async_close(self) -> None:
        self._initialized = False

    def get_sender_id(self) -> str | None:
        return self._topic


class NtfyDeviceProvider(NtfyProvider):
    """ntfy provider that uses per-device topics."""

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        server_url: str | None = None,
        base_topic: str | None = None,
        token: str | None = None,
    ) -> None:
        super().__init__(session, server_url, base_topic, token)
        self._base_topic = base_topic

    def get_device_topic(self, device_id: str) -> str:
        return f"{self._base_topic}_{device_id}"

    async def async_send(self, device_token: str, payload: NotificationPayload) -> SendResult:
        device_topic = self.get_device_topic(device_token)
        return await super().async_send(device_topic, payload)
