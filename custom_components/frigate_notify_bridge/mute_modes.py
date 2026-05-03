"""Mute-mode normalization and matching.

This module stays Home Assistant-free so the app/bridge mute contract can be
tested without loading the integration runtime.
"""
from __future__ import annotations

import time
from typing import Any


DEFAULT_MUTE_MODE_SETTINGS: dict[str, Any] = {
    "default_duration_minutes": 30,
    "default_scope": "device",
    "important_labels": ["package"],
    "important_sub_labels": [],
    "live_activity_enabled": False,
    "notify_on_start": True,
    "notify_on_end": True,
}


def normalize_mute_mode_settings(raw: Any) -> dict[str, Any]:
    """Normalize per-device mute-mode preferences synced from mobile."""
    merged = dict(DEFAULT_MUTE_MODE_SETTINGS)
    if isinstance(raw, dict):
        merged.update(raw)

    try:
        duration = int(float(merged.get("default_duration_minutes", 30)))
    except (TypeError, ValueError):
        duration = 30
    duration = max(1, min(24 * 60, duration))

    scope = str(merged.get("default_scope") or "device").strip().lower()
    if scope not in {"device", "all"}:
        scope = "device"

    return {
        "default_duration_minutes": duration,
        "default_scope": scope,
        "important_labels": _string_list(merged.get("important_labels")),
        "important_sub_labels": _string_list(merged.get("important_sub_labels")),
        "live_activity_enabled": bool(merged.get("live_activity_enabled", False)),
        "notify_on_start": bool(merged.get("notify_on_start", True)),
        "notify_on_end": bool(merged.get("notify_on_end", True)),
    }


def normalize_mute_mode(raw: Any) -> dict[str, Any] | None:
    """Normalize an active mute mode dict, or return None for inactive state."""
    if not isinstance(raw, dict) or not raw.get("active", True):
        return None
    try:
        expires_at = float(raw.get("expires_at"))
    except (TypeError, ValueError):
        return None
    scope = str(raw.get("scope") or "device").strip().lower()
    if scope not in {"device", "all"}:
        scope = "device"
    return {
        "active": True,
        "id": str(raw.get("id") or f"mute-{int(expires_at)}"),
        "scope": scope,
        "device_id": str(raw.get("device_id") or ""),
        "created_by_device_id": str(raw.get("created_by_device_id") or raw.get("device_id") or ""),
        "created_by_name": str(raw.get("created_by_name") or ""),
        "reason": str(raw.get("reason") or "manual"),
        "started_at": float(raw.get("started_at") or time.time()),
        "expires_at": expires_at,
        "important_labels": _string_list(raw.get("important_labels")),
        "important_sub_labels": _string_list(raw.get("important_sub_labels")),
        "live_activity_enabled": bool(raw.get("live_activity_enabled", False)),
        "notify_on_start": bool(raw.get("notify_on_start", True)),
        "notify_on_end": bool(raw.get("notify_on_end", True)),
    }


def active_mute_mode_for_device(
    mode: Any,
    *,
    device_id: str,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return active mode if it applies to this device."""
    parsed = normalize_mute_mode(mode)
    if parsed is None:
        return None
    current = now if now is not None else time.time()
    if parsed["expires_at"] <= current:
        return None
    if parsed["scope"] == "all":
        return parsed
    return parsed if parsed.get("device_id") == device_id else None


def should_mute_event(
    mode: Any,
    *,
    device_id: str,
    camera: str | None,
    label: str | None,
    sub_label: str | None,
    now: float | None = None,
) -> bool:
    """Return true when mute mode should suppress this event for a device."""
    active = active_mute_mode_for_device(mode, device_id=device_id, now=now)
    if active is None:
        return False

    normalized_label = str(label or "").strip().lower()
    important_labels = {
        item.strip().lower()
        for item in active.get("important_labels", [])
        if item.strip()
    }
    if normalized_label and normalized_label in important_labels:
        return False

    normalized_sub_label = str(sub_label or "").strip().lower()
    important_sub_labels = {
        item.strip().lower()
        for item in active.get("important_sub_labels", [])
        if item.strip()
    }
    if normalized_sub_label and normalized_sub_label in important_sub_labels:
        return False

    return True


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return sorted({str(item).strip() for item in raw if str(item).strip()})
