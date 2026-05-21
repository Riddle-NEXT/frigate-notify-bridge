"""Coordinator for Frigate Notify Bridge."""
from __future__ import annotations

import asyncio
from pathlib import Path
import logging
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING
from urllib.parse import urlencode

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_MEDIA_PROXY_PATH,
    CONF_CAMERA_GROUPS,
    CONF_CROSS_CAMERA_COOLDOWN,
    CONF_FRIGATE_URL,
    CONF_FRIGATE_USERNAME,
    CONF_FRIGATE_PASSWORD,
    DEFAULT_CROSS_CAMERA_COOLDOWN,
    DEFAULT_NOTIFICATION_TITLE,
)
import json as _json

from .cross_camera import CrossCameraCorrelator, CorrelationRecord, compose_snapshot_image
from .device_manager import should_suspend_notification_delivery
from .issues import ISSUE_DEVICE_NOTIFICATION_UNREACHABLE, ISSUE_NOTIFICATION_DELIVERY
from .push_providers.base import NotificationPayload, SendResult
from .smart_rules import smart_rule_runtime_match

if TYPE_CHECKING:
    from .device_manager import DeviceManager
    from .issues import BridgeIssueManager
    from .push_providers import PushProvider

_LOGGER = logging.getLogger(__name__)
_SAMPLE_NOTIFICATION_IMAGE_ID = "bridge_sample_alert"
_SAMPLE_NOTIFICATION_IMAGE_PATH = (
    Path(__file__).resolve().parent / "brand" / "icon@2x.png"
)


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


def _coerce_timestamp(value: Any, *, default: float) -> float:
    """Parse Frigate timestamps while tolerating missing MQTT fields."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _format_sub_label(raw: str) -> str:
    """Format a sub_label for display (title case, clean separators)."""
    return raw.replace("_", " ").replace("-", " ").strip().title()


def _is_modifier_sub_label(sub_label: str) -> bool:
    """Check if a sub_label is a modifier (e.g. with_package) rather than an identity (e.g. John)."""
    lower = sub_label.lower().strip()
    return lower.startswith("with") or lower in {
        "package", "bicycle", "pet", "vehicle",
    }


def _object_signature(event_data: dict[str, Any]) -> str:
    """Return the stable object set used for cooldown and cross-camera dedupe."""
    objects = [
        str(item).strip().lower()
        for item in event_data.get("objects", []) or []
        if str(item).strip()
    ]
    if not objects:
        label = str(event_data.get("label") or "object").strip().lower()
        objects = [label or "object"]

    sub_label = str(event_data.get("sub_label") or "").strip().lower()
    if sub_label:
        objects.append(f"sub:{sub_label}")

    return "|".join(sorted(objects))


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
        self.last_event_at: str | None = None
        self.mqtt_subscribed: bool = False
        self._correlator = CrossCameraCorrelator()
        self._smart_rule_recent_events: list[dict[str, Any]] = []
        self._mute_mode_end_handles: dict[str, asyncio.TimerHandle] = {}

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
        self.last_event_at = datetime.now().isoformat()

        event_id = event_data.get("event_id")
        review_id = event_data.get("review_id")
        camera = event_data.get("camera")
        label = event_data.get("label")
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)
        event_kind = _normalize_event_kind(event_data.get("event_kind", "recording"))
        object_signature = _object_signature(event_data)
        recent_smart_rule_events = list(self._smart_rule_recent_events)
        self._remember_smart_rule_event(event_data)

        _LOGGER.debug(
            "Processing %s notification: event=%s review=%s camera=%s label=%s",
            event_kind,
            event_id,
            review_id,
            camera,
            label,
        )

        # Get devices that should receive this notification
        camera_groups = self.entry.options.get(CONF_CAMERA_GROUPS, {})
        cross_camera_cooldown = self.entry.options.get(
            CONF_CROSS_CAMERA_COOLDOWN, DEFAULT_CROSS_CAMERA_COOLDOWN
        )
        devices = await self.device_manager.async_get_devices_for_notification(
            kind=event_kind,
            camera=camera,
            label=label,
            sub_label=event_data.get("sub_label"),
            zones=zones,
            confidence=score,
            cooldown_key=(
                f"{event_kind}:{review_id or event_id or camera}:"
                f"{object_signature}"
            ),
            camera_groups=camera_groups,
            cross_camera_cooldown_seconds=cross_camera_cooldown,
            cross_camera_signature=object_signature,
        )

        if not devices:
            _LOGGER.debug("No devices to notify for event %s", event_id)
            return

        # Relay registrations are stored under the bridge device ID unless the
        # relay assigns a dedicated relay_device_id later.
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)

        async def send_for_device(
            device: dict[str, Any],
        ) -> tuple[dict[str, Any], SendResult] | None:
            token = _device_target(device, use_relay)
            if not token:
                return None

            settings = device.get("notification_settings", {})
            group = self._correlator.find_device_camera_group(camera or "", settings)
            smart_match = smart_rule_runtime_match(
                settings.get("smart_rules", []),
                event_data,
                recent_events=recent_smart_rule_events,
            )

            if group and camera and label:
                # Cross-camera correlation path
                is_update, record = self._correlator.check_correlation(
                    group_name=group["name"],
                    camera=camera,
                    label=label,
                    object_signature=object_signature,
                    event_id=event_id or "",
                    score=float(score or 0),
                    time_window=group.get("time_window_seconds", 10),
                )
                if is_update:
                    payload = await self._build_cross_camera_update_payload(
                        event_data, device, record
                    )
                else:
                    # First camera — send immediately but with a notification_tag
                    payload = await self._build_notification_payload(
                        event_data, device, notification_tag=record.notification_tag,
                    )
            else:
                payload = await self._build_notification_payload(event_data, device)

            if smart_match:
                rule = smart_match["rule"]
                payload.notification_tag = (
                    payload.notification_tag
                    or f"smart_mode_{rule.get('id') or rule.get('mode_type')}"
                )
                payload.data = dict(payload.data or {})
                payload.data.update({
                    "smart_mode": "1",
                    "smart_mode_id": str(rule.get("id") or ""),
                    "smart_mode_name": str(rule.get("name") or "Smart mode"),
                    "smart_mode_action": str(smart_match.get("action") or "update"),
                    "smart_mode_confidence": str(
                        int(float(smart_match.get("confidence") or 0) * 100)
                    ),
                })
                payload.title = f"{rule.get('name') or 'Smart mode'} active"
                payload.body = (
                    f"{payload.camera or camera or 'Camera'} matched "
                    f"{label or 'activity'}; updating this mode instead of sending repeats."
                )

            _LOGGER.info(
                "Sending %s notification to device %s for event=%s review=%s tag=%s",
                event_kind,
                device["id"],
                event_id,
                review_id,
                payload.notification_tag or "none",
            )
            if use_relay:
                result = await self.push_provider.async_send_to_device(
                    device,
                    token,
                    payload,
                )
            else:
                result = await self.push_provider.async_send(token, payload)
            return device, result

        send_results = await asyncio.gather(
            *(send_for_device(device) for device in devices)
        )
        notified_pairs = [item for item in send_results if item is not None]
        notified_devices = [device for device, _ in notified_pairs]
        results = [result for _, result in notified_pairs]

        if not results:
            _LOGGER.debug("No device targets available for notification")
            return

        suspended_failure_names: list[str] = []
        suspended_failure_reason: str | None = None

        # Record delivery results and increment alert counts for successful sends
        for device, result in zip(notified_devices, results):
            await self.device_manager.async_record_delivery_result(
                device["id"], result.success, result.error
            )
            if result.success:
                await self.device_manager.async_increment_alert_count(device["id"])
            elif should_suspend_notification_delivery(result.error):
                suspended_failure_names.append(device.get("name", result.device_id))
                suspended_failure_reason = suspended_failure_reason or result.error

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
                    (lambda issue_id, title, body, **kwargs: self._async_send_issue_alert(
                        successful_devices,
                        issue_id,
                        title,
                        body,
                        **kwargs,
                    ))
                    if successful_devices
                    else None
                ),
            )
            if suspended_failure_names:
                await self.issue_manager.async_report_device_notification_unreachable(
                    failed_devices=suspended_failure_names,
                    reason=suspended_failure_reason or first_error,
                    send_alert=(
                        (
                            lambda issue_id, title, body, **kwargs: self._async_send_issue_alert(
                                successful_devices,
                                issue_id,
                                title,
                                body,
                                **kwargs,
                            )
                        )
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
            suspended_devices = (
                await self.device_manager.async_get_notification_suspended_devices()
            )
            if not suspended_devices:
                await self.issue_manager.async_clear_issue(
                    ISSUE_DEVICE_NOTIFICATION_UNREACHABLE
                )
            _LOGGER.debug("All %d notifications sent successfully", success_count)

    def _remember_smart_rule_event(self, event_data: dict[str, Any]) -> None:
        """Keep a small rolling event window for session-aware smart rules."""
        now = time.time()
        event_time = _coerce_timestamp(
            event_data.get("start_time") or event_data.get("timestamp"),
            default=now,
        )
        remembered = dict(event_data)
        remembered.setdefault("start_time", event_time)
        remembered.setdefault("end_time", event_data.get("end_time") or event_time)
        self._smart_rule_recent_events.append(remembered)
        cutoff = now - 2 * 3600
        self._smart_rule_recent_events = [
            event
            for event in self._smart_rule_recent_events[-500:]
            if _coerce_timestamp(
                event.get("end_time")
                or event.get("start_time")
                or event.get("timestamp"),
                default=now,
            )
            >= cutoff
        ]

    async def _build_notification_payload(
        self,
        event_data: dict[str, Any],
        device: dict[str, Any],
        notification_tag: str | None = None,
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

        # Build title — enrich with sub_label when available
        display_label = _display_label(label)
        sub_label_is_identity = False
        if objects:
            display_label = ", ".join(_display_label(obj) for obj in objects[:2])
            if len(objects) > 2:
                display_label = f"{display_label}, +{len(objects) - 2}"

        if sub_label and str(sub_label).strip():
            cleaned_sub = str(sub_label).strip()
            if _is_modifier_sub_label(cleaned_sub):
                # Modifier: "Person With Package"
                display_label = f"{display_label} {_format_sub_label(cleaned_sub)}"
            else:
                # Identity: use sub_label as primary name (e.g. "John", "Buddy")
                sub_label_is_identity = True
                display_label = _format_sub_label(cleaned_sub)

        title = f"{display_label} on {camera}" if camera else f"{display_label} detected"
        if event_kind == "alert":
            title = f"{display_label} activity on {camera}" if camera else f"{display_label} activity"
        elif event_kind == "detection":
            title = f"{display_label} detected on {camera}" if camera else f"{display_label} detected"

        # Build body
        body_parts = []
        # When sub_label replaced the label in the title, show the base label as context
        if sub_label_is_identity:
            body_parts.append(_display_label(label))
        if score:
            score_percent = int(float(score) * 100) if float(score) <= 1 else int(float(score))
            body_parts.append(f"Confidence: {score_percent}%")
        if zones:
            body_parts.append(f"Zone: {', '.join(zones)}")
        body = " · ".join(body_parts) if body_parts else f"Motion detected on {camera}"

        # Build image URL - check preference order (GIF > snapshot > thumbnail)
        preferred_image_url = None
        if review_id and settings.get("include_gif_preview", False):
            preferred_image_url = self._build_media_url(device, "review_gif", str(review_id))
        elif primary_event_id:
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
            sub_label=str(sub_label) if sub_label else None,
            zones=zones,
            notification_tag=notification_tag,
        )

    async def async_send_mute_mode_status(
        self,
        mode: dict[str, Any],
        *,
        ended: bool,
    ) -> None:
        """Notify devices that mute mode started or ended."""
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)
        scope = str(mode.get("scope") or "device")
        creator = str(mode.get("created_by_name") or "another device").strip()
        title = "Mute mode ended" if ended else "Mute mode active"
        if ended:
            body = "Notifications have returned to normal."
        else:
            scope_text = "all devices" if scope == "all" else "this device"
            body = f"{creator} muted routine alerts for {scope_text}."
        data = {
            "type": "mute_mode",
            "mute_mode": "ended" if ended else "active",
            "mute_mode_scope": scope,
            "mute_mode_id": str(mode.get("id") or ""),
            "mute_mode_started_at": str(mode.get("started_at") or ""),
            "mute_mode_expires_at": str(mode.get("expires_at") or ""),
            "important_labels": ",".join(mode.get("important_labels") or []),
            "live_activity_enabled": "1"
            if mode.get("live_activity_enabled")
            else "0",
        }
        started_at = float(mode.get("started_at") or time.time())
        expires_at = float(mode.get("expires_at") or time.time())
        important_labels = list(mode.get("important_labels") or ["package"])
        live_activity = None
        if mode.get("live_activity_enabled"):
            live_activity = {
                "event": "end" if ended else "start",
                "tokenType": "update" if ended else "push_to_start",
                "attributesType": "MuteModeAttributes",
                "attributes": None if ended else {
                    "modeId": str(mode.get("id") or "mute-mode"),
                    "scope": scope,
                    "createdByName": creator,
                },
                "contentState": {
                    "title": title,
                    "body": body,
                    "startedAt": started_at,
                    "expiresAt": expires_at,
                    "importantLabels": important_labels,
                },
                "timestamp": int(time.time()),
                "dismissalDate": int(time.time()) if ended else None,
                "suppressStandardPush": True,
            }
            live_activity = {
                key: value
                for key, value in live_activity.items()
                if value is not None
            }
        devices = (await self.device_manager.async_get_devices()).values()
        for device in devices:
            if device.get("subscription_active") is False:
                continue
            if scope == "device" and device.get("id") != mode.get("device_id"):
                continue
            token = _device_target(device, use_relay)
            if not token:
                continue
            settings = self.device_manager.normalize_notification_settings(
                device.get("notification_settings")
            ).get("mute_mode", {})
            if ended and not settings.get("notify_on_end", True):
                continue
            if not ended and not settings.get("notify_on_start", True):
                continue
            payload = NotificationPayload(
                title=title,
                body=body,
                data=data,
                priority="normal",
                notification_tag=f"mute_mode_{mode.get('id') or scope}",
                live_activity=live_activity,
            )
            await self.push_provider.async_send(token, payload)
        if not ended:
            self._schedule_mute_mode_end_update(mode)

    async def async_send_smart_mode_status(
        self,
        device: dict[str, Any],
        rule: dict[str, Any],
        *,
        ended: bool,
    ) -> None:
        """Notify a device that a smart alert mode started or ended."""
        from .push_providers.relay import RelayPushProvider

        if device.get("subscription_active") is False:
            return

        token = _device_target(
            device,
            isinstance(self.push_provider, RelayPushProvider),
        )
        if not token:
            return

        rule_id = str(rule.get("id") or rule.get("mode_type") or "")
        rule_name = str(rule.get("name") or "Smart mode")
        title = f"{rule_name} ended" if ended else f"{rule_name} active"
        body = (
            "Rule deleted or disabled. Matching alerts are back to normal."
            if ended
            else "Matching alerts will update this mode instead of sending repeats."
        )
        payload = NotificationPayload(
            title=title,
            body=body,
            data={
                "type": "smart_mode",
                "smart_mode": "ended" if ended else "active",
                "smart_mode_id": rule_id,
                "smart_mode_name": rule_name,
                "smart_mode_action": "ended" if ended else "update",
            },
            priority="normal",
            notification_tag=f"smart_mode_{rule_id or rule.get('mode_type')}",
        )
        await self.push_provider.async_send(token, payload)

    def _schedule_mute_mode_end_update(self, mode: dict[str, Any]) -> None:
        """Schedule the configured end update for a mute mode."""
        mode_id = str(mode.get("id") or "")
        if not mode_id:
            return
        existing = self._mute_mode_end_handles.pop(mode_id, None)
        if existing:
            existing.cancel()
        try:
            delay = max(0, float(mode.get("expires_at")) - time.time())
        except (TypeError, ValueError):
            return

        async def _end_if_current() -> None:
            ended = await self.device_manager.async_end_mute_mode(
                str(mode.get("created_by_device_id") or mode.get("device_id") or ""),
                scope=str(mode.get("scope") or "device"),
            )
            if ended:
                await self.async_send_mute_mode_status(ended, ended=True)

        def _schedule_task() -> None:
            self.hass.async_create_task(_end_if_current())

        self._mute_mode_end_handles[mode_id] = self.hass.loop.call_later(
            delay,
            _schedule_task,
        )

    async def _build_cross_camera_update_payload(
        self,
        event_data: dict[str, Any],
        device: dict[str, Any],
        record: CorrelationRecord,
    ) -> NotificationPayload:
        """Build an UPDATE notification for a cross-camera correlation.

        This notification uses the same notification_tag as the original so
        it replaces the first notification on the device.
        """
        camera = event_data.get("camera", "Unknown")
        label = event_data.get("label", "object")
        score = event_data.get("score", 0)
        zones = event_data.get("zones", [])
        event_id = event_data.get("event_id")
        event_kind = _normalize_event_kind(event_data.get("event_kind", "recording"))

        display_label = _display_label(label)
        n_cameras = record.camera_count
        camera_list = ", ".join(record.cameras)

        title = f"{display_label} detected ({n_cameras} cameras)"
        if event_kind == "alert":
            title = f"{display_label} activity ({n_cameras} cameras)"

        # Build body with camera list and best confidence
        best_score = max(record.scores.values()) if record.scores else 0
        score_pct = int(best_score * 100) if best_score <= 1 else int(best_score)
        body_parts = [camera_list]
        if score_pct > 0:
            body_parts.append(f"{score_pct}% confidence")
        if zones:
            body_parts.append(f"Zone: {', '.join(zones)}")
        body = " \u00b7 ".join(body_parts)

        # Try to build a composite snapshot image
        image_url: str | None = None
        settings = device.get("notification_settings", {})
        primary_event_id = event_id or record.event_id

        if settings.get("include_snapshot", False) or settings.get("include_thumbnail", True):
            # Attempt composite from the Frigate API
            composite_bytes = await self._try_composite_snapshot(record)
            if composite_bytes is not None:
                # We can't serve arbitrary bytes directly; use the newest camera's snapshot
                # The composite is stored as a data URL in the data payload
                # For now, fall back to the latest camera's snapshot via proxy
                pass

            # Use the latest camera's event snapshot via media proxy
            latest_event_id = record.event_ids.get(camera) or primary_event_id
            if latest_event_id:
                if settings.get("include_snapshot", False):
                    image_url = self._build_media_url(device, "event_snapshot", latest_event_id)
                elif settings.get("include_thumbnail", True):
                    image_url = self._build_media_url(device, "event_thumbnail", latest_event_id)

        data: dict[str, Any] = {
            "ts": str(int(datetime.utcnow().timestamp())),
            "xcam": "1",
            "xcam_cameras": camera_list,
            "xcam_count": str(n_cameras),
        }
        if score_pct > 0:
            data["score"] = str(score_pct)
        if zones:
            data["zones"] = ",".join(zones[:3])

        return NotificationPayload(
            title=title,
            body=body,
            data=data,
            image_url=image_url,
            thumbnail_url=None,
            priority="high",
            event_id=primary_event_id,
            camera=camera,
            label=label,
            zones=zones,
            notification_tag=record.notification_tag,
        )

    async def _try_composite_snapshot(
        self,
        record: CorrelationRecord,
    ) -> bytes | None:
        """Attempt to build a composite side-by-side snapshot from multiple cameras."""
        if not self._frigate_url or len(record.event_ids) < 2:
            return None
        try:
            session = async_get_clientsession(self.hass)
            token = await self._async_get_frigate_access_token()
            return await compose_snapshot_image(
                session=session,
                frigate_url=self._frigate_url,
                event_ids=record.event_ids,
                access_token=token,
            )
        except Exception as err:
            _LOGGER.debug("Composite snapshot failed: %s", err)
            return None

    async def _async_get_review_details(self, review_id: str) -> dict[str, Any] | None:
        """Fetch full review details from Frigate when needed."""
        if not self._frigate_url or not review_id:
            return None

        session = async_get_clientsession(self.hass)
        headers: dict[str, str] = {}
        token = await self._async_get_frigate_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.get(
                f"{self._frigate_url}/api/review/{review_id}",
                headers=headers,
                timeout=10,
                ssl=False,
            ) as response:
                if response.status == 200:
                    return await response.json()
                if response.status == 401 and token:
                    self._frigate_api_token = None
        except aiohttp.ClientError as err:
            _LOGGER.debug(
                "Unable to fetch Frigate review details for %s: %s",
                review_id,
                err,
            )
        return None

    async def _async_get_recent_review(
        self,
        severity: str,
    ) -> dict[str, Any] | None:
        """Fetch the most recent Frigate review for a given severity."""
        if not self._frigate_url:
            return None

        session = async_get_clientsession(self.hass)
        headers: dict[str, str] = {}
        token = await self._async_get_frigate_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.get(
                f"{self._frigate_url}/api/review",
                headers=headers,
                params={"limit": 1, "severity": severity},
                timeout=10,
                ssl=False,
            ) as response:
                if response.status == 200:
                    reviews = await response.json()
                    if isinstance(reviews, list) and reviews:
                        return reviews[0]
                if response.status == 401 and token:
                    self._frigate_api_token = None
        except aiohttp.ClientError as err:
            _LOGGER.debug(
                "Unable to fetch recent Frigate %s review: %s",
                severity,
                err,
            )
        return None

    async def build_test_notification_payload(
        self,
        device: dict[str, Any],
        image_type: str = "thumbnail",
        use_recent_event: bool = True,
    ) -> tuple[NotificationPayload, dict[str, Any]]:
        """Build a test notification payload with deterministic fallbacks."""
        image_type = image_type if image_type in {"gif", "snapshot", "thumbnail", "none"} else "thumbnail"
        source = "sample"
        review: dict[str, Any] | None = None

        if use_recent_event:
            review = await self._async_get_recent_review("alert")
            if review:
                source = "recent_alert"
            else:
                review = await self._async_get_recent_review("detection")
                if review:
                    source = "recent_detection"

        review_id = review.get("id") if review else None
        if review_id:
            details = await self._async_get_review_details(str(review_id))
            if details:
                review = details

        if review:
            camera = str(review.get("camera") or "Unknown")
            severity = str(review.get("severity") or "alert").strip().lower()
            review_data = review.get("data") or {}
            objects = review_data.get("objects") or []
            label = (
                objects[0]
                if isinstance(objects, list) and objects
                else review.get("label") or "object"
            )
            label_text = _display_label(label)
            noun = "activity" if severity == "alert" else "detection"
            title = f"Test: {label_text} {noun} on {camera}"
            body = (
                "Using your most recent Frigate alert."
                if severity == "alert"
                else "Using your most recent Frigate detection."
            )

            image_url = None
            if image_type != "none":
                if image_type == "gif":
                    image_url = self._build_media_url(device, "review_gif", str(review_id))
                elif image_type in {"snapshot", "thumbnail"}:
                    primary_event_id = None
                    detections = review_data.get("detections") or []
                    if isinstance(detections, list) and detections:
                        primary_event_id = str(detections[0])
                    if primary_event_id:
                        media_kind = (
                            "event_snapshot" if image_type == "snapshot" else "event_thumbnail"
                        )
                        image_url = self._build_media_url(device, media_kind, primary_event_id)
                    elif image_type == "thumbnail":
                        image_url = self._build_media_url(
                            device,
                            "sample_image",
                            _SAMPLE_NOTIFICATION_IMAGE_ID,
                        )

            payload = NotificationPayload(
                title=title,
                body=body,
                data={
                    "type": "frigate_test",
                    "test_source": source,
                    "review_id": str(review_id),
                    "camera": camera,
                    "label": str(label),
                    "severity": severity,
                },
                image_url=image_url,
                thumbnail_url=None,
                priority="high",
                event_id=(
                    str((review_data.get("detections") or [None])[0])
                    if isinstance(review_data.get("detections"), list) and review_data.get("detections")
                    else f"test-review-{review_id}"
                ),
                camera=camera,
                label=str(label),
                zones=list(review.get("zones") or []),
            )
            return payload, {
                "source": source,
                "used_recent_event": True,
                "image_type": image_type,
                "has_image": bool(image_url),
                "review_id": str(review_id),
            }

        sample_image_url = None
        if image_type != "none":
            sample_image_url = self._build_media_url(
                device,
                "sample_image",
                _SAMPLE_NOTIFICATION_IMAGE_ID,
            )

        payload = NotificationPayload(
            title="Test: Person activity on Front Door",
            body="Sample alert fallback from Frigate Notify Bridge.",
            data={
                "type": "frigate_test",
                "test_source": "sample",
                "camera": "Front Door",
                "label": "person",
                "severity": "alert",
            },
            image_url=sample_image_url,
            thumbnail_url=None,
            priority="high",
            event_id=f"sample-alert-{int(datetime.utcnow().timestamp())}",
            camera="Front Door",
            label="person",
            zones=["entryway"],
        )
        return payload, {
            "source": "sample",
            "used_recent_event": False,
            "image_type": image_type,
            "has_image": bool(sample_image_url),
        }

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
        *,
        image_type: str = "thumbnail",
        use_recent_event: bool = True,
    ) -> list[SendResult]:
        """Send a test notification.

        Args:
            device_id: Specific device to test, or None for all devices

        Returns:
            List of send results
        """
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)

        if device_id:
            device = await self.device_manager.async_get_device(device_id)
            if not device:
                return []
            token = _device_target(device, use_relay)
            if not token:
                return []
            payload, _ = await self.build_test_notification_payload(
                device,
                image_type=image_type,
                use_recent_event=use_recent_event,
            )
            result = await self.push_provider.async_send(token, payload)
            return [result]

        # Send to all devices
        devices = await self.device_manager.async_get_devices()
        payload_by_token: list[tuple[str, NotificationPayload]] = []
        for device in devices.values():
            token = _device_target(device, use_relay)
            if token:
                payload, _ = await self.build_test_notification_payload(
                    device,
                    image_type=image_type,
                    use_recent_event=use_recent_event,
                )
                payload_by_token.append((token, payload))

        if not payload_by_token:
            return []

        results: list[SendResult] = []
        for token, payload in payload_by_token:
            results.append(await self.push_provider.async_send(token, payload))
        return results

    async def _async_send_issue_alert(
        self,
        devices: list[dict[str, Any]],
        issue_id: str,
        title: str,
        body: str,
        *,
        issue_type: str | None = None,
        affected_devices: list[str] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        suggested_action: str | None = None,
    ) -> None:
        """Send a bridge-attention alert with enriched error data to devices that still work."""
        from .push_providers.relay import RelayPushProvider

        use_relay = isinstance(self.push_provider, RelayPushProvider)

        # Build enriched data payload (capped for FCM 4KB limit)
        data: dict[str, Any] = {
            "type": "bridge_issue",
            "issue_id": issue_id,
            "timestamp": datetime.utcnow().isoformat(),
        }
        if issue_type:
            data["issue_type"] = issue_type
        if affected_devices:
            # Cap at 3 device names, with truncation indicator
            capped = affected_devices[:3]
            if len(affected_devices) > 3:
                capped.append(f"and {len(affected_devices) - 3} more")
            data["affected_devices"] = _json.dumps(capped)
        if error_code:
            data["error_code"] = error_code
        if error_detail:
            data["error_detail"] = error_detail[:200]
        if suggested_action:
            data["suggested_action"] = suggested_action

        payload = NotificationPayload(
            title=title,
            body=body,
            data=data,
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
