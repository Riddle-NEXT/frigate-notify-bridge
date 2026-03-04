"""QR code image entity for Frigate Notify Bridge pairing."""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_FRIGATE_URL, CONF_PUSH_PROVIDER
from .qr_generator import generate_pairing_qr_data, generate_qr_code_image

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the pairing QR code image entity."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_manager = data["device_manager"]
    coordinator = data["coordinator"]

    async_add_entities([
        PairingQRCodeImage(hass, entry, device_manager, coordinator),
    ])


class PairingQRCodeImage(ImageEntity):
    """Image entity that shows a fresh pairing QR code."""

    _attr_has_entity_name = True
    _attr_name = "Pairing QR Code"
    _attr_content_type = "image/png"

    def __init__(self, hass, entry, device_manager, coordinator):
        """Initialize the QR code image entity."""
        super().__init__(hass)
        self._entry = entry
        self._device_manager = device_manager
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_pairing_qr"

    @property
    def device_info(self):
        """Return device info to link to the bridge device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
        }

    async def async_image(self) -> bytes | None:
        """Generate a fresh pairing QR code image."""
        try:
            pairing_info = self._device_manager.generate_pairing_code()

            frigate_url = self._entry.data.get(CONF_FRIGATE_URL)
            push_provider = self._entry.data.get(CONF_PUSH_PROVIDER)
            fcm_sender_id = None
            if hasattr(self._coordinator, "push_provider"):
                fcm_sender_id = self._coordinator.push_provider.get_sender_id()

            custom_external_url = self._entry.options.get("external_url")
            use_cloud = self._entry.options.get("use_cloud_remote", True)

            qr_data = generate_pairing_qr_data(
                hass=self.hass,
                pairing_info=pairing_info,
                frigate_url=frigate_url,
                frigate_auth_required=bool(self._entry.data.get("frigate_username")),
                push_provider=push_provider,
                fcm_sender_id=fcm_sender_id,
                custom_external_url=custom_external_url,
                use_cloud_remote=use_cloud,
            )

            image_bytes = await generate_qr_code_image(qr_data, 400, "png")
            self._attr_image_last_updated = datetime.now()
            return image_bytes

        except Exception as exc:
            _LOGGER.error("Failed to generate pairing QR code: %s", exc)
            return None
