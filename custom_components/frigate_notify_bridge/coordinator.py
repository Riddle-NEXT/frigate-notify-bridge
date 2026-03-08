"""Coordinator for Frigate Notify Bridge."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING
from urllib.parse import urlencode

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_MEDIA_PROXY_PATH,
    CONF_FRIGATE_URL,
    CONF_FRIGATE_USERNAME,
    CONF_FRIGATE_PASSWORD,
    DEFAULT_NOTIFICATION_TITLE,
)
from .issues import ISSUE_NOTIFICATION_DELIVERY
from .push_providers.base import NotificationPayload, SendResult

if TYPE_CHECKING:
    from .device_manager import DeviceManager
    from .issues import BridgeIssueManager
    from .push_providers import PushProvider

_LOGGER = logging.getLogger(__name__)


def _device_target(device: dict[str, Any], use_relay: bool) -> str | None:
    """Return the identifier that the active push provider expects."""
    if use_relay:
        return device.get("relay_device_id") or device.get("id")
    return device.get("fcm_token")


def _normalize_event_kind(kind: Any) -> str:
    """Normalize legacy event kinds to the app-facing values."""
    normalized = str(kind or "recording").strip().lower()
    if normalized == "event":
        return "recording"
    return normalized


def _display_label(raw_label: Any) -> str:
    """Format model labels for user-facing notification copy."""
    label = str(raw_label or "object").strip()
    if not label:
        return "Object"
    for suffix in ("-alert", "-detection", "-verified", "_alert", "_detection", "_verified"):
        if label.lower().endswith(suffix):
            label = label[: -len(suffix)]
            break
    label = label.replace("_", " ").replace("-", " ").strip()
    return label.title() or "Object"


class FrigateNotifyCoordinator:
    """Coordinate notifications between Frigate events and push providers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        push_provider: PushProvider,
        device_manager: DeviceManager,
        issue_manager: BridgeIssueManager,
    ) -> None:
        """Initialize the coordinator."""
        self.hass = hass
        self.entry = entry
        self.push_provider = push_provider
        self.device_manager = device_manager
        self.issue_manager = issue_manager
        self._frigate_url = entry.data.get(CONF_FRIGATE_URL)
        self._frigate_auth: tuple[str, str] | None = None
        self._frigate_api_token: str | None = None

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
        review_id = event_data.get("review_id")
        camera = event_data.get("camera")
        label = event_data.get("label")
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)
        event_kind = _normalize_event_kind(event_data.get("event_kind", "recording"))

        _LOGGER.debug(
            "Processing %s notification: event=%s review=%s camera=%s label=%s",
            event_kind,
            event_id,
            review_id,
            camera,
            label,
        )

        # Get devices that should receive this notification
        devices = await self.device_manager.async_get_devices_for_notification(
            kind=event_kind,
            camera=camera,
            label=label,
            sub_label=event_data.get("sub_label"),
            zones=zones,
            confidence=score,
            cooldown_key=f"{event_kind}:{review_id or event_id or camera}:{label or ''}",
        )

        if not devices:
            _LOGGER.debug("No devices to notify for event %s", event_id)
            return

        # Relay registrations are stored under the bridge device ID unless the
        # relay assigns a dedicated relay_device_id later.
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)
        results: list[SendResult] = []
        notified_devices: list[dict[str, Any]] = []

        for device in devices:
            token = _device_target(device, use_relay)
            if not token:
                continue
            payload = await self._build_notification_payload(event_data, device)
            _LOGGER.info(
                "Sending %s notification to device %s for event=%s review=%s",
                event_kind,
                device["id"],
                event_id,
                review_id,
            )
            result = await self.push_provider.async_send(token, payload)
            results.append(result)
            notified_devices.append(device)

        if not results:
            _LOGGER.debug("No device targets available for notification")
            return

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

            failed_device_names = [
                device.get("name", result.device_id)
                for device, result in zip(notified_devices, results)
                if not result.success
            ]
            first_error = next(
                (
                    result.error
                    for result in results
                    if not result.success and result.error
                ),
                "Unknown delivery error",
            )
            successful_devices = [
                device
                for device, result in zip(notified_devices, results)
                if result.success
            ]
            await self.issue_manager.async_report_notification_delivery_failure(
                failed_devices=failed_device_names,
                reason=first_error,
                send_alert=(
                    (lambda issue_id, title, body: self._async_send_issue_alert(
                        successful_devices,
                        issue_id,
                        title,
                        body,
                    ))
                    if successful_devices
                    else None
                ),
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
            await self.issue_manager.async_clear_issue(ISSUE_NOTIFICATION_DELIVERY)
            _LOGGER.debug("All %d notifications sent successfully", success_count)

    async def _build_notification_payload(
        self,
        event_data: dict[str, Any],
        device: dict[str, Any],
    ) -> NotificationPayload:
        """Build notification payload from event data."""
        event_id = event_data.get("event_id")
        review_id = event_data.get("review_id")
        camera = event_data.get("camera", "Unknown")
        label = event_data.get("label", "object")
        objects = event_data.get("objects", [])
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)
        has_snapshot = event_data.get("has_snapshot", False)
        has_clip = event_data.get("has_clip", False)
        start_time = event_data.get("start_time")
        end_time = event_data.get("end_time")
        sub_label = event_data.get("sub_label")
        event_kind = _normalize_event_kind(event_data.get("event_kind", "recording"))
        settings = device.get("notification_settings", {})
        event_ids = event_data.get("event_ids", [])

        primary_event_id = event_id or (event_ids[0] if event_ids else None)
        if primary_event_id:
            details = await self._async_get_event_details(primary_event_id)
            if details:
                score = details.get("score", score)
                has_snapshot = details.get("has_snapshot", has_snapshot)
                has_clip = details.get("has_clip", has_clip)
                start_time = details.get("start_time", start_time)
                end_time = details.get("end_time", end_time)
                sub_label = details.get("sub_label", sub_label)
                if not label or label == "object":
                    label = details.get("label", label)
                if not zones:
                    zones = details.get("current_zones", []) or details.get("entered_zones", []) or zones

        # Build title
        display_label = _display_label(label)
        if objects:
            display_label = ", ".join(_display_label(obj) for obj in objects[:2])
            if len(objects) > 2:
                display_label = f"{display_label}, +{len(objects) - 2}"
        title = f"{display_label} on {camera}" if camera else f"{display_label} detected"
        if event_kind == "alert":
            title = f"{display_label} activity on {camera}" if camera else f"{display_label} activity"
        elif event_kind == "detection":
            title = f"{display_label} detected on {camera}" if camera else f"{display_label} detected"

        # Build body
        body_parts = []
        if score:
            score_percent = int(float(score) * 100) if float(score) <= 1 else int(float(score))
            body_parts.append(f"Confidence: {score_percent}%")
        if zones:
            body_parts.append(f"Zone: {', '.join(zones)}")
        if sub_label:
            body_parts.append(str(sub_label))
        body = " · ".join(body_parts) if body_parts else f"Motion detected on {camera}"

        # Build image URL - check preference order (GIF > snapshot > thumbnail)
        preferred_image_url = None
        if primary_event_id:
            # Priority 1: Animated preview GIF (if enabled)
            if settings.get("include_gif_preview", False):
                preferred_image_url = self._build_media_url(device, "event_preview_gif", primary_event_id)
            # Priority 2: Static snapshot
            elif has_snapshot and settings.get("include_snapshot", False):
                preferred_image_url = self._build_media_url(device, "event_snapshot", primary_event_id)
            # Priority 3: Thumbnail (default)
            elif settings.get("include_thumbnail", True):
                preferred_image_url = self._build_media_url(device, "event_thumbnail", primary_event_id)

        # Build compact data payload for the app (minimized for FCM 4KB limit)
        # Fields sent in plaintext notificationData are excluded from encrypted payload
        # to avoid duplication. The relay sends: event_id, review_id, camera, label,
        # event_kind, sub_label, start_time in plaintext notificationData.
        # This encrypted data dict contains only app-specific fields not in plaintext.
        data: dict[str, Any] = {
            "ts": str(int(datetime.utcnow().timestamp())),  # Unix timestamp (compact)
        }

        # Only include booleans if true (saves bytes when false is default)
        if has_clip:
            data["clip"] = "1"
        if has_snapshot:
            data["snap"] = "1"

        # Include score as integer percentage (saves ~3 bytes vs decimal string)
        if score:
            score_int = int(float(score) * 100) if float(score) <= 1 else int(float(score))
            if score_int > 0:
                data["score"] = str(score_int)

        # Limit zones to 3 max to reduce payload size
        if zones:
            data["zones"] = ",".join(zones[:3])

        # Limit objects to 2 max
        if objects and len(objects) > 0:
            data["objects"] = [str(obj) for obj in objects[:2]]

        return NotificationPayload(
            title=title,
            body=body,
            data=data,
            image_url=preferred_image_url,
            thumbnail_url=None,
            priority="high",
            event_id=primary_event_id,
            camera=camera,
            label=label,
            zones=zones,
        )

    def _build_media_url(
        self,
        device: dict[str, Any],
        media_kind: str,
        media_id: str,
    ) -> str | None:
        """Build a signed absolute media proxy URL for a device."""
        base_url = (
            device.get("mobile_app_remote_ui_url")
            or self.entry.options.get("external_url")
            or self._frigate_url
        )
        device_id = device.get("id")
        if not base_url or not device_id:
            return None

        expires = int(datetime.utcnow().timestamp()) + 10 * 60
        signature = self.device_manager.create_media_signature(
            device_id=device_id,
            media_kind=media_kind,
            media_id=media_id,
            expires=expires,
        )
        if not signature:
            return None

        query = urlencode({
            "device_id": device_id,
            "expires": expires,
            "sig": signature,
        })
        return f"{base_url.rstrip('/')}{API_MEDIA_PROXY_PATH}/{media_kind}/{media_id}?{query}"

    async def _async_get_frigate_access_token(self) -> str | None:
        """Get or refresh a Frigate API token using integration credentials."""
        if self._frigate_api_token:
            return self._frigate_api_token
        if not self._frigate_url or not self._frigate_auth:
            return None

        session = async_get_clientsession(self.hass)
        username, password = self._frigate_auth
        try:
            async with session.post(
                f"{self._frigate_url}/api/login",
                json={"user": username, "password": password},
                timeout=10,
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._frigate_api_token = data.get("access_token")
                    return self._frigate_api_token
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to authenticate to Frigate API: %s", err)
        return None

    async def _async_get_event_details(self, event_id: str) -> dict[str, Any] | None:
        """Fetch full event details from Frigate when needed."""
        if not self._frigate_url or not event_id:
            return None

        session = async_get_clientsession(self.hass)
        headers: dict[str, str] = {}
        token = await self._async_get_frigate_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.get(
                f"{self._frigate_url}/api/events/{event_id}",
                headers=headers,
                timeout=10,
                ssl=False,
            ) as response:
                if response.status == 200:
                    return await response.json()
                if response.status == 401 and token:
                    self._frigate_api_token = None
        except aiohttp.ClientError as err:
            _LOGGER.debug("Unable to fetch Frigate event details for %s: %s", event_id, err)
        return None

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
            data={
                "type": "test",
                "timestamp": datetime.utcnow().isoformat(),
            },
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

    async def _async_send_issue_alert(
        self,
        devices: list[dict[str, Any]],
        issue_id: str,
        title: str,
        body: str,
    ) -> None:
        """Send a minimal bridge-attention alert to devices that still work."""
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)
        payload = NotificationPayload(
            title=title,
            body=body,
            data={
                "type": "bridge_issue",
                "issue_id": issue_id,
                "timestamp": datetime.utcnow().isoformat(),
            },
            priority="high",
        )

        for device in devices:
            token = _device_target(device, use_relay)
            if not token:
                continue
            result = await self.push_provider.async_send(token, payload)
            if not result.success:
                _LOGGER.debug(
                    "Bridge issue alert failed for %s: %s",
                    device.get("name", token),
                    result.error,
                )

    def get_push_provider_info(self) -> dict[str, Any]:
        """Get information about the push provider for pairing."""
        return {
            "name": self.push_provider.name,
            "sender_id": self.push_provider.get_sender_id(),
            "initialized": self.push_provider.is_initialized,
        }
