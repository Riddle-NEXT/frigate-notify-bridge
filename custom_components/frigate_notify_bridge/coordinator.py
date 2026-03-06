"""Coordinator for Frigate Notify Bridge."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_FRIGATE_URL,
    CONF_FRIGATE_USERNAME,
    CONF_FRIGATE_PASSWORD,
    DEFAULT_NOTIFICATION_TITLE,
)
from .push_providers.base import NotificationPayload, SendResult

if TYPE_CHECKING:
    from .device_manager import DeviceManager
    from .push_providers import PushProvider

_LOGGER = logging.getLogger(__name__)


def _device_target(device: dict[str, Any], use_relay: bool) -> str | None:
    """Return the identifier that the active push provider expects."""
    if use_relay:
        return device.get("relay_device_id") or device.get("id")
    return device.get("fcm_token")


class FrigateNotifyCoordinator:
    """Coordinate notifications between Frigate events and push providers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        push_provider: PushProvider,
        device_manager: DeviceManager,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry = entry
        self.push_provider = push_provider
        self.device_manager = device_manager
        self._frigate_url = entry.data.get(CONF_FRIGATE_URL)
        self._frigate_auth: tuple[str, str] | None = None

        # Set up Frigate auth if configured
        username = entry.data.get(CONF_FRIGATE_USERNAME)
        password = entry.data.get(CONF_FRIGATE_PASSWORD)
        if username and password:
            self._frigate_auth = (username, password)

    async def async_handle_event(self, event_data: dict[str, Any]) -> None:
        """Handle a Frigate event and send notifications.

        Args:
            event_data: Event data from MQTT containing:
                - event_id: Unique event ID
                - event_type: new/update/end
                - camera: Camera name
                - label: Detection label
                - zones: List of zones
                - score: Detection confidence
                - has_clip: Whether clip is available
                - has_snapshot: Whether snapshot is available
        """
        event_id = event_data.get("event_id")
        camera = event_data.get("camera")
        label = event_data.get("label")
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)

        _LOGGER.debug(
            "Processing event: %s (camera=%s, label=%s)",
            event_id,
            camera,
            label,
        )

        # Get devices that should receive this notification
        devices = await self.device_manager.async_get_devices_for_notification(
            camera=camera,
            label=label,
            zone=zones[0] if zones else None,
        )

        if not devices:
            _LOGGER.debug("No devices to notify for event %s", event_id)
            return

        # Build notification payload
        payload = await self._build_notification_payload(event_data)

        # Relay registrations are stored under the bridge device ID unless the
        # relay assigns a dedicated relay_device_id later.
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)
        device_tokens = []
        notified_devices = []
        for device in devices:
            token = _device_target(device, use_relay)
            if token:
                device_tokens.append(token)
                notified_devices.append(device)
        if not device_tokens:
            _LOGGER.debug("No device tokens available for notification")
            return

        # Send notifications
        _LOGGER.info(
            "Sending notification to %d devices for event %s (relay=%s)",
            len(device_tokens),
            event_id,
            use_relay,
        )

        results = await self.push_provider.async_send_to_many(device_tokens, payload)

        # Increment alert counts for successful sends
        for device, result in zip(notified_devices, results):
            if result.success:
                await self.device_manager.async_increment_alert_count(device["id"])

        # Log results
        success_count = sum(1 for r in results if r.success)
        failure_count = len(results) - success_count

        if failure_count > 0:
            _LOGGER.warning(
                "Notification sent: %d success, %d failure",
                success_count,
                failure_count,
            )

            # Handle failed tokens (e.g., remove invalid tokens)
            for result in results:
                if not result.success:
                    if "not-registered" in (result.error or "").lower():
                        # Token is invalid, could remove device or mark for cleanup
                        _LOGGER.info(
                            "FCM token no longer valid: %s",
                            result.device_id[:20] + "...",
                        )
        else:
            _LOGGER.debug("All %d notifications sent successfully", success_count)

    async def _build_notification_payload(
        self,
        event_data: dict[str, Any],
    ) -> NotificationPayload:
        """Build notification payload from event data."""
        event_id = event_data.get("event_id")
        camera = event_data.get("camera", "Unknown")
        label = event_data.get("label", "object")
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)
        has_snapshot = event_data.get("has_snapshot", False)

        # Build title
        title = f"{label.title()} detected"
        if camera:
            title = f"{label.title()} on {camera}"

        # Build body
        body_parts = []
        if score:
            body_parts.append(f"Confidence: {int(score * 100)}%")
        if zones:
            body_parts.append(f"Zone: {', '.join(zones)}")

        body = " · ".join(body_parts) if body_parts else f"Motion detected on {camera}"

        # Build image URLs
        thumbnail_url = None
        image_url = None

        if self._frigate_url and event_id:
            # Thumbnail URL (small, fast)
            thumbnail_url = f"{self._frigate_url}/api/events/{event_id}/thumbnail.jpg"

            # Snapshot URL (full size)
            if has_snapshot:
                image_url = f"{self._frigate_url}/api/events/{event_id}/snapshot.jpg"

        # Build data payload for the app
        data = {
            "type": "frigate_event",
            "event_id": event_id,
            "camera": camera,
            "label": label,
            "score": str(score),
            "timestamp": event_data.get("timestamp", datetime.utcnow().isoformat()),
        }

        if zones:
            data["zones"] = ",".join(zones)

        if self._frigate_url:
            data["frigate_url"] = self._frigate_url

        return NotificationPayload(
            title=title,
            body=body,
            data=data,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            priority="high",
            event_id=event_id,
            camera=camera,
            label=label,
            zones=zones,
        )

    async def async_get_frigate_thumbnail(
        self,
        event_id: str,
    ) -> bytes | None:
        """Fetch thumbnail from Frigate.

        This can be used to embed images in notifications for providers
        that don't support URL-based images.
        """
        if not self._frigate_url:
            return None

        try:
            session = async_get_clientsession(self.hass)
            url = f"{self._frigate_url}/api/events/{event_id}/thumbnail.jpg"

            # Add auth if configured
            auth = None
            if self._frigate_auth:
                from aiohttp import BasicAuth
                auth = BasicAuth(*self._frigate_auth)

            async with session.get(
                url,
                auth=auth,
                timeout=10,
                ssl=False,
            ) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    _LOGGER.warning(
                        "Failed to fetch thumbnail: %d",
                        response.status,
                    )
                    return None

        except Exception as e:
            _LOGGER.error("Error fetching thumbnail: %s", e)
            return None

    async def async_test_notification(
        self,
        device_id: str | None = None,
    ) -> list[SendResult]:
        """Send a test notification.

        Args:
            device_id: Specific device to test, or None for all devices

        Returns:
            List of send results
        """
        payload = NotificationPayload(
            title="Test Notification",
            body="This is a test from Frigate Notify Bridge",
            data={"type": "test"},
            priority="normal",
        )

        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)

        if device_id:
            device = await self.device_manager.async_get_device(device_id)
            if not device:
                return []
            token = _device_target(device, use_relay)
            if not token:
                return []
            result = await self.push_provider.async_send(token, payload)
            return [result]

        # Send to all devices
        devices = await self.device_manager.async_get_devices()
        tokens = []
        for device in devices.values():
            token = _device_target(device, use_relay)
            if token:
                tokens.append(token)

        if not tokens:
            return []

        return await self.push_provider.async_send_to_many(tokens, payload)

    def get_push_provider_info(self) -> dict[str, Any]:
        """Get information about the push provider for pairing."""
        return {
            "name": self.push_provider.name,
            "sender_id": self.push_provider.get_sender_id(),
            "initialized": self.push_provider.is_initialized,
        }
