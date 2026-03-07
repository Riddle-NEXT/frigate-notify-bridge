"""Shared HA device metadata helpers for paired Frigate Mobile devices."""

from __future__ import annotations

from .const import DOMAIN, MANUFACTURER


def get_device_auth_mode(device: dict) -> str:
    """Return the auth mode label for a paired mobile device."""
    if device.get("auth_mode"):
        return str(device["auth_mode"])
    if device.get("ha_user_id"):
        return "native_mobile_app"
    return "legacy_bridge_token"


def build_mobile_device_info(
    entry_id: str,
    device_id: str,
    device: dict,
) -> dict:
    """Build consistent Home Assistant device registry metadata."""
    auth_mode = get_device_auth_mode(device)
    native_auth = auth_mode == "native_mobile_app"
    platform = str(device.get("platform", "mobile")).title()
    model = f"{platform} App"
    model_id = "frigate-mobile-native" if native_auth else "frigate-mobile-legacy"
    hw_version = (
        "Home Assistant Native Auth" if native_auth else "Legacy Bridge Pairing"
    )

    serial_number = (
        device.get("mobile_app_device_id")
        or device.get("relay_device_id")
        or device_id
    )

    return {
        "identifiers": {(DOMAIN, device_id)},
        "name": device.get("name", "Unknown Device"),
        "manufacturer": MANUFACTURER,
        "model": model,
        "model_id": model_id,
        "hw_version": hw_version,
        "serial_number": serial_number,
        "sw_version": device.get("app_version"),
        "via_device": (DOMAIN, entry_id),
    }
