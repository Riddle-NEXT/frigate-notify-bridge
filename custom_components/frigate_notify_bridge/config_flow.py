"""Config flow for Frigate Notify Bridge integration."""

from __future__ import annotations

import base64
import logging
import os
import secrets
import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEBUG_LOGGING,
    DOMAIN,
    CONF_FRIGATE_URL,
    CONF_FRIGATE_USERNAME,
    CONF_FRIGATE_PASSWORD,
    CONF_HOME_SSIDS,
    CONF_MQTT_HOST,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_TOPIC_PREFIX,
    CONF_USE_HA_MQTT,
    CONF_USE_FRIGATE_INTEGRATION,
    CONF_PUSH_PROVIDER,
    CONF_RELAY_URL,
    CONF_RELAY_BRIDGE_ID,
    CONF_RELAY_BRIDGE_SECRET,
    CONF_RELAY_E2E_KEY,
    CONF_NTFY_URL,
    CONF_NTFY_TOPIC,
    CONF_NTFY_TOKEN,
    CONF_PUSHOVER_USER_KEY,
    CONF_PUSHOVER_API_TOKEN,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC_PREFIX,
    DEFAULT_NTFY_URL,
    DEFAULT_RELAY_URL,
    PUSH_PROVIDER_RELAY,
    PUSH_PROVIDER_NTFY,
    PUSH_PROVIDER_PUSHOVER,
    SIGNAL_DEVICE_UPDATED,
)

_LOGGER = logging.getLogger(__name__)


async def validate_frigate_connection(
    hass: HomeAssistant, url: str, username: str | None, password: str | None
) -> dict[str, Any]:
    """Validate Frigate connection and return info."""
    session = async_get_clientsession(hass)

    try:
        async with session.get(
            f"{url}/api/version",
            timeout=10,
            ssl=False,
        ) as response:
            if response.status == 200:
                version = await response.text()
                return {"version": version.strip(), "auth_required": False}
            elif response.status in (401, 302):
                return {"version": None, "auth_required": True}
            else:
                raise ConnectionError(f"Unexpected status: {response.status}")
    except Exception as e:
        raise ConnectionError(f"Could not connect to Frigate: {e}")


async def validate_frigate_auth(
    hass: HomeAssistant, url: str, username: str, password: str
) -> bool:
    """Validate Frigate credentials."""
    session = async_get_clientsession(hass)

    try:
        async with session.post(
            f"{url}/api/login",
            json={"user": username, "password": password},
            timeout=10,
            ssl=False,
        ) as response:
            return response.status == 200
    except Exception:
        return False


def check_frigate_integration(hass: HomeAssistant) -> dict[str, Any] | None:
    """Check if Frigate integration is configured and return its config."""
    frigate_entries = hass.config_entries.async_entries("frigate")
    if frigate_entries:
        entry = frigate_entries[0]
        return {
            "url": entry.data.get("url"),
            "entry_id": entry.entry_id,
        }
    return None


def check_mqtt_integration(hass: HomeAssistant) -> bool:
    """Check if MQTT integration is configured."""
    return "mqtt" in hass.config.components


# ── Config Flow ──────────────────────────────────────────────────────────────


class FrigateNotifyBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Frigate Notify Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._frigate_info: dict[str, Any] = {}
        self._has_frigate_integration: bool = False
        self._has_mqtt_integration: bool = False

    # ── Step: user ───────────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step — go straight to Frigate setup."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        self._has_frigate_integration = check_frigate_integration(self.hass) is not None
        self._has_mqtt_integration = check_mqtt_integration(self.hass)

        return await self.async_step_frigate_setup()

    # ── Step: frigate_setup ──────────────────────────────────────────────────

    async def async_step_frigate_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Frigate connection."""
        errors: dict[str, str] = {}

        frigate_config = check_frigate_integration(self.hass)
        has_mqtt = check_mqtt_integration(self.hass)

        if user_input is not None:
            use_frigate_integration = user_input.get(
                CONF_USE_FRIGATE_INTEGRATION, False
            )
            use_ha_mqtt = user_input.get(CONF_USE_HA_MQTT, False)

            if use_frigate_integration and frigate_config:
                self._data[CONF_FRIGATE_URL] = frigate_config["url"]
                self._data[CONF_USE_FRIGATE_INTEGRATION] = True
            else:
                frigate_url = user_input.get(CONF_FRIGATE_URL, "").rstrip("/")
                if not frigate_url:
                    errors[CONF_FRIGATE_URL] = "url_required"
                else:
                    try:
                        info = await validate_frigate_connection(
                            self.hass,
                            frigate_url,
                            user_input.get(CONF_FRIGATE_USERNAME),
                            user_input.get(CONF_FRIGATE_PASSWORD),
                        )
                        self._frigate_info = info
                        self._data[CONF_FRIGATE_URL] = frigate_url
                        self._data[CONF_USE_FRIGATE_INTEGRATION] = False

                        if info.get("auth_required"):
                            username = user_input.get(CONF_FRIGATE_USERNAME)
                            password = user_input.get(CONF_FRIGATE_PASSWORD)
                            if not username or not password:
                                errors["base"] = "auth_required"
                            elif not await validate_frigate_auth(
                                self.hass, frigate_url, username, password
                            ):
                                errors["base"] = "invalid_auth"
                            else:
                                self._data[CONF_FRIGATE_USERNAME] = username
                                self._data[CONF_FRIGATE_PASSWORD] = password

                    except ConnectionError:
                        errors[CONF_FRIGATE_URL] = "cannot_connect"

            if use_ha_mqtt and has_mqtt:
                self._data[CONF_USE_HA_MQTT] = True
            else:
                self._data[CONF_USE_HA_MQTT] = False

            if not errors:
                if not self._data.get(CONF_USE_HA_MQTT):
                    return await self.async_step_mqtt()
                return await self.async_step_push_provider()

        schema_dict: dict = {}

        if frigate_config:
            schema_dict[vol.Optional(CONF_USE_FRIGATE_INTEGRATION, default=True)] = (
                selector.BooleanSelector()
            )

        schema_dict[vol.Optional(CONF_FRIGATE_URL, default="")] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        )
        schema_dict[vol.Optional(CONF_FRIGATE_USERNAME)] = selector.TextSelector()
        schema_dict[vol.Optional(CONF_FRIGATE_PASSWORD)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )

        if has_mqtt:
            schema_dict[vol.Optional(CONF_USE_HA_MQTT, default=True)] = (
                selector.BooleanSelector()
            )

        return self.async_show_form(
            step_id="frigate_setup",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "frigate_url": (
                    frigate_config["url"] if frigate_config else "Not configured"
                ),
            },
        )

    # ── Step: mqtt ───────────────────────────────────────────────────────────

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure MQTT connection (if not using HA MQTT)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mqtt_host = user_input.get(CONF_MQTT_HOST)
            if not mqtt_host:
                errors[CONF_MQTT_HOST] = "host_required"
            else:
                self._data[CONF_MQTT_HOST] = mqtt_host
                self._data[CONF_MQTT_PORT] = user_input.get(
                    CONF_MQTT_PORT, DEFAULT_MQTT_PORT
                )
                self._data[CONF_MQTT_USERNAME] = user_input.get(CONF_MQTT_USERNAME)
                self._data[CONF_MQTT_PASSWORD] = user_input.get(CONF_MQTT_PASSWORD)
                self._data[CONF_MQTT_TOPIC_PREFIX] = user_input.get(
                    CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_TOPIC_PREFIX
                )

            if not errors:
                return await self.async_step_push_provider()

        return self.async_show_form(
            step_id="mqtt",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MQTT_HOST): selector.TextSelector(),
                    vol.Optional(
                        CONF_MQTT_PORT, default=DEFAULT_MQTT_PORT
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_MQTT_USERNAME): selector.TextSelector(),
                    vol.Optional(CONF_MQTT_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                    vol.Optional(
                        CONF_MQTT_TOPIC_PREFIX, default=DEFAULT_MQTT_TOPIC_PREFIX
                    ): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    # ── Step: push_provider ──────────────────────────────────────────────────

    async def async_step_push_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose push notification provider."""
        if user_input is not None:
            provider = user_input[CONF_PUSH_PROVIDER]
            if provider == PUSH_PROVIDER_RELAY:
                return await self.async_step_relay()
            elif provider == PUSH_PROVIDER_NTFY:
                return await self.async_step_ntfy()
            elif provider == PUSH_PROVIDER_PUSHOVER:
                return await self.async_step_pushover()

        return self.async_show_form(
            step_id="push_provider",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PUSH_PROVIDER, default=PUSH_PROVIDER_RELAY
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=PUSH_PROVIDER_RELAY,
                                    label="Frigate Mobile Relay (Recommended)",
                                ),
                                selector.SelectOptionDict(
                                    value=PUSH_PROVIDER_NTFY,
                                    label="ntfy (Manual setup required)",
                                ),
                                selector.SelectOptionDict(
                                    value=PUSH_PROVIDER_PUSHOVER,
                                    label="Pushover (Manual setup required)",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    # ── Step: relay ──────────────────────────────────────────────────────────

    async def async_step_relay(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure push relay — auto-generates credentials."""
        if user_input is not None:
            self._data[CONF_RELAY_URL] = user_input[CONF_RELAY_URL].rstrip("/")
            self._data[CONF_RELAY_BRIDGE_ID] = user_input[CONF_RELAY_BRIDGE_ID].strip()
            self._data[CONF_RELAY_BRIDGE_SECRET] = user_input[CONF_RELAY_BRIDGE_SECRET]
            self._data[CONF_RELAY_E2E_KEY] = user_input[CONF_RELAY_E2E_KEY]
            self._data[CONF_PUSH_PROVIDER] = PUSH_PROVIDER_RELAY
            return self.async_create_entry(
                title="Frigate Notify Bridge",
                data=self._data,
            )

        # Auto-generate credentials
        default_bridge_id = uuid.uuid4().hex[:16]
        default_bridge_secret = secrets.token_hex(32)
        # Generate 32 random bytes and encode as base64 for the e2e key
        default_e2e_key = base64.b64encode(os.urandom(32)).decode("ascii")

        return self.async_show_form(
            step_id="relay",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RELAY_URL, default=DEFAULT_RELAY_URL
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Required(
                        CONF_RELAY_BRIDGE_ID, default=default_bridge_id
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_RELAY_BRIDGE_SECRET, default=default_bridge_secret
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                    vol.Required(
                        CONF_RELAY_E2E_KEY, default=default_e2e_key
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            description_placeholders={
                "relay_url": DEFAULT_RELAY_URL,
            },
        )

    # ── Step: ntfy ───────────────────────────────────────────────────────────

    async def async_step_ntfy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure ntfy push notifications."""
        errors: dict[str, str] = {}

        if user_input is not None:
            topic = user_input.get(CONF_NTFY_TOPIC, "").strip()
            if not topic:
                errors[CONF_NTFY_TOPIC] = "topic_required"
            else:
                self._data[CONF_NTFY_URL] = user_input.get(
                    CONF_NTFY_URL, DEFAULT_NTFY_URL
                ).rstrip("/")
                self._data[CONF_NTFY_TOPIC] = topic
                self._data[CONF_NTFY_TOKEN] = user_input.get(CONF_NTFY_TOKEN)
                self._data[CONF_PUSH_PROVIDER] = PUSH_PROVIDER_NTFY
                return self.async_create_entry(
                    title="Frigate Notify Bridge",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="ntfy",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_NTFY_URL, default=DEFAULT_NTFY_URL
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                    vol.Required(CONF_NTFY_TOPIC): selector.TextSelector(),
                    vol.Optional(CONF_NTFY_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ── Step: pushover ───────────────────────────────────────────────────────

    async def async_step_pushover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Pushover push notifications."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_key = user_input.get(CONF_PUSHOVER_USER_KEY, "").strip()
            api_token = user_input.get(CONF_PUSHOVER_API_TOKEN, "").strip()
            if not user_key:
                errors[CONF_PUSHOVER_USER_KEY] = "user_key_required"
            elif not api_token:
                errors[CONF_PUSHOVER_API_TOKEN] = "api_token_required"
            else:
                self._data[CONF_PUSHOVER_USER_KEY] = user_key
                self._data[CONF_PUSHOVER_API_TOKEN] = api_token
                self._data[CONF_PUSH_PROVIDER] = PUSH_PROVIDER_PUSHOVER
                return self.async_create_entry(
                    title="Frigate Notify Bridge",
                    data=self._data,
                )

        return self.async_show_form(
            step_id="pushover",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PUSHOVER_USER_KEY): selector.TextSelector(),
                    vol.Required(CONF_PUSHOVER_API_TOKEN): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )

    # ── Options flow ─────────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return FrigateNotifyBridgeOptionsFlow()


class FrigateNotifyBridgeOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Frigate Notify Bridge."""

    def __init__(self) -> None:
        """Initialize options flow state."""
        self._active_pairing_info: dict[str, Any] | None = None
        self._selected_device_id: str | None = None

    def _merged_options(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge option updates without dropping unrelated saved values."""
        return {**self.config_entry.options, **updates}

    @staticmethod
    def _csv_string(values: list[str] | None) -> str:
        """Serialize a list of strings for a simple text field."""
        if not values:
            return ""
        return ", ".join(values)

    @staticmethod
    def _parse_csv_list(value: Any) -> list[str]:
        """Parse a comma-separated text field into a normalized string list."""
        if value is None:
            return []
        if isinstance(value, list):
            raw_values = value
        else:
            raw_values = str(value).split(",")

        normalized: list[str] = []
        for item in raw_values:
            text = str(item).strip()
            if text:
                normalized.append(text)
        return sorted(set(normalized))

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "connection_settings": "Connection Settings",
                "notification_settings": "Notification Settings",
                "device_notification_settings_select": "Device Notification Rules",
                "diagnostics": "Diagnostics",
                "device_management": "Manage Devices",
                "add_device": "Add Device",
            },
        )

    async def async_step_connection_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure connection settings (Frigate URL, home WiFi SSIDs)."""
        if user_input is not None:
            new_data = {**self.config_entry.data}
            if user_input.get(CONF_FRIGATE_URL):
                new_data[CONF_FRIGATE_URL] = user_input[CONF_FRIGATE_URL]
            new_data[CONF_HOME_SSIDS] = user_input.get(CONF_HOME_SSIDS, [])
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            # external_url goes into options (api.py reads from entry.options)
            external_url = user_input.get("external_url", "").strip()
            new_options = {**self.config_entry.options}
            if external_url:
                new_options["external_url"] = external_url
            else:
                new_options.pop("external_url", None)
            return self.async_create_entry(title="", data=new_options)

        current_url = self.config_entry.data.get(CONF_FRIGATE_URL, "")
        current_ssids = self.config_entry.data.get(CONF_HOME_SSIDS, [])
        current_external_url = self.config_entry.options.get("external_url", "")

        return self.async_show_form(
            step_id="connection_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FRIGATE_URL,
                        default=current_url,
                    ): selector.TextSelector(),
                    vol.Optional(
                        CONF_HOME_SSIDS,
                        default=current_ssids,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[],
                            custom_value=True,
                            multiple=True,
                        )
                    ),
                    vol.Optional(
                        "external_url",
                        default=current_external_url,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                }
            ),
        )

    async def async_step_notification_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure notification settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=self._merged_options(user_input),
            )

        return self.async_show_form(
            step_id="notification_settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "cooldown_seconds",
                        default=self.config_entry.options.get("cooldown_seconds", 60),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=3600,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        "include_thumbnail",
                        default=self.config_entry.options.get(
                            "include_thumbnail", True
                        ),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "include_snapshot",
                        default=self.config_entry.options.get(
                            "include_snapshot", False
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_device_notification_settings_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose which paired device to edit notification rules for."""
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        device_manager = data.get("device_manager")

        if device_manager is None:
            return self.async_abort(reason="not_configured")

        devices = await device_manager.async_get_devices()
        if not devices:
            return self.async_abort(reason="no_devices")

        if user_input is not None:
            self._selected_device_id = user_input.get("device_id")
            return await self.async_step_device_notification_settings()

        device_options = [
            selector.SelectOptionDict(
                value=device_id,
                label=f"{device['name']} ({device.get('platform', 'Unknown')})",
            )
            for device_id, device in sorted(
                devices.items(), key=lambda item: item[1].get("name", item[0]).lower()
            )
        ]

        return self.async_show_form(
            step_id="device_notification_settings_select",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=device_options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_device_notification_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit per-device notification rules."""
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        device_manager = data.get("device_manager")

        if device_manager is None:
            return self.async_abort(reason="not_configured")

        if not self._selected_device_id:
            return await self.async_step_device_notification_settings_select()

        device = await device_manager.async_get_device(self._selected_device_id)
        if not device:
            self._selected_device_id = None
            return self.async_abort(reason="no_devices")

        settings = device_manager.normalize_notification_settings(
            device.get("notification_settings")
        )

        if user_input is not None:
            updates = {
                "notification_settings": {
                    "enabled": user_input.get("enabled", True),
                    "event_kinds": user_input.get("event_kinds", ["alert"]),
                    "cameras": self._parse_csv_list(user_input.get("cameras")),
                    "excluded_cameras": self._parse_csv_list(user_input.get("excluded_cameras")),
                    "labels": self._parse_csv_list(user_input.get("labels")),
                    "excluded_labels": self._parse_csv_list(user_input.get("excluded_labels")),
                    "sub_labels": self._parse_csv_list(user_input.get("sub_labels")),
                    "excluded_sub_labels": self._parse_csv_list(user_input.get("excluded_sub_labels")),
                    "zones": self._parse_csv_list(user_input.get("zones")),
                    "excluded_zones": self._parse_csv_list(user_input.get("excluded_zones")),
                    "min_confidence": user_input.get("min_confidence", 0),
                    "cooldown_seconds": user_input.get("cooldown_seconds", 60),
                    "quiet_hours_start": user_input.get("quiet_hours_start"),
                    "quiet_hours_end": user_input.get("quiet_hours_end"),
                    "include_thumbnail": user_input.get("include_thumbnail", True),
                    "include_snapshot": user_input.get("include_snapshot", False),
                    "include_actions": user_input.get("include_actions", True),
                    "include_gif_preview": user_input.get("include_gif_preview", False),
                }
            }

            await device_manager.async_update_device(self._selected_device_id, updates)
            from homeassistant.helpers.dispatcher import async_dispatcher_send

            async_dispatcher_send(
                self.hass,
                SIGNAL_DEVICE_UPDATED,
                self._selected_device_id,
            )
            self._selected_device_id = None
            return self.async_create_entry(
                title="",
                data=dict(self.config_entry.options),
            )

        return self.async_show_form(
            step_id="device_notification_settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "enabled",
                        default=settings.get("enabled", True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "event_kinds",
                        default=settings.get("event_kinds", ["alert"]),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="alert",
                                    label="Alerts",
                                ),
                                selector.SelectOptionDict(
                                    value="detection",
                                    label="Detections",
                                ),
                                selector.SelectOptionDict(
                                    value="recording",
                                    label="Recordings",
                                ),
                            ],
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(
                        "cameras",
                        default=self._csv_string(settings.get("cameras")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "excluded_cameras",
                        default=self._csv_string(settings.get("excluded_cameras")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "labels",
                        default=self._csv_string(settings.get("labels")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "excluded_labels",
                        default=self._csv_string(settings.get("excluded_labels")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "sub_labels",
                        default=self._csv_string(settings.get("sub_labels")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "excluded_sub_labels",
                        default=self._csv_string(settings.get("excluded_sub_labels")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "zones",
                        default=self._csv_string(settings.get("zones")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "excluded_zones",
                        default=self._csv_string(settings.get("excluded_zones")),
                    ): selector.TextSelector(),
                    vol.Optional(
                        "min_confidence",
                        default=settings.get("min_confidence", 0),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=100,
                            mode=selector.NumberSelectorMode.SLIDER,
                        )
                    ),
                    vol.Optional(
                        "cooldown_seconds",
                        default=settings.get("cooldown_seconds", 60),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=3600,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        "quiet_hours_start",
                        default=settings.get("quiet_hours_start"),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=23,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        "quiet_hours_end",
                        default=settings.get("quiet_hours_end"),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=23,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(
                        "include_thumbnail",
                        default=settings.get("include_thumbnail", True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "include_snapshot",
                        default=settings.get("include_snapshot", False),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "include_actions",
                        default=settings.get("include_actions", True),
                    ): selector.BooleanSelector(),
                    vol.Optional(
                        "include_gif_preview",
                        default=settings.get("include_gif_preview", False),
                    ): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "device_name": device.get("name", self._selected_device_id),
            },
        )

    async def async_step_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure integration diagnostics options."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=self._merged_options(user_input),
            )

        return self.async_show_form(
            step_id="diagnostics",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DEBUG_LOGGING,
                        default=self.config_entry.options.get(
                            CONF_DEBUG_LOGGING, False
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_device_management(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage paired devices."""
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        device_manager = data.get("device_manager")

        if device_manager is None:
            return self.async_abort(reason="not_configured")

        devices = await device_manager.async_get_devices()

        if user_input is not None:
            devices_to_remove = user_input.get("remove_devices", [])
            for device_id in devices_to_remove:
                await device_manager.async_remove_device(device_id)
            return self.async_create_entry(
                title="",
                data=dict(self.config_entry.options),
            )

        if not devices:
            return self.async_abort(reason="no_devices")

        device_options = [
            selector.SelectOptionDict(
                value=device_id,
                label=(f"{device['name']} ({device.get('platform', 'Unknown')})"),
            )
            for device_id, device in devices.items()
        ]

        return self.async_show_form(
            step_id="device_management",
            data_schema=vol.Schema(
                {
                    vol.Optional("remove_devices"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=device_options,
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "device_count": str(len(devices)),
            },
        )

    async def async_step_add_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Generate a fresh pairing QR code and display it inline."""
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        device_manager = data.get("device_manager")
        coordinator = data.get("coordinator")

        if device_manager is None:
            return self.async_abort(reason="not_configured")

        if user_input is not None:
            # User clicked Submit (Done) — just close the flow
            self._active_pairing_info = None
            return self.async_create_entry(
                title="",
                data=dict(self.config_entry.options),
            )

        pairing_info = self._active_pairing_info
        if pairing_info is None:
            pairing_info = device_manager.generate_pairing_code()
            self._active_pairing_info = pairing_info
            _LOGGER.debug(
                "Generated new add-device pairing code=%s expires_at=%s",
                pairing_info["code"],
                pairing_info["expires_at"],
            )
        else:
            _LOGGER.debug(
                "Reusing active add-device pairing code=%s expires_at=%s",
                pairing_info["code"],
                pairing_info["expires_at"],
            )

        from .const import CONF_PUSH_PROVIDER, CONF_RELAY_URL, CONF_RELAY_E2E_KEY
        from .qr_generator import generate_pairing_qr_data, generate_qr_code_base64

        push_provider = self.config_entry.data.get(CONF_PUSH_PROVIDER)
        fcm_sender_id = None
        if coordinator and hasattr(coordinator, "push_provider"):
            fcm_sender_id = coordinator.push_provider.get_sender_id()

        custom_external_url = self.config_entry.options.get("external_url")
        use_cloud = self.config_entry.options.get("use_cloud_remote", True)
        relay_url = self.config_entry.data.get(CONF_RELAY_URL)
        e2e_key = self.config_entry.data.get(CONF_RELAY_E2E_KEY)

        qr_data = await generate_pairing_qr_data(
            hass=self.hass,
            pairing_info=pairing_info,
            frigate_url=self.config_entry.data.get(CONF_FRIGATE_URL),
            frigate_auth_required=bool(self.config_entry.data.get("frigate_username")),
            push_provider=push_provider,
            fcm_sender_id=fcm_sender_id,
            custom_external_url=custom_external_url,
            use_cloud_remote=use_cloud,
            relay_url=relay_url,
            e2e_key=e2e_key,
        )

        try:
            qr_b64 = await generate_qr_code_base64(qr_data, 300)
            qr_image_uri = f"data:image/png;base64,{qr_b64}"
        except Exception:
            qr_image_uri = ""

        from datetime import datetime, timezone

        expires_at = datetime.fromisoformat(pairing_info["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining_seconds = max(
            0,
            int((expires_at - datetime.now(timezone.utc)).total_seconds()),
        )

        # If the cached code expired while the dialog remained open, replace it.
        if remaining_seconds == 0:
            pairing_info = device_manager.generate_pairing_code()
            self._active_pairing_info = pairing_info
            expires_at = datetime.fromisoformat(pairing_info["expires_at"]).replace(
                tzinfo=timezone.utc
            )
            remaining_seconds = max(
                0,
                int((expires_at - datetime.now(timezone.utc)).total_seconds()),
            )
            _LOGGER.debug(
                "Regenerated expired add-device pairing code=%s expires_at=%s",
                pairing_info["code"],
                pairing_info["expires_at"],
            )

            qr_data = await generate_pairing_qr_data(
                hass=self.hass,
                pairing_info=pairing_info,
                frigate_url=self.config_entry.data.get(CONF_FRIGATE_URL),
                frigate_auth_required=bool(
                    self.config_entry.data.get("frigate_username")
                ),
                push_provider=push_provider,
                fcm_sender_id=fcm_sender_id,
                custom_external_url=custom_external_url,
                use_cloud_remote=use_cloud,
                relay_url=relay_url,
                e2e_key=e2e_key,
            )

            try:
                qr_b64 = await generate_qr_code_base64(qr_data, 300)
                qr_image_uri = f"data:image/png;base64,{qr_b64}"
            except Exception:
                qr_image_uri = ""

        expires_min = max(1, remaining_seconds // 60)

        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema({}),
            description_placeholders={
                "code": pairing_info["code"],
                "expires_in": str(expires_min),
                "qr_image": qr_image_uri,
            },
        )
