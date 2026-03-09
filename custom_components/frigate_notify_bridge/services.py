"""Service handlers for Frigate Notify Bridge."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import FrigateNotifyCoordinator
from .device_manager import DeviceManager

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_SEND_TEST_NOTIFICATION = "send_test_notification"

# Service schemas
SERVICE_SEND_TEST_NOTIFICATION_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("image_type", default="thumbnail"): vol.In(
            ["gif", "snapshot", "thumbnail", "none"]
        ),
        vol.Optional("use_recent_event", default=True): cv.boolean,
    }
)

# Rate limiting: 5 tests per device per hour
_rate_limit_window = timedelta(hours=1)
_rate_limit_max = 5
_test_history: dict[str, list[datetime]] = {}


def async_setup_services(
    hass: HomeAssistant,
    coordinator: FrigateNotifyCoordinator,
    device_manager: DeviceManager,
) -> None:
    """Set up services for Frigate Notify Bridge."""

    async def async_handle_send_test_notification(call: ServiceCall) -> None:
        """Handle send_test_notification service call."""
        device_id = call.data["device_id"]
        image_type = call.data["image_type"]
        use_recent_event = call.data["use_recent_event"]

        _LOGGER.info(
            "Test notification requested: device=%s, image_type=%s, use_recent=%s",
            device_id,
            image_type,
            use_recent_event,
        )

        # Check rate limiting
        now = datetime.now()
        if device_id not in _test_history:
            _test_history[device_id] = []

        # Clean up old entries outside the rate limit window
        _test_history[device_id] = [
            ts for ts in _test_history[device_id] if now - ts < _rate_limit_window
        ]

        if len(_test_history[device_id]) >= _rate_limit_max:
            oldest = _test_history[device_id][0]
            wait_time = (_rate_limit_window - (now - oldest)).total_seconds()
            _LOGGER.warning(
                "Rate limit exceeded for device %s - %d tests in last hour. Try again in %d seconds.",
                device_id,
                len(_test_history[device_id]),
                int(wait_time),
            )
            raise ValueError(
                f"Rate limit exceeded: max {_rate_limit_max} tests per hour. "
                f"Try again in {int(wait_time)} seconds."
            )

        # Get device settings
        device = device_manager.get_device(device_id)
        if not device:
            _LOGGER.error("Device not found: %s", device_id)
            raise ValueError(f"Device not found: {device_id}")

        settings = device.get("settings", {})

        # Get recent event if requested
        event_id = None
        event_data = None
        if use_recent_event:
            # Try to get a recent event from Frigate API
            frigate_url = coordinator.entry.data.get("frigate_url")
            _LOGGER.info("Fetching recent event from Frigate for test notification (url=%s)", frigate_url)
            if frigate_url:
                try:
                    from homeassistant.helpers.aiohttp_client import async_get_clientsession
                    session = async_get_clientsession(hass)

                    # Get the most recent event with a snapshot
                    api_url = f"{frigate_url}/api/events?limit=1&has_snapshot=1"
                    _LOGGER.debug("Calling Frigate API: %s", api_url)
                    async with session.get(
                        api_url,
                        timeout=10,
                        ssl=False,
                    ) as resp:
                        if resp.status == 200:
                            events = await resp.json()
                            _LOGGER.info("Frigate API returned %d events", len(events) if events else 0)
                            if events and len(events) > 0:
                                event_data = events[0]
                                event_id = event_data.get("id")
                                _LOGGER.info(
                                    "Using recent event %s (camera=%s, label=%s) for test notification",
                                    event_id,
                                    event_data.get("camera"),
                                    event_data.get("label")
                                )
                            else:
                                _LOGGER.warning("No recent events found in Frigate with snapshots")
                        else:
                            _LOGGER.warning("Frigate API returned status %d", resp.status)
                except Exception as err:
                    _LOGGER.error("Failed to fetch recent event from Frigate: %s", err, exc_info=True)
            else:
                _LOGGER.warning("No Frigate URL configured - cannot fetch recent events")

        # Build test notification payload
        from .coordinator import NotificationPayload

        # Override image preferences based on requested type
        test_settings = dict(settings)
        test_settings["include_gif_preview"] = image_type == "gif"
        test_settings["include_snapshot"] = image_type == "snapshot"
        test_settings["include_thumbnail"] = image_type == "thumbnail"

        # If none, disable all images
        if image_type == "none":
            test_settings["include_gif_preview"] = False
            test_settings["include_snapshot"] = False
            test_settings["include_thumbnail"] = False

        # Create test payload
        if event_data and event_id:
            # Use real event data
            title = f"🧪 Test: {event_data.get('label', 'Unknown')} detected"
            body = f"Camera: {event_data.get('camera', 'Unknown')}"
            payload_event_id = event_id
            camera = event_data.get("camera")
            label = event_data.get("label")
        else:
            # Use mock data
            title = "🧪 Test Notification"
            body = "This is a test notification from Frigate Notify Bridge"
            payload_event_id = "test-" + datetime.now().strftime("%Y%m%d-%H%M%S")
            camera = "test_camera"
            label = "person"

        # Build media URL if requested
        image_url = None
        if image_type != "none":
            if event_id:
                # We have a real event - build the image URL
                if image_type == "gif":
                    image_url = coordinator._build_media_url(
                        device, "event_preview_gif", event_id
                    )
                elif image_type == "snapshot":
                    image_url = coordinator._build_media_url(
                        device, "event_snapshot", event_id
                    )
                elif image_type == "thumbnail":
                    image_url = coordinator._build_media_url(
                        device, "event_thumbnail", event_id
                    )
                _LOGGER.info("Test notification with %s image from event %s", image_type, event_id)
            else:
                # No recent event found - log warning
                _LOGGER.warning(
                    "Test notification requested with %s image but no recent Frigate event found. "
                    "Create some events in Frigate first, or disable 'use_recent_event'.",
                    image_type
                )

        payload = NotificationPayload(
            title=title,
            body=body,
            data={"type": "frigate_test"},
            image_url=image_url,
            thumbnail_url=None,
            priority="high",
            event_id=payload_event_id,
            camera=camera,
            label=label,
            zones=[],
        )

        # Send notification
        try:
            await coordinator._send_notification(device_id, payload, test_settings)
            _LOGGER.info("Test notification sent successfully to device %s", device_id)

            # Record successful test for rate limiting
            _test_history[device_id].append(now)

        except Exception as err:
            _LOGGER.error(
                "Failed to send test notification to device %s: %s",
                device_id,
                err,
            )
            raise

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_TEST_NOTIFICATION,
        async_handle_send_test_notification,
        schema=SERVICE_SEND_TEST_NOTIFICATION_SCHEMA,
    )

    _LOGGER.info("Registered Frigate Notify Bridge services")


def async_unload_services(hass: HomeAssistant) -> None:
    """Unload services for Frigate Notify Bridge."""
    hass.services.async_remove(DOMAIN, SERVICE_SEND_TEST_NOTIFICATION)
    _LOGGER.info("Unloaded Frigate Notify Bridge services")
