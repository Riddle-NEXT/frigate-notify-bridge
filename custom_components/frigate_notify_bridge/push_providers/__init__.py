"""Push notification providers for Frigate Notify Bridge."""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..const import (
    CONF_PUSH_PROVIDER,
    CONF_FCM_CREDENTIALS,
    CONF_NTFY_URL,
    CONF_NTFY_TOPIC,
    CONF_NTFY_TOKEN,
    CONF_PUSHOVER_USER_KEY,
    CONF_PUSHOVER_API_TOKEN,
    CONF_RELAY_URL,
    CONF_RELAY_BRIDGE_ID,
    CONF_RELAY_BRIDGE_SECRET,
    CONF_RELAY_E2E_KEY,
    PUSH_PROVIDER_FCM,
    PUSH_PROVIDER_NTFY,
    PUSH_PROVIDER_PUSHOVER,
    PUSH_PROVIDER_RELAY,
)
from .base import PushProvider
from .fcm import FCMProvider
from .ntfy import NtfyProvider
from .pushover import PushoverProvider
from .relay import RelayPushProvider

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "PushProvider",
    "FCMProvider",
    "NtfyProvider",
    "PushoverProvider",
    "RelayPushProvider",
    "create_push_provider",
]


async def create_push_provider(
    hass: HomeAssistant,
    config: dict[str, Any],
) -> PushProvider | None:
    """Create a push provider based on configuration.

    Args:
        hass: Home Assistant instance
        config: Configuration data from config entry

    Returns:
        Initialized push provider or None on failure
    """
    provider_type = config.get(CONF_PUSH_PROVIDER)

    if provider_type == PUSH_PROVIDER_FCM:
        credentials = config.get(CONF_FCM_CREDENTIALS)
        if not credentials:
            _LOGGER.error("FCM credentials not configured")
            return None

        provider = FCMProvider(hass, credentials)
        if await provider.async_initialize():
            return provider
        return None

    elif provider_type == PUSH_PROVIDER_NTFY:
        url = config.get(CONF_NTFY_URL)
        topic = config.get(CONF_NTFY_TOPIC)
        token = config.get(CONF_NTFY_TOKEN)

        if not topic:
            _LOGGER.error("ntfy topic not configured")
            return None

        provider = NtfyProvider(hass, url, topic, token)
        if await provider.async_initialize():
            return provider
        return None

    elif provider_type == PUSH_PROVIDER_PUSHOVER:
        user_key = config.get(CONF_PUSHOVER_USER_KEY)
        api_token = config.get(CONF_PUSHOVER_API_TOKEN)

        if not user_key or not api_token:
            _LOGGER.error("Pushover credentials not configured")
            return None

        provider = PushoverProvider(hass, user_key, api_token)
        if await provider.async_initialize():
            return provider
        return None

    elif provider_type == PUSH_PROVIDER_RELAY:
        import base64

        relay_url = config.get(CONF_RELAY_URL)
        bridge_id = config.get(CONF_RELAY_BRIDGE_ID)
        bridge_secret = config.get(CONF_RELAY_BRIDGE_SECRET)
        e2e_key_b64 = config.get(CONF_RELAY_E2E_KEY)

        if not all([relay_url, bridge_id, bridge_secret, e2e_key_b64]):
            _LOGGER.error("Relay push provider: missing configuration")
            return None

        e2e_key = base64.b64decode(e2e_key_b64)
        provider = RelayPushProvider(hass, relay_url, bridge_id, bridge_secret, e2e_key)
        if await provider.async_initialize():
            return provider
        return None

    else:
        _LOGGER.error("Unknown push provider: %s", provider_type)
        return None
