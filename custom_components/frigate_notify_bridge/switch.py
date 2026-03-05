"""Switch entities for Frigate Notify Bridge per-device push notification toggle."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
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
    """Set up per-device push notification switch entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]

    devices = await device_manager.async_get_devices()
    entities = [
        DevicePushEnabledSwitch(hass, entry, device_manager, device_id, device)
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
                        DevicePushEnabledSwitch(
                            hass, entry, device_manager, device_id, device
                        )
                    ]
                )

        hass.async_create_task(_add())

    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_REGISTERED, _on_device_registered)
    )


class DevicePushEnabledSwitch(SwitchEntity):
    """Switch that enables/disables push notifications for a paired device."""

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
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_push_enabled"
        self._attr_name = "Push Notifications"

    @property
    def device_info(self) -> dict:
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
        return self._device.get("notification_settings", {}).get("enabled", True)

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        device = await self._device_manager.async_update_device(
            self._device_id,
            {"notification_settings": {"enabled": enabled}},
        )
        if device:
            self._device = device
            self.async_write_ha_state()

        # Dispatch so other entities (e.g. the API layer) also get notified
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        async_dispatcher_send(self.hass, SIGNAL_DEVICE_UPDATED, self._device_id)

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
