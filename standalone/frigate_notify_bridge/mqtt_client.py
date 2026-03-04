"""MQTT client for Frigate events in standalone mode."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Awaitable

import paho.mqtt.client as mqtt

from .config import Config

logger = logging.getLogger(__name__)


class FrigateMQTTClient:
    """MQTT client for receiving Frigate events."""

    def __init__(
        self,
        config: Config,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Initialize the MQTT client."""
        self.config = config
        self.on_event = on_event
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_events: dict[str, datetime] = {}
        self._cooldown_seconds = 60

    async def start(self) -> None:
        """Start the MQTT client."""
        self._loop = asyncio.get_event_loop()

        self._client = mqtt.Client(
            client_id="frigate-notify-bridge",
            protocol=mqtt.MQTTv311,
        )

        # Set credentials if provided
        if self.config.mqtt_username:
            self._client.username_pw_set(
                self.config.mqtt_username,
                self.config.mqtt_password,
            )

        # Set callbacks
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Connect
        try:
            self._client.connect_async(
                self.config.mqtt_host,
                self.config.mqtt_port,
            )
            self._client.loop_start()
            logger.info(
                "Connecting to MQTT broker at %s:%d",
                self.config.mqtt_host,
                self.config.mqtt_port,
            )
        except Exception as e:
            logger.error("Failed to connect to MQTT broker: %s", e)
            raise

    async def stop(self) -> None:
        """Stop the MQTT client."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("MQTT client disconnected")

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict,
        rc: int,
    ) -> None:
        """Handle MQTT connection."""
        if rc == 0:
            logger.info("Connected to MQTT broker")

            # Subscribe to Frigate topics
            prefix = self.config.mqtt_topic_prefix
            topics = [
                (f"{prefix}/events", 1),
                (f"{prefix}/reviews", 1),
            ]

            for topic, qos in topics:
                client.subscribe(topic, qos)
                logger.info("Subscribed to %s", topic)
        else:
            logger.error("MQTT connection failed with code: %d", rc)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        rc: int,
    ) -> None:
        """Handle MQTT disconnection."""
        if rc != 0:
            logger.warning("Unexpected MQTT disconnection (code: %d), reconnecting...", rc)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Handle incoming MQTT message."""
        try:
            # Parse payload
            payload = json.loads(msg.payload.decode())

            # Determine message type based on topic
            if msg.topic.endswith("/events"):
                self._handle_event(payload)
            elif msg.topic.endswith("/reviews"):
                self._handle_review(payload)

        except json.JSONDecodeError as e:
            logger.error("Failed to parse MQTT message: %s", e)
        except Exception as e:
            logger.exception("Error handling MQTT message: %s", e)

    def _handle_event(self, payload: dict[str, Any]) -> None:
        """Handle a Frigate event message."""
        event_type = payload.get("type")
        before = payload.get("before", {})
        after = payload.get("after", {})

        # Use 'after' data if available
        event_data = after if after else before
        if not event_data:
            return

        event_id = event_data.get("id")
        camera = event_data.get("camera")
        label = event_data.get("label")
        zones = event_data.get("current_zones", []) or event_data.get("entered_zones", [])
        score = event_data.get("score", 0)

        # Only process certain event types
        if event_type not in ("new", "update", "end"):
            return

        # For updates, only notify if new zones entered
        if event_type == "update":
            before_zones = set(before.get("current_zones", []) or before.get("entered_zones", []))
            after_zones = set(zones)
            if not (after_zones - before_zones):
                return

        # Check cooldown
        cooldown_key = f"{camera}:{label}"
        if not self._check_cooldown(cooldown_key):
            logger.debug("Skipping due to cooldown: %s", cooldown_key)
            return

        # Build event data for handler
        event = {
            "event_id": event_id,
            "event_type": event_type,
            "camera": camera,
            "label": label,
            "zones": zones,
            "score": score,
            "has_clip": event_data.get("has_clip", False),
            "has_snapshot": event_data.get("has_snapshot", False),
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Schedule async handler
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.on_event(event),
                self._loop,
            )

    def _handle_review(self, payload: dict[str, Any]) -> None:
        """Handle a Frigate review message."""
        # Reviews are aggregated events - skip for now
        # Could be used for summarized notifications in the future
        pass

    def _check_cooldown(self, key: str) -> bool:
        """Check if we're within cooldown period."""
        now = datetime.utcnow()
        last_time = self._last_events.get(key)

        if last_time and (now - last_time).total_seconds() < self._cooldown_seconds:
            return False

        self._last_events[key] = now

        # Clean up old entries
        cutoff = now - timedelta(minutes=10)
        self._last_events = {
            k: v for k, v in self._last_events.items()
            if v > cutoff
        }

        return True
