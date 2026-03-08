"""Base class for push notification providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant


@dataclass
class NotificationPayload:
    """Notification payload to send to devices."""

    title: str
    body: str
    data: dict[str, Any] | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    priority: str = "high"  # low, normal, high
    sound: str | None = None
    badge: int | None = None

    # Frigate-specific data
    event_id: str | None = None
    camera: str | None = None
    label: str | None = None
    zones: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
        }
        if self.data:
            result["data"] = self.data
        if self.image_url:
            result["image_url"] = self.image_url
        if self.thumbnail_url:
            result["thumbnail_url"] = self.thumbnail_url
        if self.sound:
            result["sound"] = self.sound
        if self.badge is not None:
            result["badge"] = self.badge
        if self.event_id:
            result["event_id"] = self.event_id
        if self.camera:
            result["camera"] = self.camera
        if self.label:
            result["label"] = self.label
        if self.zones:
            result["zones"] = self.zones
        return result


@dataclass
class SendResult:
    """Result of sending a notification."""

    success: bool
    device_id: str
    message_id: str | None = None
    error: str | None = None


class PushProvider(ABC):
    """Abstract base class for push notification providers."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the provider."""
        self.hass = hass
        self._initialized = False
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        """Return the provider name."""
        return self.__class__.__name__

    @property
    def is_initialized(self) -> bool:
        """Return whether the provider is initialized."""
        return self._initialized

    @property
    def last_error(self) -> str | None:
        """Return the last provider error, if any."""
        return self._last_error

    def _set_error(self, message: str) -> None:
        """Record a provider error."""
        self._initialized = False
        self._last_error = message

    def _clear_error(self) -> None:
        """Clear the last provider error."""
        self._last_error = None

    @abstractmethod
    async def async_initialize(self) -> bool:
        """Initialize the provider.

        Returns:
            True if initialization succeeded, False otherwise.
        """
        pass

    @abstractmethod
    async def async_send(
        self,
        device_token: str,
        payload: NotificationPayload,
    ) -> SendResult:
        """Send a notification to a device.

        Args:
            device_token: The device's push token
            payload: The notification payload

        Returns:
            SendResult indicating success/failure
        """
        pass

    async def async_send_to_many(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        """Send a notification to multiple devices.

        Default implementation sends sequentially.
        Providers may override for batch sending.

        Args:
            device_tokens: List of device push tokens
            payload: The notification payload

        Returns:
            List of SendResults
        """
        results = []
        for token in device_tokens:
            result = await self.async_send(token, payload)
            results.append(result)
        return results

    @abstractmethod
    async def async_close(self) -> None:
        """Clean up provider resources."""
        pass

    def get_sender_id(self) -> str | None:
        """Get the sender ID for this provider (used in QR pairing).

        Returns:
            Sender ID string or None if not applicable
        """
        return None
