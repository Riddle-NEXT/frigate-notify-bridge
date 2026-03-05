"""Binary sensors for Frigate Notify Bridge per-device status."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    DEVICE_ONLINE_THRESHOLD_MINUTES,
    SIGNAL_DEVICE_REGISTERED,
    SIGNAL_DEVICE_REMOVED,
    SIGNAL_DEVICE_UPDATED,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-device connection status binary sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]

    devices = await device_manager.async_get_devices()
    entities = [
        DeviceConnectionStatusEntity(hass, entry, device_manager, device_id, device)
        for device_id, device in devices.items()
    ]
    async_add_entities(entities)

    @callback
    def _on_device_registered(device_id: str) -> None:
        async def _add() -> None:
            device = await device_manager.async_get_device(device_id)
            if device:
                async_add_entities(
                    [
                        DeviceConnectionStatusEntity(
                            hass, entry, device_manager, device_id, device
                        )
                    ]
                )

        hass.async_create_task(_add())

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REGISTERED, _on_device_registered)
    )


class DeviceConnectionStatusEntity(BinarySensorEntity):
    """Binary sensor showing whether a paired device is online."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_manager,
        device_id: str,
        device: dict,
    ) -> None:
        """Initialize the entity."""
        self.hass = hass
        self._entry = entry
        self._device_manager = device_manager
        self._device_id = device_id
        self._device = device
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_connection_status"
        self._attr_name = "Connection Status"

    @property
    def device_info(self) -> dict:
        """Return device info linking this entity to the per-device HA device."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device.get("name", "Unknown Device"),
            "manufacturer": "Frigate Mobile",
            "model": self._device.get("platform", "mobile").title(),
            "sw_version": self._device.get("app_version"),
            "via_device": (DOMAIN, self._entry.entry_id),
        }

    @property
    def is_on(self) -> bool:
        """Return True if the device was seen within the online threshold."""
        last_seen = self._device.get("last_seen")
        if not last_seen:
            return False
        try:
            dt = datetime.fromisoformat(last_seen)
            return datetime.utcnow() - dt < timedelta(
                minutes=DEVICE_ONLINE_THRESHOLD_MINUTES
            )
        except Exception:
            return False

    async def async_added_to_hass(self) -> None:
        """Register dispatcher callbacks when added to hass."""
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
