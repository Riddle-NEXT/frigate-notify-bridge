"""Device storage for standalone mode."""

import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DeviceStore:
    """Simple file-based device storage."""

    def __init__(self, data_dir: Path) -> None:
        """Initialize device store."""
        self.data_dir = data_dir
        self.devices_file = data_dir / "devices.json"
        self._devices: dict[str, dict[str, Any]] = {}
        self._pending_pairings: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        """Load devices from file."""
        if self.devices_file.exists():
            try:
                with open(self.devices_file) as f:
                    data = json.load(f)
                    self._devices = data.get("devices", {})
                    logger.info("Loaded %d devices from storage", len(self._devices))
            except Exception as e:
                logger.error("Failed to load devices: %s", e)
                self._devices = {}
        else:
            self._devices = {}

    async def save(self) -> None:
        """Save devices to file."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.devices_file, "w") as f:
                json.dump({"devices": self._devices}, f, indent=2)
            logger.debug("Saved %d devices to storage", len(self._devices))
        except Exception as e:
            logger.error("Failed to save devices: %s", e)

    def generate_pairing_code(self) -> dict[str, Any]:
        """Generate a new pairing code."""
        code = "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
            for _ in range(6)
        )
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(minutes=10)

        pairing_data = {
            "code": code,
            "token": token,
            "expires_at": expires_at.isoformat(),
        }

        self._pending_pairings[code] = pairing_data
        self._pending_pairings[token] = pairing_data

        return {
            "code": code,
            "token": token,
            "expires_at": expires_at.isoformat(),
            "expires_in": 600,
        }

    def validate_pairing_token(self, token_or_code: str) -> dict[str, Any] | None:
        """Validate a pairing token or code."""
        pairing_data = self._pending_pairings.get(token_or_code)
        if not pairing_data:
            return None

        expires_at = datetime.fromisoformat(pairing_data["expires_at"])
        if datetime.utcnow() > expires_at:
            self._cleanup_pairing(pairing_data)
            return None

        return pairing_data

    def _cleanup_pairing(self, pairing_data: dict[str, Any]) -> None:
        """Remove pairing data."""
        code = pairing_data.get("code")
        token = pairing_data.get("token")
        if code:
            self._pending_pairings.pop(code, None)
        if token:
            self._pending_pairings.pop(token, None)

    async def complete_pairing(
        self,
        token_or_code: str,
        device_info: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete device pairing."""
        pairing_data = self.validate_pairing_token(token_or_code)
        if not pairing_data:
            raise ValueError("Invalid or expired pairing token")

        device_id = secrets.token_urlsafe(16)
        api_token = secrets.token_urlsafe(32)

        device = {
            "id": device_id,
            "name": device_info.get("name", "Unknown Device"),
            "platform": device_info.get("platform", "unknown"),
            "fcm_token": device_info.get("fcm_token"),
            "app_version": device_info.get("app_version"),
            "api_token": api_token,
            "paired_at": datetime.utcnow().isoformat(),
            "last_seen": datetime.utcnow().isoformat(),
            "notification_settings": {
                "enabled": True,
                "cameras": [],
                "labels": ["person"],
                "zones": [],
                "cooldown_seconds": 60,
            },
        }

        self._devices[device_id] = device
        await self.save()

        self._cleanup_pairing(pairing_data)

        return {
            "device_id": device_id,
            "api_token": api_token,
        }

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Get a device by ID."""
        return self._devices.get(device_id)

    async def get_all_devices(self) -> dict[str, dict[str, Any]]:
        """Get all devices."""
        return self._devices.copy()

    async def update_device(
        self,
        device_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update device settings."""
        if device_id not in self._devices:
            return None

        device = self._devices[device_id]

        allowed = ["name", "fcm_token", "app_version", "notification_settings"]
        for key in allowed:
            if key in updates:
                if key == "notification_settings":
                    device["notification_settings"].update(updates[key])
                else:
                    device[key] = updates[key]

        device["last_seen"] = datetime.utcnow().isoformat()
        await self.save()

        return device

    async def remove_device(self, device_id: str) -> bool:
        """Remove a device."""
        if device_id not in self._devices:
            return False

        del self._devices[device_id]
        await self.save()
        return True

    def validate_api_token(self, api_token: str) -> str | None:
        """Validate API token and return device ID."""
        for device_id, device in self._devices.items():
            if device.get("api_token") == api_token:
                return device_id
        return None

    async def get_devices_for_notification(
        self,
        camera: str | None = None,
        label: str | None = None,
        zone: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get devices that should receive a notification."""
        devices_to_notify = []

        for device in self._devices.values():
            settings = device.get("notification_settings", {})

            if not settings.get("enabled", True):
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
            if allowed_zones and zone and zone not in allowed_zones:
                continue

            devices_to_notify.append(device)

        return devices_to_notify
