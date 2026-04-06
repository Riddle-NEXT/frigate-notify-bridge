"""Cross-camera alert correlation for Frigate Notify Bridge.

When cameras in the same group fire on the same label within a time window,
the bridge sends an initial notification immediately and then sends an UPDATE
notification (with the same notification_tag) so the second alert replaces
the first on the user's device.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Maximum age in seconds before a correlation record is garbage-collected.
_MAX_CORRELATION_TTL = 120


@dataclass
class CorrelationRecord:
    """Tracks an active cross-camera correlation."""

    group_name: str
    label: str
    first_camera: str
    event_id: str
    notification_tag: str
    timestamp: float
    cameras: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    event_ids: dict[str, str] = field(default_factory=dict)
    updated: bool = False

    def add_camera(
        self,
        camera: str,
        event_id: str,
        score: float = 0.0,
    ) -> None:
        """Register an additional camera hit."""
        if camera not in self.cameras:
            self.cameras.append(camera)
        self.scores[camera] = score
        self.event_ids[camera] = event_id
        self.updated = True

    @property
    def camera_count(self) -> int:
        return len(self.cameras)


class CrossCameraCorrelator:
    """In-memory correlator for cross-camera alert groups."""

    def __init__(self) -> None:
        # key: "{group_name}:{label}" -> CorrelationRecord
        self._active: dict[str, CorrelationRecord] = {}

    def _correlation_key(self, group_name: str, label: str) -> str:
        return f"{group_name}:{label}"

    def cleanup(self) -> None:
        """Remove stale correlation records."""
        now = time.time()
        expired = [
            k for k, v in self._active.items()
            if (now - v.timestamp) > _MAX_CORRELATION_TTL
        ]
        for k in expired:
            del self._active[k]

    def find_device_camera_group(
        self,
        camera: str,
        device_settings: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the enabled camera group a camera belongs to for a device."""
        groups: list[dict[str, Any]] = device_settings.get("camera_groups", [])
        for group in groups:
            if not group.get("enabled", True):
                continue
            if camera in group.get("cameras", []):
                return group
        return None

    def check_correlation(
        self,
        group_name: str,
        camera: str,
        label: str,
        event_id: str,
        score: float,
        time_window: int,
    ) -> tuple[bool, CorrelationRecord]:
        """Check if this event correlates with an existing one in the group.

        Returns (is_update, record).
        - is_update=False means this is the first camera in a new correlation.
        - is_update=True means another camera already fired and we should
          send an update notification.
        """
        self.cleanup()
        key = self._correlation_key(group_name, label)
        now = time.time()

        existing = self._active.get(key)
        if existing and (now - existing.timestamp) <= time_window:
            # Second+ camera in the same group within the window
            existing.add_camera(camera, event_id, score)
            return True, existing

        # First camera — create a new correlation record
        tag = f"xcam_{group_name}_{label}_{int(now)}"
        record = CorrelationRecord(
            group_name=group_name,
            label=label,
            first_camera=camera,
            event_id=event_id,
            notification_tag=tag,
            timestamp=now,
            cameras=[camera],
            scores={camera: score},
            event_ids={camera: event_id},
        )
        self._active[key] = record
        return False, record


async def compose_snapshot_image(
    session: Any,
    frigate_url: str,
    event_ids: dict[str, str],
    auth: tuple[str, str] | None = None,
    access_token: str | None = None,
) -> bytes | None:
    """Download snapshots for each camera and create a side-by-side composite.

    Falls back gracefully: if Pillow is unavailable or any download fails,
    returns None so the caller can use the single-camera snapshot instead.
    """
    try:
        from PIL import Image
    except ImportError:
        _LOGGER.debug("Pillow not available; skipping snapshot composition")
        return None

    headers: dict[str, str] = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    images: list[Image.Image] = []
    for camera_name, eid in event_ids.items():
        try:
            url = f"{frigate_url}/api/events/{eid}/snapshot.jpg"
            kwargs: dict[str, Any] = {"headers": headers, "timeout": 8, "ssl": False}
            async with session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Snapshot fetch failed for camera %s event %s: HTTP %d",
                        camera_name, eid, resp.status,
                    )
                    continue
                img_bytes = await resp.read()
                images.append(Image.open(io.BytesIO(img_bytes)))
        except Exception as err:
            _LOGGER.debug("Snapshot fetch error for %s: %s", camera_name, err)

    if len(images) < 2:
        return None

    # Build side-by-side composite
    # Normalize heights to the smallest image
    min_height = min(img.height for img in images)
    resized: list[Image.Image] = []
    for img in images:
        if img.height != min_height:
            ratio = min_height / img.height
            img = img.resize(
                (int(img.width * ratio), min_height),
                Image.LANCZOS,
            )
        resized.append(img)

    total_width = sum(img.width for img in resized)
    composite = Image.new("RGB", (total_width, min_height))
    x_offset = 0
    for img in resized:
        composite.paste(img, (x_offset, 0))
        x_offset += img.width

    buf = io.BytesIO()
    composite.save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return buf.getvalue()
