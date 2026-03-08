"""Frigate Notify Bridge integration for Home Assistant.

This integration bridges Frigate NVR events to mobile push notifications,
supporting FCM, ntfy, and Pushover. It provides QR code pairing for the
Frigate Mobile app and can optionally leverage the existing Frigate integration.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEBUG_LOGGING,
    DOMAIN,
    PLATFORMS,
    CONF_USE_HA_MQTT,
    CONF_USE_FRIGATE_INTEGRATION,
    CONF_RELAY_URL,
    CONF_RELAY_BRIDGE_ID,
    CONF_RELAY_BRIDGE_SECRET,
    CONF_RELAY_E2E_KEY,
    DEFAULT_RELAY_URL,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import FrigateNotifyCoordinator
from .api import async_setup_api
from .issues import (
    BridgeIssueManager,
    ISSUE_PUSH_PROVIDER_UNAVAILABLE,
    ISSUE_NOTIFICATION_DELIVERY,
)
from .mqtt_listener import FrigateMQTTListener
from .device_manager import DeviceManager
from .push_providers import create_push_provider

_LOGGER = logging.getLogger(__name__)
_DOMAIN_LOGGER = logging.getLogger(f"custom_components.{DOMAIN}")


def _apply_runtime_logging(entry: ConfigEntry) -> None:
    """Apply runtime logging overrides from config entry options."""
    debug_enabled = bool(entry.options.get(CONF_DEBUG_LOGGING, False))
    _DOMAIN_LOGGER.setLevel(logging.DEBUG if debug_enabled else logging.NOTSET)
    _LOGGER.info(
        "Integration debug logging %s via options",
        "enabled" if debug_enabled else "disabled",
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Frigate Notify Bridge from YAML (not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Frigate Notify Bridge from a config entry."""
    _apply_runtime_logging(entry)
    _LOGGER.info("Setting up Frigate Notify Bridge integration")

    hass.data.setdefault(DOMAIN, {})
    issue_manager = BridgeIssueManager(hass)

    # Initialize storage for devices and settings
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load() or {
        "devices": {},
        "settings": {},
    }

    # Register with push relay if not already registered
    await _ensure_relay_registration(hass, entry)

    # Create push provider based on configuration
    provider_name = str(entry.data.get("push_provider", "unknown")).strip() or "unknown"
    try:
        push_provider = await create_push_provider(hass, entry.data)
    except Exception as err:
        reason = str(err) or "Push provider failed to initialize"
        await issue_manager.async_report_push_provider_unavailable(
            provider_name=provider_name,
            reason=reason,
        )
        _LOGGER.error("Failed to initialize push provider: %s", reason)
        return False
    await issue_manager.async_clear_issue(ISSUE_PUSH_PROVIDER_UNAVAILABLE)

    # Create device manager
    device_manager = DeviceManager(hass, store, stored_data.get("devices", {}))

    # Create coordinator
    coordinator = FrigateNotifyCoordinator(
        hass=hass,
        entry=entry,
        push_provider=push_provider,
        device_manager=device_manager,
        issue_manager=issue_manager,
    )

    # Set up MQTT listener
    mqtt_listener = FrigateMQTTListener(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
    )

    # Store references
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "mqtt_listener": mqtt_listener,
        "device_manager": device_manager,
        "push_provider": push_provider,
        "issue_manager": issue_manager,
        "store": store,
    }

    # Set up MQTT subscription
    await mqtt_listener.async_start()

    # Set up REST API endpoints
    await async_setup_api(hass, entry, coordinator, device_manager)

    # Register device in Home Assistant device registry
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Frigate Notify Bridge",
        manufacturer="Frigate Mobile",
        model="Notify Bridge",
        sw_version="0.8.0",
    )

    # Set up platforms (if any)
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.info("Frigate Notify Bridge setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Frigate Notify Bridge integration")

    # Unload platforms
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if not unload_ok:
            return False

    # Stop MQTT listener
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        mqtt_listener = data.get("mqtt_listener")
        if mqtt_listener:
            await mqtt_listener.async_stop()

        # Close push provider
        push_provider = data.get("push_provider")
        if push_provider:
            await push_provider.async_close()

        issue_manager = data.get("issue_manager")
        if issue_manager:
            await issue_manager.async_clear_issue(ISSUE_PUSH_PROVIDER_UNAVAILABLE)
            await issue_manager.async_clear_issue(ISSUE_NOTIFICATION_DELIVERY)

    # Remove stored data
    hass.data[DOMAIN].pop(entry.entry_id, None)

    return True


async def _ensure_relay_registration(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Verify relay is reachable and store relay_url in config entry data."""
    import base64
    import os

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    data = dict(entry.data)

    # Generate E2E key if missing
    if not data.get(CONF_RELAY_E2E_KEY):
        e2e_key = base64.b64encode(os.urandom(32)).decode()
        data[CONF_RELAY_E2E_KEY] = e2e_key
        _LOGGER.info("Generated E2E encryption key for push relay")

    # Determine relay URL (use stored value or fall back to default)
    relay_url = data.get(CONF_RELAY_URL) or DEFAULT_RELAY_URL
    data[CONF_RELAY_URL] = relay_url

    # Verify relay is reachable via health check
    try:
        session = async_get_clientsession(hass)
        async with session.get(
            f"{relay_url}/health",
            timeout=10,
        ) as resp:
            if resp.status == 200:
                _LOGGER.info("Push relay reachable at %s", relay_url)
            else:
                _LOGGER.warning(
                    "Push relay health check returned %d at %s",
                    resp.status,
                    relay_url,
                )
    except Exception as e:
        _LOGGER.warning("Push relay unreachable (%s), will use direct FCM", e)

    # Persist updated data (e2e key + relay_url) to config entry
    if data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=data)


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _apply_runtime_logging(entry)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        # Future migrations go here
        pass

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deletion of a paired mobile device from the HA device registry UI."""
    # Extract the device_id from the device's identifiers
    device_id = next(
        (ident[1] for ident in device_entry.identifiers if ident[0] == DOMAIN),
        None,
    )
    if device_id is None:
        return False

    # Skip the bridge's own device entry (identified by entry_id, not a mobile device_id)
    if device_id == config_entry.entry_id:
        return False

    data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if not data:
        return True  # Allow deletion even if data is already gone

    device_manager = data.get("device_manager")
    if device_manager:
        await device_manager.async_remove_device(device_id)

    return True
