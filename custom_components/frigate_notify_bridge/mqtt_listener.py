"""MQTT listener for Frigate events."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_MQTT_TOPIC_PREFIX,
    CONF_USE_HA_MQTT,
    CONF_MQTT_HOST,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_MQTT_PASSWORD,
    DEFAULT_MQTT_TOPIC_PREFIX,
    MQTT_EVENTS_TOPIC,
    MQTT_REVIEWS_TOPIC,
    NOTIFY_EVENT_TYPES,
)

_LOGGER = logging.getLogger(__name__)


class FrigateMQTTListener:
    """Listen to Frigate MQTT events and trigger notifications."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: Any,  # FrigateNotifyCoordinator
    ) -> None:
        """Initialize the MQTT listener."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._subscriptions: list[Callable] = []
        self._use_ha_mqtt = entry.data.get(CONF_USE_HA_MQTT, True)
        self._topic_prefix = entry.data.get(
            CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_TOPIC_PREFIX
        )
        self._external_client = None
        self._last_events: dict[str, datetime] = {}  # For cooldown tracking
        self._cleanup_unsub = None

    async def async_start(self) -> None:
        """Start listening for MQTT events."""
        if self._use_ha_mqtt:
            await self._subscribe_ha_mqtt()
        else:
            await self._start_external_mqtt()

        # Schedule periodic cleanup of old events
        self._cleanup_unsub = async_track_time_interval(
            self.hass,
            self._cleanup_old_events,
            timedelta(minutes=5),
        )

        _LOGGER.info("MQTT listener started (prefix: %s)", self._topic_prefix)

    async def async_stop(self) -> None:
        """Stop listening for MQTT events."""
        # Unsubscribe from HA MQTT
        for unsub in self._subscriptions:
            unsub()
        self._subscriptions.clear()

        # Stop external client if used
        if self._external_client:
            await self._stop_external_mqtt()

        # Cancel cleanup task
        if self._cleanup_unsub:
            self._cleanup_unsub()
            self._cleanup_unsub = None

        _LOGGER.info("MQTT listener stopped")

    async def _subscribe_ha_mqtt(self) -> None:
        """Subscribe using Home Assistant's MQTT integration."""
        # Subscribe to frigate/events
        events_topic = f"{self._topic_prefix}/{MQTT_EVENTS_TOPIC}"
        unsub = await mqtt.async_subscribe(
            self.hass,
            events_topic,
            self._handle_event_message,
            qos=1,
        )
        self._subscriptions.append(unsub)
        _LOGGER.debug("Subscribed to %s", events_topic)

        # Subscribe to frigate/reviews
        reviews_topic = f"{self._topic_prefix}/{MQTT_REVIEWS_TOPIC}"
        unsub = await mqtt.async_subscribe(
            self.hass,
            reviews_topic,
            self._handle_review_message,
            qos=1,
        )
        self._subscriptions.append(unsub)
        _LOGGER.debug("Subscribed to %s", reviews_topic)

    async def _start_external_mqtt(self) -> None:
        """Start external MQTT client (when not using HA MQTT)."""
        try:
            import paho.mqtt.client as mqtt_client
        except ImportError:
            _LOGGER.error("paho-mqtt not installed, cannot use external MQTT")
            return

        host = self.entry.data.get(CONF_MQTT_HOST)
        port = self.entry.data.get(CONF_MQTT_PORT, 1883)
        username = self.entry.data.get(CONF_MQTT_USERNAME)
        password = self.entry.data.get(CONF_MQTT_PASSWORD)

        if not host:
            _LOGGER.error("MQTT host not configured")
            return

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                _LOGGER.info("Connected to external MQTT broker")
                # Subscribe to topics
                client.subscribe(f"{self._topic_prefix}/{MQTT_EVENTS_TOPIC}", qos=1)
                client.subscribe(f"{self._topic_prefix}/{MQTT_REVIEWS_TOPIC}", qos=1)
            else:
                _LOGGER.error("Failed to connect to MQTT broker: %s", rc)

        def on_message(client, userdata, msg):
            # Route to appropriate handler
            topic = msg.topic
            if topic.endswith(MQTT_EVENTS_TOPIC):
                self.hass.loop.call_soon_threadsafe(
                    lambda: self.hass.async_create_task(
                        self._process_event_payload(msg.payload.decode())
                    )
                )
            elif topic.endswith(MQTT_REVIEWS_TOPIC):
                self.hass.loop.call_soon_threadsafe(
                    lambda: self.hass.async_create_task(
                        self._process_review_payload(msg.payload.decode())
                    )
                )

        client = mqtt_client.Client()
        if username:
            client.username_pw_set(username, password)

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect_async(host, port)
            client.loop_start()
            self._external_client = client
        except Exception as e:
            _LOGGER.error("Failed to connect to external MQTT: %s", e)

    async def _stop_external_mqtt(self) -> None:
        """Stop external MQTT client."""
        if self._external_client:
            self._external_client.loop_stop()
            self._external_client.disconnect()
            self._external_client = None

    @callback
    def _handle_event_message(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle incoming event message from HA MQTT."""
        self.hass.async_create_task(
            self._process_event_payload(msg.payload)
        )

    @callback
    def _handle_review_message(self, msg: mqtt.ReceiveMessage) -> None:
        """Handle incoming review message from HA MQTT."""
        self.hass.async_create_task(
            self._process_review_payload(msg.payload)
        )

    async def _process_event_payload(self, payload: str | bytes) -> None:
        """Process a Frigate event payload."""
        try:
            if isinstance(payload, bytes):
                payload = payload.decode()

            data = json.loads(payload)
            _LOGGER.debug("Received event: %s", data)

            # Extract event info
            event_type = data.get("type")
            before = data.get("before", {})
            after = data.get("after", {})

            # Use 'after' data if available, otherwise 'before'
            event_data = after if after else before

            if not event_data:
                return

            event_id = event_data.get("id")
            camera = event_data.get("camera")
            label = event_data.get("label")
            zones = event_data.get("current_zones", []) or event_data.get("entered_zones", [])
            score = event_data.get("score", 0)
            has_clip = event_data.get("has_clip", False)
            has_snapshot = event_data.get("has_snapshot", False)

            # Check if we should notify for this event type
            if event_type not in NOTIFY_EVENT_TYPES:
                return

            # For "update" events, only notify if this is a significant update
            # (e.g., new zones entered, higher score)
            if event_type == "update":
                # Skip minor updates to reduce notification spam
                before_zones = set(before.get("current_zones", []) or before.get("entered_zones", []))
                after_zones = set(zones)
                new_zones = after_zones - before_zones

                # Only notify on update if new zones were entered
                if not new_zones:
                    return

            # Check cooldown
            cooldown_key = f"{camera}:{label}"
            if not self._check_cooldown(cooldown_key):
                _LOGGER.debug("Skipping notification due to cooldown: %s", cooldown_key)
                return

            # Build notification data
            notification_data = {
                "event_id": event_id,
                "event_type": event_type,
                "camera": camera,
                "label": label,
                "zones": zones,
                "score": score,
                "has_clip": has_clip,
                "has_snapshot": has_snapshot,
                "timestamp": datetime.utcnow().isoformat(),
            }

            # Send to coordinator for processing
            await self.coordinator.async_handle_event(notification_data)

        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse event payload: %s", e)
        except Exception as e:
            _LOGGER.exception("Error processing event: %s", e)

    async def _process_review_payload(self, payload: str | bytes) -> None:
        """Process a Frigate review payload."""
        try:
            if isinstance(payload, bytes):
                payload = payload.decode()

            data = json.loads(payload)
            _LOGGER.debug("Received review: %s", data)

            # Reviews are aggregated events - we might want to handle these differently
            # For now, we'll skip reviews and rely on events
            # Future enhancement: use reviews for summarized notifications

        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse review payload: %s", e)
        except Exception as e:
            _LOGGER.exception("Error processing review: %s", e)

    def _check_cooldown(self, key: str, cooldown_seconds: int = 60) -> bool:
        """Check if we're within the cooldown period for a key.

        Returns True if we should send the notification, False if in cooldown.
        """
        now = datetime.utcnow()
        last_time = self._last_events.get(key)

        if last_time and (now - last_time).total_seconds() < cooldown_seconds:
            return False

        self._last_events[key] = now
        return True

    @callback
    def _cleanup_old_events(self, _: datetime) -> None:
        """Clean up old event timestamps."""
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=10)

        keys_to_remove = [
            key for key, timestamp in self._last_events.items()
            if timestamp < cutoff
        ]

        for key in keys_to_remove:
            del self._last_events[key]

        if keys_to_remove:
            _LOGGER.debug("Cleaned up %d old event entries", len(keys_to_remove))
