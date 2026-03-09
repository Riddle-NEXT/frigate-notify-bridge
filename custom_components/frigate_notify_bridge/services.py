"""Service handlers for Frigate Notify Bridge."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
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
        device = await device_manager.async_get_device(device_id)
        if not device:
            _LOGGER.error("Device not found: %s", device_id)
            raise ValueError(f"Device not found: {device_id}")

        # Send notification
        try:
            _, metadata = await coordinator.build_test_notification_payload(
                device,
                image_type=image_type,
                use_recent_event=use_recent_event,
            )
            _LOGGER.info(
                "Prepared test notification for %s using source=%s image_type=%s has_image=%s",
                device_id,
                metadata.get("source"),
                metadata.get("image_type"),
                metadata.get("has_image"),
            )
            result = await coordinator.async_test_notification(
                device_id,
                image_type=image_type,
                use_recent_event=use_recent_event,
            )
            if not result or not result[0].success:
                error = result[0].error if result else "No push token available for device"
                raise ValueError(
                    json.dumps(
                        {
                            "error": error,
                            "source": metadata.get("source"),
                            "has_image": metadata.get("has_image"),
                        }
                    )
                )
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
