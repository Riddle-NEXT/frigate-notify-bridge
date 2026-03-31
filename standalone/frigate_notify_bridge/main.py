"""Frigate Notify Bridge - Standalone server main entry point."""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import Config, load_config
from .mqtt_client import FrigateMQTTClient
from .push_service import PushService
from .device_store import DeviceStore
from .issue_manager import StandaloneIssueManager
from .api import setup_routes

# Set up logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Failure threshold before raising an issue (failures today across all devices)
_FAILURE_THRESHOLD = 3


class FrigateNotifyBridge:
    """Main application class for standalone Frigate Notify Bridge."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.app: web.Application | None = None
        self.mqtt_client: FrigateMQTTClient | None = None
        self.push_service: PushService | None = None
        self.device_store: DeviceStore | None = None
        self.issue_manager: StandaloneIssueManager | None = None
        self._shutdown_event = asyncio.Event()
        self._last_event_at: str | None = None
        self._mqtt_connected: bool = False

    async def start(self) -> None:
        logger.info("Starting Frigate Notify Bridge v0.1.0")

        # Initialize subsystems
        self.device_store = DeviceStore(self.config.data_dir)
        await self.device_store.load()

        self.issue_manager = StandaloneIssueManager()

        self.push_service = PushService(self.config)
        if not await self.push_service.initialize():
            logger.error("Failed to initialize push service")
            sys.exit(1)

        self.mqtt_client = FrigateMQTTClient(
            config=self.config,
            on_event=self._handle_frigate_event,
        )

        # Build web app
        self.app = web.Application()
        self.app["config"] = self.config
        self.app["device_store"] = self.device_store
        self.app["push_service"] = self.push_service
        self.app["issue_manager"] = self.issue_manager
        self.app["mqtt_connected"] = False
        self.app["last_event_at"] = None

        setup_routes(self.app)

        # Start MQTT
        await self.mqtt_client.start()

        # Start web server
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.api_port)
        await site.start()

        logger.info("API server listening on port %d", self.config.api_port)
        logger.info("Frigate Notify Bridge started successfully")

        await self._shutdown_event.wait()
        await self.stop()

    async def stop(self) -> None:
        logger.info("Shutting down Frigate Notify Bridge")

        if self.mqtt_client:
            await self.mqtt_client.stop()

        if self.push_service:
            await self.push_service.close()

        if self.device_store:
            await self.device_store.save()

        logger.info("Frigate Notify Bridge stopped")

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    async def _handle_frigate_event(self, event_data: dict[str, Any]) -> None:
        """Handle a Frigate event from MQTT."""
        event_id = event_data.get("event_id")
        camera = event_data.get("camera")
        label = event_data.get("label")
        zones = event_data.get("zones", [])

        logger.debug("Processing event: %s (camera=%s, label=%s)", event_id, camera, label)

        # Track last event time for status endpoint
        self._last_event_at = datetime.utcnow().isoformat()
        if self.app:
            self.app["last_event_at"] = self._last_event_at

        # Get devices for this notification
        devices = await self.device_store.get_devices_for_notification(
            camera=camera,
            label=label,
            zone=zones[0] if zones else None,
        )

        if not devices:
            logger.debug("No devices to notify for event %s", event_id)
            return

        notification = self._build_notification(event_data)

        # Build (device_id, push_token) pairs so we can record delivery results per device
        device_pairs = [
            (device["id"], device.get("fcm_token"))
            for device in devices
            if device.get("fcm_token")
        ]

        if not device_pairs:
            logger.debug("No push tokens available for event %s", event_id)
            return

        logger.info("Sending notification to %d devices for event %s", len(device_pairs), event_id)

        # Send and record per-device results
        for device_id, push_token in device_pairs:
            result = await self.push_service.send(push_token, notification)
            await self.device_store.record_delivery_result(
                device_id=device_id,
                success=result.get("success", False),
                error=result.get("error"),
            )

        # Check failure state and raise/clear issues
        await self._check_delivery_health()

    async def _check_delivery_health(self) -> None:
        """Raise or clear delivery issues based on current failure counts."""
        devices = await self.device_store.get_all_devices()
        failing_devices = [
            d for d in devices.values()
            if d.get("failure_count_today", 0) >= _FAILURE_THRESHOLD
        ]

        if failing_devices:
            affected_names = [d.get("name", d["id"]) for d in failing_devices[:3]]
            count = len(failing_devices)
            self.issue_manager.raise_issue(
                issue_type="notification_delivery_failures",
                title=f"Notification delivery failures ({count} device{'s' if count > 1 else ''})",
                description=(
                    f"Delivery has failed {_FAILURE_THRESHOLD}+ times today for: "
                    + ", ".join(affected_names)
                ),
                severity="warning",
                affected_devices=affected_names,
                error_code="DELIVERY_FAILURE",
                error_detail=failing_devices[0].get("last_error"),
                suggested_action="Check push provider credentials or re-pair affected devices",
                fingerprint="notification_delivery_failures",
            )
        else:
            self.issue_manager.clear_issues_for_type("notification_delivery_failures")

    @staticmethod
    def _format_sub_label(raw: str) -> str:
        return raw.replace("_", " ").replace("-", " ").strip().title()

    @staticmethod
    def _is_modifier_sub_label(sub_label: str) -> bool:
        lower = sub_label.lower().strip()
        return lower.startswith("with") or lower in {"package", "bicycle", "pet", "vehicle"}

    def _build_notification(self, event_data: dict[str, Any]) -> dict[str, Any]:
        event_id = event_data.get("event_id")
        camera = event_data.get("camera", "Unknown")
        label = event_data.get("label", "object")
        sub_label = event_data.get("sub_label")
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)

        display_label = label.title()
        sub_label_is_identity = False
        if sub_label and str(sub_label).strip():
            cleaned_sub = str(sub_label).strip()
            if self._is_modifier_sub_label(cleaned_sub):
                display_label = f"{display_label} {self._format_sub_label(cleaned_sub)}"
            else:
                sub_label_is_identity = True
                display_label = self._format_sub_label(cleaned_sub)

        title = f"{display_label} on {camera}" if camera else f"{display_label} detected"

        body_parts = []
        if sub_label_is_identity:
            body_parts.append(label.title())
        if score:
            body_parts.append(f"Confidence: {int(score * 100)}%")
        if zones:
            body_parts.append(f"Zone: {', '.join(zones)}")

        body = " · ".join(body_parts) if body_parts else f"Motion detected on {camera}"

        thumbnail_url = None
        if self.config.frigate_url and event_id:
            thumbnail_url = f"{self.config.frigate_url}/api/events/{event_id}/thumbnail.jpg"

        return {
            "title": title,
            "body": body,
            "data": {
                "type": "frigate_event",
                "event_id": event_id,
                "camera": camera,
                "label": label,
                "zones": ",".join(zones) if zones else "",
                "frigate_url": self.config.frigate_url or "",
            },
            "image_url": thumbnail_url,
            "priority": "high",
        }


def main() -> None:
    config = load_config()
    bridge = FrigateNotifyBridge(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge.request_shutdown)

    try:
        loop.run_until_complete(bridge.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
