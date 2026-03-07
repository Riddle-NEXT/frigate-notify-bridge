"""Button entities for Frigate Notify Bridge per-device actions."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SIGNAL_DEVICE_REGISTERED,
    SIGNAL_DEVICE_REMOVED,
)
from .device_metadata import build_mobile_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-device button entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]
    coordinator = data["coordinator"]

    devices = await device_manager.async_get_devices()
    entities: list[ButtonEntity] = []
    for device_id, device in devices.items():
        entities.extend(
            _create_device_buttons(
                hass, entry, device_manager, coordinator, device_id, device
            )
        )
    async_add_entities(entities)

    @callback
    def _on_device_registered(device_id: str) -> None:
        async def _add() -> None:
            device = await device_manager.async_get_device(device_id)
            if device:
                async_add_entities(
                    _create_device_buttons(
                        hass, entry, device_manager, coordinator, device_id, device
                    )
                )

        hass.async_create_task(_add())

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REGISTERED, _on_device_registered)
    )


def _create_device_buttons(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_manager,
    coordinator,
    device_id: str,
    device: dict,
) -> list[ButtonEntity]:
    return [
        DeviceTestNotificationButton(
            hass, entry, device_manager, coordinator, device_id, device
        ),
        DeviceRemoveButton(hass, entry, device_manager, coordinator, device_id, device),
    ]


class _BaseDeviceButton(ButtonEntity):
    """Base class for per-device buttons."""

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_manager,
        coordinator,
        device_id: str,
        device: dict,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_manager = device_manager
        self._coordinator = coordinator
        self._device_id = device_id
        self._device = device

    @property
    def device_info(self) -> dict:
        return build_mobile_device_info(self._entry.entry_id, self._device_id, self._device)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DEVICE_REMOVED, self._handle_remove
            )
        )

    @callback
    def _handle_remove(self, device_id: str) -> None:
        if device_id == self._device_id:
            self.hass.async_create_task(self.async_remove())


class DeviceTestNotificationButton(_BaseDeviceButton):
    """Button that sends a test push notification to the device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, hass, entry, device_manager, coordinator, device_id, device
    ) -> None:
        super().__init__(hass, entry, device_manager, coordinator, device_id, device)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_test_notification"
        self._attr_name = "Test Notification"

    async def async_press(self) -> None:
        """Send a test notification to this device."""
        results = await self._coordinator.async_test_notification(self._device_id)
        if results:
            result = results[0]
            if result.success:
                _LOGGER.info(
                    "Test notification sent successfully to %s",
                    self._device.get("name"),
                )
            else:
                _LOGGER.warning(
                    "Test notification failed for %s: %s",
                    self._device.get("name"),
                    result.error,
                )
        else:
            _LOGGER.warning(
                "Test notification failed for %s: no token available",
                self._device.get("name"),
            )


class DeviceRemoveButton(_BaseDeviceButton):
    """Button that removes/unpairs the device from the bridge."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, hass, entry, device_manager, coordinator, device_id, device
    ) -> None:
        super().__init__(hass, entry, device_manager, coordinator, device_id, device)
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_remove_device"
        self._attr_name = "Remove Device"

    async def async_press(self) -> None:
        """Remove this device from the bridge."""
        success = await self._device_manager.async_remove_device(self._device_id)
        if success:
            _LOGGER.info("Device removed via HA button: %s", self._device.get("name"))
        else:
            _LOGGER.warning("Failed to remove device %s — not found", self._device_id)
