"""Sensors for Frigate Notify Bridge per-device statistics."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SIGNAL_DEVICE_REGISTERED,
    SIGNAL_DEVICE_REMOVED,
    SIGNAL_DEVICE_UPDATED,
)
from .device_metadata import build_mobile_device_info, get_device_auth_mode

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-device sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]

    devices = await device_manager.async_get_devices()
    entities: list[SensorEntity] = []
    for device_id, device in devices.items():
        entities.extend(
            _create_device_sensors(hass, entry, device_manager, device_id, device)
        )
    async_add_entities(entities)

    @callback
    def _on_device_registered(device_id: str) -> None:
        async def _add() -> None:
            device = await device_manager.async_get_device(device_id)
            if device:
                async_add_entities(
                    _create_device_sensors(
                        hass, entry, device_manager, device_id, device
                    )
                )

        hass.async_create_task(_add())

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REGISTERED, _on_device_registered)
    )


def _create_device_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_manager,
    device_id: str,
    device: dict,
) -> list[SensorEntity]:
    return [
        DeviceLastSeenSensor(hass, entry, device_manager, device_id, device),
        DeviceAlertsTodaySensor(hass, entry, device_manager, device_id, device),
        DeviceTotalAlertsSensor(hass, entry, device_manager, device_id, device),
    ]


class _BaseDeviceSensor(SensorEntity):
    """Base class for per-device sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_manager,
        device_id: str,
        device: dict,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_manager = device_manager
        self._device_id = device_id
        self._device = device

    @property
    def device_info(self) -> dict:
        return build_mobile_device_info(self._entry.entry_id, self._device_id, self._device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "auth_mode": get_device_auth_mode(self._device),
            "ha_user_id": self._device.get("ha_user_id"),
            "mobile_app_device_id": self._device.get("mobile_app_device_id"),
            "mobile_app_webhook_registered": bool(
                self._device.get("mobile_app_webhook_id")
            ),
            "relay_device_id": self._device.get("relay_device_id"),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DEVICE_UPDATED, self._handle_update
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DEVICE_REMOVED, self._handle_remove
            )
        )

    @callback
    def _handle_update(self, device_id: str) -> None:
        if device_id != self._device_id:
            return

        async def _refresh() -> None:
            device = await self._device_manager.async_get_device(device_id)
            if device:
                self._device = device
                self.async_write_ha_state()

        self.hass.async_create_task(_refresh())

    @callback
    def _handle_remove(self, device_id: str) -> None:
        if device_id == self._device_id:
            self.hass.async_create_task(self.async_remove())


class DeviceLastSeenSensor(_BaseDeviceSensor):
    """Sensor showing the last time the device made an API call."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, entry, device_manager, device_id, device) -> None:
        super().__init__(hass, entry, device_manager, device_id, device)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_last_seen"
        self._attr_name = "Last Seen"

    @property
    def native_value(self) -> Any:
        last_seen = self._device.get("last_seen")
        if not last_seen:
            return None
        try:
            dt = datetime.fromisoformat(last_seen)
            # Ensure timezone-aware for HA timestamp sensor
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None


class DeviceAlertsTodaySensor(_BaseDeviceSensor):
    """Sensor showing the number of alerts sent today."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "alerts"

    def __init__(self, hass, entry, device_manager, device_id, device) -> None:
        super().__init__(hass, entry, device_manager, device_id, device)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_alerts_today"
        self._attr_name = "Alerts Today"

    @property
    def native_value(self) -> int:
        return self._device.get("alert_count_today", 0)


class DeviceTotalAlertsSensor(_BaseDeviceSensor):
    """Sensor showing the lifetime total alerts sent to the device."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "alerts"

    def __init__(self, hass, entry, device_manager, device_id, device) -> None:
        super().__init__(hass, entry, device_manager, device_id, device)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_alerts_total"
        self._attr_name = "Total Alerts"

    @property
    def native_value(self) -> int:
        return self._device.get("alert_count_total", 0)
