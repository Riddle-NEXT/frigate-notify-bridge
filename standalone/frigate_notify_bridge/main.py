"""Frigate Notify Bridge - Standalone server main entry point."""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import Config, load_config
from .mqtt_client import FrigateMQTTClient
from .push_service import PushService
from .device_store import DeviceStore
from .api import setup_routes

# Set up logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class FrigateNotifyBridge:
    """Main application class for standalone Frigate Notify Bridge."""

    def __init__(self, config: Config) -> None:
        """Initialize the bridge."""
        self.config = config
        self.app: web.Application | None = None
        self.mqtt_client: FrigateMQTTClient | None = None
        self.push_service: PushService | None = None
        self.device_store: DeviceStore | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the bridge."""
        logger.info("Starting Frigate Notify Bridge v0.1.0")

        # Initialize device store
        self.device_store = DeviceStore(self.config.data_dir)
        await self.device_store.load()

        # Initialize push service
        self.push_service = PushService(self.config)
        if not await self.push_service.initialize():
            logger.error("Failed to initialize push service")
            sys.exit(1)

        # Initialize MQTT client
        self.mqtt_client = FrigateMQTTClient(
            config=self.config,
            on_event=self._handle_frigate_event,
        )

        # Create web application
        self.app = web.Application()
        self.app["config"] = self.config
        self.app["device_store"] = self.device_store
        self.app["push_service"] = self.push_service

        # Set up API routes
        setup_routes(self.app)

        # Start MQTT client
        await self.mqtt_client.start()

        # Start web server
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.api_port)
        await site.start()

        logger.info("API server listening on port %d", self.config.api_port)
        logger.info("Frigate Notify Bridge started successfully")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        # Cleanup
        await self.stop()

    async def stop(self) -> None:
        """Stop the bridge."""
        logger.info("Shutting down Frigate Notify Bridge")

        if self.mqtt_client:
            await self.mqtt_client.stop()

        if self.push_service:
            await self.push_service.close()

        if self.device_store:
            await self.device_store.save()

        logger.info("Frigate Notify Bridge stopped")

    def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        self._shutdown_event.set()

    async def _handle_frigate_event(self, event_data: dict[str, Any]) -> None:
        """Handle a Frigate event from MQTT."""
        event_id = event_data.get("event_id")
        camera = event_data.get("camera")
        label = event_data.get("label")
        zones = event_data.get("zones", [])

        logger.debug(
            "Processing event: %s (camera=%s, label=%s)",
            event_id,
            camera,
            label,
        )

        # Get devices that should receive this notification
        devices = await self.device_store.get_devices_for_notification(
            camera=camera,
            label=label,
            zone=zones[0] if zones else None,
        )

        if not devices:
            logger.debug("No devices to notify for event %s", event_id)
            return

        # Build notification
        notification = self._build_notification(event_data)

        # Get FCM tokens
        fcm_tokens = [
            device.get("fcm_token")
            for device in devices
            if device.get("fcm_token")
        ]

        if not fcm_tokens:
            logger.debug("No FCM tokens available")
            return

        # Send notifications
        logger.info(
            "Sending notification to %d devices for event %s",
            len(fcm_tokens),
            event_id,
        )

        results = await self.push_service.send_to_many(fcm_tokens, notification)

        success_count = sum(1 for r in results if r.get("success"))
        logger.info(
            "Notification results: %d success, %d failure",
            success_count,
            len(results) - success_count,
        )

    def _build_notification(self, event_data: dict[str, Any]) -> dict[str, Any]:
        """Build notification payload from event data."""
        event_id = event_data.get("event_id")
        camera = event_data.get("camera", "Unknown")
        label = event_data.get("label", "object")
        zones = event_data.get("zones", [])
        score = event_data.get("score", 0)

        title = f"{label.title()} on {camera}" if camera else f"{label.title()} detected"

        body_parts = []
        if score:
            body_parts.append(f"Confidence: {int(score * 100)}%")
        if zones:
            body_parts.append(f"Zone: {', '.join(zones)}")

        body = " · ".join(body_parts) if body_parts else f"Motion detected on {camera}"

        # Build image URLs
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
    """Main entry point."""
    # Load configuration
    config = load_config()

    # Create bridge
    bridge = FrigateNotifyBridge(config)

    # Set up signal handlers
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
