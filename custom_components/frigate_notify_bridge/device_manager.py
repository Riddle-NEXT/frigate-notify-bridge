"""Device manager for Frigate Notify Bridge."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    PAIRING_TOKEN_EXPIRY_SECONDS,
    PAIRING_CODE_LENGTH,
    SIGNAL_DEVICE_REGISTERED,
    SIGNAL_DEVICE_REMOVED,
    SIGNAL_DEVICE_UPDATED,
    EVENT_DEVICE_PAIRED,
    EVENT_DEVICE_REMOVED,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_NOTIFICATION_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "event_kinds": ["alert"],
    "cameras": [],
    "labels": [],
    "zones": [],
    "min_confidence": 0,
    "cooldown_seconds": 60,
    "quiet_hours_start": None,
    "quiet_hours_end": None,
    "include_thumbnail": True,
    "include_snapshot": False,
    "include_actions": True,
    "include_gif_preview": False,
}

ALLOWED_EVENT_KINDS = {"alert", "detection", "event"}


class DeviceManager:
    """Manage paired mobile devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        store,
        initial_devices: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the device manager."""
        self.hass = hass
        self._store = store
        self._devices: dict[str, dict[str, Any]] = initial_devices or {}
        self._pending_pairings: dict[str, dict[str, Any]] = {}
        self._cooldowns: dict[str, datetime] = {}

        for device in self._devices.values():
            device["notification_settings"] = self.normalize_notification_settings(
                device.get("notification_settings")
            )

    async def async_save(self) -> None:
        """Save devices to storage."""
        await self._store.async_save(
            {
                "devices": self._devices,
                "settings": {},
            }
        )

    async def async_get_devices(self) -> dict[str, dict[str, Any]]:
        """Get all paired devices."""
        return self._devices.copy()

    async def async_get_device(self, device_id: str) -> dict[str, Any] | None:
        """Get a specific device."""
        return self._devices.get(device_id)

    @classmethod
    def normalize_notification_settings(
        cls,
        settings: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Normalize persisted notification settings."""
        merged = dict(DEFAULT_NOTIFICATION_SETTINGS)
        if settings:
            merged.update(settings)

        event_kinds = [
            str(kind).strip().lower()
            for kind in merged.get("event_kinds", [])
            if str(kind).strip().lower() in ALLOWED_EVENT_KINDS
        ]
        if not event_kinds:
            event_kinds = list(DEFAULT_NOTIFICATION_SETTINGS["event_kinds"])

        def _string_list(key: str) -> list[str]:
            values = merged.get(key, [])
            if not isinstance(values, list):
                return []
            result: list[str] = []
            for value in values:
                normalized = str(value).strip()
                if normalized:
                    result.append(normalized)
            return sorted(set(result))

        def _hour_or_none(key: str) -> int | None:
            value = merged.get(key)
            if value in (None, "", False):
                return None
            try:
                hour = int(value)
            except (TypeError, ValueError):
                return None
            return hour if 0 <= hour <= 23 else None

        try:
            min_confidence = int(float(merged.get("min_confidence", 0)))
        except (TypeError, ValueError):
            min_confidence = 0
        min_confidence = max(0, min(100, min_confidence))

        try:
            cooldown_seconds = int(float(merged.get("cooldown_seconds", 60)))
        except (TypeError, ValueError):
            cooldown_seconds = 60
        cooldown_seconds = max(0, min(24 * 3600, cooldown_seconds))

        return {
            "enabled": bool(merged.get("enabled", True)),
            "event_kinds": event_kinds,
            "cameras": _string_list("cameras"),
            "labels": _string_list("labels"),
            "zones": _string_list("zones"),
            "min_confidence": min_confidence,
            "cooldown_seconds": cooldown_seconds,
            "quiet_hours_start": _hour_or_none("quiet_hours_start"),
            "quiet_hours_end": _hour_or_none("quiet_hours_end"),
            "include_thumbnail": bool(merged.get("include_thumbnail", True)),
            "include_snapshot": bool(merged.get("include_snapshot", False)),
            "include_actions": bool(merged.get("include_actions", True)),
            "include_gif_preview": bool(merged.get("include_gif_preview", False)),
        }

    def _device_media_secret(self, device: dict[str, Any]) -> str | None:
        """Return the strongest available per-device secret for media URL signing."""
        return (
            device.get("mobile_app_secret")
            or device.get("api_token")
            or None
        )

    def create_media_signature(
        self,
        device_id: str,
        media_kind: str,
        media_id: str,
        expires: int,
    ) -> str | None:
        """Create a short-lived HMAC signature for media proxy access."""
        device = self._devices.get(device_id)
        if not device:
            return None
        secret = self._device_media_secret(device)
        if not secret:
            return None
        canonical = "\n".join([device_id, media_kind, media_id, str(expires)])
        return hmac.new(
            secret.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()

    def validate_media_signature(
        self,
        *,
        device_id: str,
        media_kind: str,
        media_id: str,
        expires: int,
        signature: str,
    ) -> bool:
        """Validate a signed media proxy URL."""
        if expires < int(datetime.utcnow().timestamp()):
            return False
        expected = self.create_media_signature(device_id, media_kind, media_id, expires)
        if not expected:
            return False
        return hmac.compare_digest(expected, signature)

    def generate_pairing_code(self) -> dict[str, Any]:
        """Generate a new pairing code and token.

        Returns a dict with:
        - code: Human-readable 6-character code for display
        - token: Full token for QR code
        - expires_at: Expiration timestamp
        """
        self.cleanup_expired_pairings()

        # The add-device flow should expose exactly one active QR code at a
        # time so a retried pairing attempt cannot accidentally validate
        # against stale in-memory state.
        if self._pending_pairings:
            pending_codes = {
                data.get("code")
                for data in self._pending_pairings.values()
                if data.get("code")
            }
            self._pending_pairings.clear()
            _LOGGER.debug(
                "Cleared %d stale pending pairing(s) before issuing a new code",
                len(pending_codes),
            )

        # Generate a readable pairing code (6 alphanumeric chars)
        code = "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
            for _ in range(PAIRING_CODE_LENGTH)
        )

        # Generate a secure token
        token = secrets.token_urlsafe(32)

        expires_at = datetime.utcnow() + timedelta(seconds=PAIRING_TOKEN_EXPIRY_SECONDS)

        pairing_data = {
            "code": code,
            "token": token,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.utcnow().isoformat(),
        }

        # Store by both code and token for flexible lookup
        self._pending_pairings[code] = pairing_data
        self._pending_pairings[token] = pairing_data

        _LOGGER.debug("Generated pairing code: %s (expires: %s)", code, expires_at)

        return {
            "code": code,
            "token": token,
            "expires_at": expires_at.isoformat(),
            "expires_in": PAIRING_TOKEN_EXPIRY_SECONDS,
        }

    def validate_pairing_token(self, token_or_code: str) -> dict[str, Any] | None:
        """Validate a pairing token or code.

        Returns the pairing data if valid, None if invalid or expired.
        """
        pairing_data = self._pending_pairings.get(token_or_code)
        if pairing_data is None:
            _LOGGER.debug(
                "Rejected pairing lookup for unknown token/code: len=%d prefix=%s",
                len(token_or_code),
                token_or_code[:8],
            )
            return None

        # Check expiration
        expires_at = datetime.fromisoformat(pairing_data["expires_at"])
        if datetime.utcnow() > expires_at:
            # Clean up expired pairing
            _LOGGER.debug(
                "Rejected expired pairing token/code for code=%s expired_at=%s",
                pairing_data.get("code"),
                pairing_data["expires_at"],
            )
            self._cleanup_pairing(pairing_data)
            return None

        _LOGGER.debug(
            "Validated pairing token/code for code=%s expires_at=%s",
            pairing_data.get("code"),
            pairing_data["expires_at"],
        )
        return pairing_data

    def _cleanup_pairing(self, pairing_data: dict[str, Any]) -> None:
        """Remove a pairing from pending."""
        code = pairing_data.get("code")
        token = pairing_data.get("token")
        if code:
            self._pending_pairings.pop(code, None)
        if token:
            self._pending_pairings.pop(token, None)

    async def async_complete_pairing(
        self,
        token_or_code: str,
        device_info: dict[str, Any],
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Complete device pairing.

        Args:
            token_or_code: The pairing token or code
            device_info: Device information from the mobile app:
                - name: Device name
                - platform: ios/android
                - fcm_token: FCM registration token (for FCM provider)
                - app_version: App version string

        Returns:
            Device registration data including device_id and optional API token

        Raises:
            ValueError: If token is invalid or expired
        """
        pairing_data = self.validate_pairing_token(token_or_code)
        if pairing_data is None:
            raise ValueError("Invalid or expired pairing token")

        # In the HA-native flow, the app provides its stable mobile_app
        # device_id and authenticates as the signed-in HA user. Keep the
        # legacy bridge-issued API token path for backwards compatibility.
        device_id = device_info.get("mobile_app_device_id") or secrets.token_urlsafe(16)
        api_token = None if user_id else secrets.token_urlsafe(32)

        # Create device record
        device = {
            "id": device_id,
            "name": device_info.get("name", "Unknown Device"),
            "platform": device_info.get("platform", "unknown"),
            "fcm_token": device_info.get("fcm_token"),
            "app_version": device_info.get("app_version"),
            "api_token": api_token,
            "auth_mode": "native_mobile_app" if user_id else "legacy_bridge_token",
            "ha_user_id": user_id,
            "mobile_app_device_id": device_info.get("mobile_app_device_id"),
            "mobile_app_webhook_id": device_info.get("mobile_app_webhook_id"),
            "mobile_app_secret": device_info.get("mobile_app_secret"),
            "mobile_app_cloudhook_url": device_info.get("mobile_app_cloudhook_url"),
            "mobile_app_remote_ui_url": device_info.get("mobile_app_remote_ui_url"),
            "paired_at": datetime.utcnow().isoformat(),
            "last_seen": datetime.utcnow().isoformat(),
            "notification_settings": self.normalize_notification_settings(None),
            "alert_count_today": 0,
            "alert_count_total": 0,
            "alert_count_date": datetime.utcnow().strftime("%Y-%m-%d"),
        }

        self._devices[device_id] = device
        await self.async_save()

        # Clean up pairing data
        self._cleanup_pairing(pairing_data)

        # Notify listeners
        async_dispatcher_send(self.hass, SIGNAL_DEVICE_REGISTERED, device_id)

        # Fire HA event for automations
        self.hass.bus.async_fire(EVENT_DEVICE_PAIRED, {
            "device_id": device_id,
            "device_name": device["name"],
            "platform": device["platform"],
        })

        # Create persistent notification
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Frigate Mobile Device Paired",
                "message": (
                    f"**{device['name']}** ({device['platform']}) "
                    f"has been paired successfully."
                ),
                "notification_id": f"{DOMAIN}_paired_{device_id}",
            },
        )

        _LOGGER.info("Device paired: %s (%s)", device["name"], device_id)

        result = {"device_id": device_id}
        if api_token:
            result["api_token"] = api_token
        return result

    async def async_remove_device(self, device_id: str) -> bool:
        """Remove a paired device."""
        if device_id not in self._devices:
            return False

        device = self._devices.pop(device_id)
        await self.async_save()

        # Remove the HA device registry entry so it doesn't linger as an orphan
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, device_id)}
        )
        if device_entry:
            device_registry.async_remove_device(device_entry.id)

        # Notify listeners
        async_dispatcher_send(self.hass, SIGNAL_DEVICE_REMOVED, device_id)

        # Fire HA event for automations
        self.hass.bus.async_fire(EVENT_DEVICE_REMOVED, {
            "device_id": device_id,
            "device_name": device.get("name", "Unknown"),
        })

        _LOGGER.info("Device removed: %s (%s)", device.get("name"), device_id)
        return True

    async def async_update_device(
        self,
        device_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update device information."""
        if device_id not in self._devices:
            return None

        device = self._devices[device_id]

        # Update allowed fields
        allowed_updates = ["name", "fcm_token", "app_version", "notification_settings", "relay_device_id"]
        for key in allowed_updates:
            if key in updates:
                if key == "notification_settings":
                    # Merge notification settings
                    merged_settings = dict(device.get("notification_settings", {}))
                    merged_settings.update(updates[key])
                    device["notification_settings"] = self.normalize_notification_settings(
                        merged_settings
                    )
                else:
                    device[key] = updates[key]

        device["last_seen"] = datetime.utcnow().isoformat()
        _LOGGER.debug(
            "Updated device %s fields=%s last_seen=%s",
            device_id,
            list(updates.keys()),
            device["last_seen"],
        )

        await self.async_save()
        return device

    async def async_update_fcm_token(
        self,
        device_id: str,
        fcm_token: str,
    ) -> bool:
        """Update a device's FCM token."""
        if device_id not in self._devices:
            return False

        self._devices[device_id]["fcm_token"] = fcm_token
        self._devices[device_id]["last_seen"] = datetime.utcnow().isoformat()
        await self.async_save()
        _LOGGER.info(
            "FCM token updated for device %s (%s)",
            self._devices[device_id].get("name"),
            device_id,
        )
        return True

    def validate_api_token(self, api_token: str) -> str | None:
        """Validate an API token and return the device ID if valid."""
        for device_id, device in self._devices.items():
            if device.get("api_token") == api_token:
                return device_id
        return None

    def user_owns_device(self, user_id: str, device_id: str) -> bool:
        """Return whether the HA user owns the paired bridge device."""
        device = self._devices.get(device_id)
        if not device:
            return False
        return device.get("ha_user_id") == user_id

    async def async_get_devices_for_notification(
        self,
        kind: str,
        camera: str | None = None,
        label: str | None = None,
        zones: list[str] | None = None,
        confidence: float | int | None = None,
        cooldown_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get devices that should receive a notification based on filters."""
        devices_to_notify = []
        now = datetime.utcnow()
        normalized_kind = kind.strip().lower()
        zone_set = {str(zone) for zone in (zones or []) if str(zone).strip()}
        confidence_percent = None
        if confidence is not None:
            try:
                confidence_float = float(confidence)
                confidence_percent = confidence_float * 100 if confidence_float <= 1 else confidence_float
            except (TypeError, ValueError):
                confidence_percent = None

        for device in self._devices.values():
            settings = self.normalize_notification_settings(
                device.get("notification_settings")
            )

            # Check if notifications are enabled
            if not settings.get("enabled", True):
                continue

            if normalized_kind not in settings.get("event_kinds", []):
                continue

            # Check camera filter
            allowed_cameras = settings.get("cameras", [])
            if allowed_cameras and camera and camera not in allowed_cameras:
                continue

            # Check label filter
            allowed_labels = settings.get("labels", [])
            if allowed_labels and label and label not in allowed_labels:
                continue

            # Check zone filter
            allowed_zones = settings.get("zones", [])
            if allowed_zones:
                if not zone_set:
                    continue
                if not zone_set.intersection(allowed_zones):
                    continue

            min_confidence = settings.get("min_confidence", 0)
            if confidence_percent is not None and confidence_percent < min_confidence:
                continue

            # Check quiet hours
            quiet_start = settings.get("quiet_hours_start")
            quiet_end = settings.get("quiet_hours_end")
            if quiet_start is not None and quiet_end is not None:
                current_hour = datetime.now().hour
                if quiet_start <= quiet_end:
                    # Normal range (e.g., 22-7 doesn't wrap)
                    if quiet_start <= current_hour < quiet_end:
                        continue
                else:
                    # Wrapped range (e.g., 22-7 wraps midnight)
                    if current_hour >= quiet_start or current_hour < quiet_end:
                        continue

            if cooldown_key:
                device_cooldown_key = f"{device['id']}:{cooldown_key}"
                last_sent = self._cooldowns.get(device_cooldown_key)
                cooldown_seconds = settings.get("cooldown_seconds", 60)
                if (
                    last_sent
                    and cooldown_seconds > 0
                    and (now - last_sent).total_seconds() < cooldown_seconds
                ):
                    continue
                self._cooldowns[device_cooldown_key] = now

            devices_to_notify.append(device)

        return devices_to_notify

    async def async_set_frigate_credentials(
        self,
        device_id: str,
        username: str,
        password: str,
    ) -> bool:
        """Store per-device Frigate credentials for proxy authentication."""
        if device_id not in self._devices:
            return False

        self._devices[device_id]["frigate_username"] = username
        self._devices[device_id]["frigate_password"] = password
        await self.async_save()

        _LOGGER.debug("Updated Frigate credentials for device %s", device_id)
        return True

    def get_frigate_credentials(
        self,
        device_id: str,
    ) -> tuple[str | None, str | None]:
        """Get Frigate credentials for a device.

        Returns per-device credentials if set, otherwise returns (None, None).
        The caller should fall back to integration-level credentials.
        """
        device = self._devices.get(device_id)
        if not device:
            return None, None

        username = device.get("frigate_username")
        password = device.get("frigate_password")
        if username and password:
            return username, password

        return None, None

    def cleanup_expired_pairings(self) -> int:
        """Clean up expired pairing tokens. Returns count of removed."""
        now = datetime.utcnow()
        expired_codes = []

        for key, data in self._pending_pairings.items():
            expires_at = datetime.fromisoformat(data["expires_at"])
            if now > expires_at:
                expired_codes.append(data.get("code"))

        # Remove expired (using codes to avoid removing twice)
        removed = 0
        for code in set(expired_codes):
            if code and code in self._pending_pairings:
                data = self._pending_pairings.pop(code)
                token = data.get("token")
                if token:
                    self._pending_pairings.pop(token, None)
                removed += 1

        if removed:
            _LOGGER.debug("Cleaned up %d expired pairing tokens", removed)

        return removed

    async def async_increment_alert_count(self, device_id: str) -> None:
        """Increment alert counters for a device, resetting today's count at midnight."""
        if device_id not in self._devices:
            return

        device = self._devices[device_id]
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Reset today counter if day has rolled over
        if device.get("alert_count_date") != today:
            device["alert_count_today"] = 0
            device["alert_count_date"] = today

        device["alert_count_today"] = device.get("alert_count_today", 0) + 1
        device["alert_count_total"] = device.get("alert_count_total", 0) + 1

        await self.async_save()
        async_dispatcher_send(self.hass, SIGNAL_DEVICE_UPDATED, device_id)
