"""Config flow for Frigate Notify Bridge integration."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import uuid
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.hdrs import METH_GET
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
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
    CONF_FCM_CREDENTIALS,
    CONF_FCM_PROJECT_ID,
    CONF_FCM_SETUP_METHOD,
    CONF_FIREBASE_PROJECT,
    CONF_NTFY_URL,
    CONF_NTFY_TOPIC,
    CONF_NTFY_TOKEN,
    CONF_PUSHOVER_USER_KEY,
    CONF_PUSHOVER_API_TOKEN,
    PUSH_PROVIDER_FCM,
    PUSH_PROVIDER_NTFY,
    PUSH_PROVIDER_PUSHOVER,
    FCM_SETUP_OAUTH,
    FCM_SETUP_MANUAL,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC_PREFIX,
    DEFAULT_NTFY_URL,
    GOOGLE_AUTH_URL,
    GOOGLE_OAUTH_REDIRECT_URI,
    GOOGLE_OAUTH_SCOPES,
    GOOGLE_REVOKE_URL,
    GOOGLE_CLIENT_ID,
    GCP_CREATE_PROJECT_URL,
    GCP_GET_PROJECT_URL,
    GCP_OPERATIONS_URL,
    FIREBASE_ADD_URL,
    FIREBASE_LIST_PROJECTS_URL,
    FIREBASE_OPERATIONS_URL,
    IAM_CREATE_SA_URL,
    IAM_CREATE_KEY_URL,
    IAM_SA_EMAIL_TEMPLATE,
    SERVICE_USAGE_ENABLE_URL,
    FCM_SERVICE_NAME,
    IAM_SERVICE_NAME,
    FCM_SA_NAME,
    FCM_SA_DISPLAY_NAME,
    CONF_FIREBASE_CLIENT_CONFIG,
    FIREBASE_WEB_APPS_URL,
    FIREBASE_WEB_APP_CONFIG_URL,
    CONF_RELAY_URL,
    CONF_RELAY_BRIDGE_ID,
    CONF_RELAY_BRIDGE_SECRET,
    CONF_RELAY_E2E_KEY,
    DEFAULT_RELAY_URL,
)

_LOGGER = logging.getLogger(__name__)

_CREATE_NEW = "__create_new__"

# Status-code → error-key suffix mapping
_ERROR_SUFFIX_MAP: dict[int, str] = {
    403: "_permission_denied",
    429: "_quota_exceeded",
}


class ConfigureError(Exception):
    """Raised when a Firebase configuration step fails."""

    def __init__(self, step: str, status: int, detail: str) -> None:
        self.step = step
        self.status = status
        self.detail = detail
        super().__init__(f"{step} failed ({status}): {detail}")

    @property
    def error_key(self) -> str:
        """Map status + step to a strings.json error key."""
        if self.status in _ERROR_SUFFIX_MAP:
            return f"fcm_{self.step}{_ERROR_SUFFIX_MAP[self.status]}"
        if 500 <= self.status < 600:
            return "fcm_server_error"
        return f"fcm_{self.step}_failed"


# ── Helpers ──────────────────────────────────────────────────────────

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


# ── OAuth Webhook Callback ───────────────────────────────────────────


async def _async_handle_oauth_webhook(
    hass: HomeAssistant,
    webhook_id: str,
    request: web.Request,
) -> web.Response | None:
    """Receive auth code from relay page via webhook (works through Nabu Casa)."""
    code = request.query.get("code")
    state = request.query.get("state")

    if not code or not state:
        _LOGGER.error("OAuth webhook missing code or state parameter")
        return web.Response(
            text="<html><body><h1>Error</h1>"
            "<p>Missing parameters. Please try again.</p>"
            "</body></html>",
            content_type="text/html",
            status=400,
        )

    # Decode state to get flow_id and verify CSRF token
    try:
        padded = state + "=" * (4 - len(state) % 4) if len(state) % 4 else state
        state_data = json.loads(
            base64.urlsafe_b64decode(padded).decode()
        )
        flow_id = state_data["flow_id"]
        csrf_token = state_data["csrf"]
    except (json.JSONDecodeError, KeyError, Exception) as exc:
        _LOGGER.error("OAuth webhook: failed to decode state: %s", exc)
        return web.Response(
            text="<html><body><h1>Error</h1>"
            "<p>Invalid state parameter.</p>"
            "</body></html>",
            content_type="text/html",
            status=400,
        )

    # Verify the flow exists and CSRF matches
    oauth_flows = hass.data.get(f"{DOMAIN}_oauth_flows", {})
    flow_data = oauth_flows.get(flow_id)

    if not flow_data or flow_data.get("csrf") != csrf_token:
        _LOGGER.error(
            "OAuth webhook: invalid session — flow_id=%s not found in "
            "active flows (%s)",
            flow_id,
            list(oauth_flows.keys()),
        )
        return web.Response(
            text="<html><body><h1>Error</h1>"
            "<p>Invalid or expired session. Please go back and try again.</p>"
            "</body></html>",
            content_type="text/html",
            status=400,
        )

    # Store the code and signal the waiting flow
    flow_data["code"] = code
    _LOGGER.debug("OAuth webhook received code for flow %s", flow_id)

    event: asyncio.Event | None = flow_data.get("event")
    if event:
        event.set()

    return web.Response(
        text="<html><body>"
        "<h1 style='text-align:center;margin-top:3rem;font-family:system-ui'>"
        "&#10003; Success!</h1>"
        "<p style='text-align:center;font-family:system-ui'>"
        "You can close this tab and return to Home Assistant.</p>"
        "<script>window.close()</script>"
        "</body></html>",
        content_type="text/html",
    )


# ── Config Flow ──────────────────────────────────────────────────────

class FrigateNotifyBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Frigate Notify Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._frigate_info: dict[str, Any] = {}
        self._has_frigate_integration: bool = False
        self._has_mqtt_integration: bool = False
        # OAuth auth code flow state
        self._oauth_token: str | None = None
        self._oauth_event: asyncio.Event | None = None
        self._oauth_task: asyncio.Task | None = None
        self._oauth_webhook_id: str | None = None
        self._configure_task: asyncio.Task | None = None
        # Firebase project picker state
        self._firebase_projects: list[dict] = []
        self._selected_project_id: str | None = None
        # Error handling state
        self._configure_error: str | None = None
        self._configure_error_detail: str | None = None

    # ── Step: user ───────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step — choose setup method."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        self._has_frigate_integration = check_frigate_integration(self.hass) is not None
        self._has_mqtt_integration = check_mqtt_integration(self.hass)

        return self.async_show_menu(
            step_id="user",
            menu_options={
                "frigate_setup": "Configure Frigate Connection",
                "push_provider": "Configure Push Notifications (Advanced)",
            },
            description_placeholders={
                "has_frigate": "Yes" if self._has_frigate_integration else "No",
                "has_mqtt": "Yes" if self._has_mqtt_integration else "No",
            },
        )

    # ── Step: frigate_setup ──────────────────────────────────────────

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
            schema_dict[
                vol.Optional(CONF_USE_FRIGATE_INTEGRATION, default=True)
            ] = selector.BooleanSelector()

        schema_dict[
            vol.Optional(CONF_FRIGATE_URL, default="")
        ] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        )
        schema_dict[vol.Optional(CONF_FRIGATE_USERNAME)] = selector.TextSelector()
        schema_dict[vol.Optional(CONF_FRIGATE_PASSWORD)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )

        if has_mqtt:
            schema_dict[
                vol.Optional(CONF_USE_HA_MQTT, default=True)
            ] = selector.BooleanSelector()

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

    # ── Step: mqtt ───────────────────────────────────────────────────

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

    # ── Step: push_provider ──────────────────────────────────────────

    async def async_step_push_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose push notification provider."""
        if user_input is not None:
            provider = user_input.get(CONF_PUSH_PROVIDER)
            self._data[CONF_PUSH_PROVIDER] = provider

            if provider == PUSH_PROVIDER_FCM:
                return await self.async_step_fcm()
            elif provider == PUSH_PROVIDER_NTFY:
                return await self.async_step_ntfy()
            elif provider == PUSH_PROVIDER_PUSHOVER:
                return await self.async_step_pushover()

        return self.async_show_form(
            step_id="push_provider",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PUSH_PROVIDER): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=PUSH_PROVIDER_FCM,
                                    label="Firebase Cloud Messaging (Recommended)",
                                ),
                                selector.SelectOptionDict(
                                    value=PUSH_PROVIDER_NTFY,
                                    label="ntfy (Self-hostable)",
                                ),
                                selector.SelectOptionDict(
                                    value=PUSH_PROVIDER_PUSHOVER,
                                    label="Pushover",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    # ── Step: fcm (menu) ─────────────────────────────────────────────

    async def async_step_fcm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """FCM setup menu: Easy Setup (Google Sign-In) vs Manual."""
        if user_input is not None:
            method = user_input.get(CONF_FCM_SETUP_METHOD)
            if method == FCM_SETUP_OAUTH:
                return await self.async_step_fcm_google_sign_in()
            return await self.async_step_fcm_manual()

        return self.async_show_form(
            step_id="fcm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FCM_SETUP_METHOD, default=FCM_SETUP_OAUTH
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=FCM_SETUP_OAUTH,
                                    label="Easy Setup (Google Sign-In)",
                                ),
                                selector.SelectOptionDict(
                                    value=FCM_SETUP_MANUAL,
                                    label="Manual (Paste Service Account JSON)",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )

    # ── Step: fcm_manual ─────────────────────────────────────────────

    async def async_step_fcm_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure FCM with manually pasted service account JSON."""
        errors: dict[str, str] = {}

        if user_input is not None:
            credentials = user_input.get(CONF_FCM_CREDENTIALS, "").strip()
            if not credentials:
                errors[CONF_FCM_CREDENTIALS] = "credentials_required"
            else:
                try:
                    creds_dict = json.loads(credentials)
                    if "project_id" not in creds_dict:
                        errors[CONF_FCM_CREDENTIALS] = "invalid_credentials"
                    elif "private_key" not in creds_dict:
                        errors[CONF_FCM_CREDENTIALS] = "invalid_credentials"
                    elif "client_email" not in creds_dict:
                        errors[CONF_FCM_CREDENTIALS] = "invalid_credentials"
                    else:
                        self._data[CONF_FCM_CREDENTIALS] = credentials
                        self._data[CONF_FCM_PROJECT_ID] = creds_dict["project_id"]
                except json.JSONDecodeError:
                    errors[CONF_FCM_CREDENTIALS] = "invalid_json"

            if not errors:
                return await self.async_step_relay()

        return self.async_show_form(
            step_id="fcm_manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FCM_CREDENTIALS): selector.TextSelector(
                        selector.TextSelectorConfig(
                            multiline=True,
                            type=selector.TextSelectorType.TEXT,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "fcm_instructions": (
                    "Paste your Firebase service account JSON credentials. "
                    "You can download this from the Firebase Console under "
                    "Project Settings > Service Accounts > "
                    "Generate New Private Key."
                ),
            },
        )

    # ── Step: fcm_google_sign_in — show link, then wait for callback ──

    async def async_step_fcm_google_sign_in(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show Google sign-in link. User opens it, then clicks Next."""
        if user_input is not None:
            # User clicked Next — start waiting for the callback
            return await self.async_step_fcm_wait_for_oauth()

        # Register a webhook for the OAuth callback (works through Nabu Casa)
        self._oauth_webhook_id = secrets.token_hex(32)
        webhook.async_register(
            self.hass,
            domain=DOMAIN,
            name="OAuth Callback",
            webhook_id=self._oauth_webhook_id,
            handler=_async_handle_oauth_webhook,
            local_only=False,
            allowed_methods=[METH_GET],
        )

        # Create an asyncio.Event for the callback to signal
        self._oauth_event = asyncio.Event()

        # Generate CSRF token and store flow state
        csrf_token = secrets.token_urlsafe(32)
        self.hass.data.setdefault(f"{DOMAIN}_oauth_flows", {})[
            self.flow_id
        ] = {
            "csrf": csrf_token,
            "code": None,
            "event": self._oauth_event,
        }

        # Build webhook callback URL (handles Nabu Casa automatically)
        callback_url = webhook.async_generate_url(
            self.hass, self._oauth_webhook_id
        )

        # Encode flow_id + csrf + callback into OAuth state parameter
        state_data = json.dumps({
            "flow_id": self.flow_id,
            "csrf": csrf_token,
            "callback": callback_url,
        })
        state = base64.urlsafe_b64encode(state_data.encode()).decode()

        # Build Google OAuth URL
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(GOOGLE_OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        self._oauth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

        return self.async_show_form(
            step_id="fcm_google_sign_in",
            data_schema=vol.Schema({}),
            description_placeholders={
                "oauth_url": self._oauth_url,
            },
        )

    # ── Step: fcm_wait_for_oauth — spinner waiting for callback ────────

    async def async_step_fcm_wait_for_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show spinner while waiting for OAuth callback."""
        # Check if the code has already arrived
        oauth_flows = self.hass.data.get(f"{DOMAIN}_oauth_flows", {})
        flow_data = oauth_flows.get(self.flow_id, {})
        if flow_data.get("code"):
            return self.async_show_progress_done(
                next_step_id="fcm_exchange_token",
            )

        # Guard: only create the task once
        if not self._oauth_task:
            self._oauth_task = self.hass.async_create_task(
                self._async_wait_for_oauth()
            )

        # Task completed (callback arrived or timeout)
        if self._oauth_task.done():
            # Re-check for code after task completion
            if flow_data.get("code"):
                return self.async_show_progress_done(
                    next_step_id="fcm_exchange_token",
                )
            # Timeout — no code arrived
            return self.async_abort(reason="oauth_failed")

        return self.async_show_progress(
            step_id="fcm_wait_for_oauth",
            progress_action="wait_for_oauth",
            progress_task=self._oauth_task,
        )

    async def _async_wait_for_oauth(self) -> None:
        """Background task: wait for the OAuth callback to deliver the code."""
        try:
            async with asyncio.timeout(300):  # 5 minute timeout
                await self._oauth_event.wait()
        except TimeoutError:
            _LOGGER.warning("OAuth sign-in timed out after 5 minutes")

    def _unregister_webhook(self) -> None:
        """Clean up the OAuth webhook."""
        if self._oauth_webhook_id:
            webhook.async_unregister(self.hass, self._oauth_webhook_id)
            self._oauth_webhook_id = None

    def async_remove(self) -> None:
        """Clean up when flow is cancelled or removed."""
        self._unregister_webhook()
        # Clean up flow data
        oauth_flows = self.hass.data.get(f"{DOMAIN}_oauth_flows", {})
        oauth_flows.pop(self.flow_id, None)
        # Cancel background tasks
        for task in (self._oauth_task, self._configure_task):
            if task and not task.done():
                task.cancel()

    # ── Step: fcm_exchange_token — exchange code for access token ──────

    async def async_step_fcm_exchange_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Exchange the auth code for an access token."""
        # Clean up webhook
        self._unregister_webhook()

        oauth_flows = self.hass.data.get(f"{DOMAIN}_oauth_flows", {})
        flow_data = oauth_flows.pop(self.flow_id, {})
        auth_code = flow_data.get("code")

        if not auth_code:
            return self.async_abort(reason="oauth_failed")

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{DEFAULT_RELAY_URL}/exchangeGoogleToken",
                json={
                    "code": auth_code,
                    "redirectUri": GOOGLE_OAUTH_REDIRECT_URI,
                },
            ) as resp:
                body = await resp.json()

                if resp.status == 200:
                    self._oauth_token = body["access_token"]
                    return await self.async_step_fcm_pick_project()

                _LOGGER.error(
                    "OAuth token exchange via relay failed (%s): %s",
                    resp.status, body,
                )
                return self.async_abort(reason="oauth_token_failed")

        except Exception as exc:
            _LOGGER.error("OAuth token exchange error: %s", exc)
            return self.async_abort(reason="oauth_failed")

    # ── Step: fcm_pick_project ─────────────────────────────────────────

    async def async_step_fcm_pick_project(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick an existing Firebase project or create a new one."""
        if user_input is not None:
            choice = user_input.get(CONF_FIREBASE_PROJECT, _CREATE_NEW)
            if choice == _CREATE_NEW:
                self._selected_project_id = None
            else:
                self._selected_project_id = choice
            return await self.async_step_fcm_configure_project()

        # Fetch existing Firebase projects
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {self._oauth_token}"}
        try:
            async with session.get(
                FIREBASE_LIST_PROJECTS_URL,
                headers=headers,
                timeout=15,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._firebase_projects = data.get("results", [])
                else:
                    _LOGGER.debug(
                        "Firebase project listing returned %s, skipping picker",
                        resp.status,
                    )
                    self._firebase_projects = []
        except Exception as exc:
            _LOGGER.debug("Failed to list Firebase projects: %s", exc)
            self._firebase_projects = []

        # If no existing projects, skip picker and create new
        if not self._firebase_projects:
            self._selected_project_id = None
            return await self.async_step_fcm_configure_project()

        # Build options list
        options = [
            selector.SelectOptionDict(
                value=_CREATE_NEW,
                label="Create a new Firebase project",
            ),
        ]
        for proj in self._firebase_projects:
            proj_id = proj.get("projectId", "")
            display = proj.get("displayName", proj_id)
            if proj_id:
                options.append(
                    selector.SelectOptionDict(
                        value=proj_id,
                        label=f"{display} ({proj_id})",
                    )
                )

        return self.async_show_form(
            step_id="fcm_pick_project",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FIREBASE_PROJECT, default=_CREATE_NEW
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    # ── Step: fcm_configure_project ──────────────────────────────────

    async def async_step_fcm_configure_project(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Auto-create GCP project + Firebase + FCM + SA with spinner."""
        if not self._configure_task:
            self._configure_task = self.hass.async_create_task(
                self._async_configure_firebase()
            )

        if self._configure_task.done():
            try:
                self._configure_task.result()  # Raise if failed
            except ConfigureError as exc:
                _LOGGER.error("FCM configuration failed at %s: %s", exc.step, exc)
                self._configure_error = exc.error_key
                self._configure_error_detail = exc.detail
                self._configure_task = None  # Allow retry
                return self.async_show_progress_done(
                    next_step_id="fcm_configure_failed",
                )
            except Exception as exc:
                _LOGGER.error("FCM project configuration failed: %s", exc)
                self._configure_error = "fcm_configure_unknown"
                self._configure_error_detail = str(exc)
                self._configure_task = None  # Allow retry
                return self.async_show_progress_done(
                    next_step_id="fcm_configure_failed",
                )
            return self.async_show_progress_done(
                next_step_id="fcm_finish",
            )

        return self.async_show_progress(
            step_id="fcm_configure_project",
            progress_action="configuring_firebase",
            progress_task=self._configure_task,
        )

    async def async_step_fcm_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Create the config entry after Firebase setup is done."""
        return await self.async_step_relay()

    # ── Step: fcm_configure_failed ──────────────────────────────────

    async def async_step_fcm_configure_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show error details and offer retry or manual fallback."""
        if user_input is not None:
            action = user_input.get("action", "retry")
            if action == "manual":
                return await self.async_step_fcm_manual()
            # Retry: reset task and re-enter configure
            self._configure_task = None
            return await self.async_step_fcm_configure_project()

        error_key = self._configure_error or "fcm_configure_unknown"
        error_detail = self._configure_error_detail or "Unknown error"

        return self.async_show_form(
            step_id="fcm_configure_failed",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="retry"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="retry",
                                    label="Retry automatic setup",
                                ),
                                selector.SelectOptionDict(
                                    value="manual",
                                    label="Switch to manual setup",
                                ),
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "error_key": error_key,
                "error_detail": error_detail,
            },
        )

    async def _async_configure_firebase(self) -> None:
        """Background task: create GCP project, add Firebase, set up FCM."""
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {self._oauth_token}"}

        reusing = self._selected_project_id is not None

        if reusing:
            project_id = self._selected_project_id
            _LOGGER.info(
                "Reusing existing Firebase project: %s (skipping steps 1-2)",
                project_id,
            )
        else:
            # 1. Create GCP project
            project_id = f"frigate-notify-{secrets.token_hex(4)}"
            _LOGGER.info("Step 1/5: Creating GCP project: %s", project_id)

            try:
                async with session.post(
                    GCP_CREATE_PROJECT_URL,
                    json={
                        "projectId": project_id,
                        "displayName": "Frigate Notify Bridge",
                    },
                    headers=headers,
                ) as resp:
                    if resp.status not in (200, 409):
                        body = await resp.text()
                        raise ConfigureError(
                            "create_project", resp.status, body
                        )
                    op = await resp.json()

                # Poll until project creation completes
                if not op.get("done"):
                    op_name = op.get("name", "")
                    await self._poll_operation(
                        session, headers, GCP_OPERATIONS_URL, op_name
                    )
            except ConfigureError:
                raise
            except Exception as exc:
                raise ConfigureError(
                    "create_project", 0, str(exc)
                ) from exc

            # 2. Add Firebase to the project
            _LOGGER.info("Step 2/5: Adding Firebase to project %s", project_id)
            try:
                async with session.post(
                    FIREBASE_ADD_URL.format(project_id=project_id),
                    json={},
                    headers=headers,
                ) as resp:
                    if resp.status not in (200, 409):
                        body = await resp.text()
                        raise ConfigureError(
                            "add_firebase", resp.status, body
                        )
                    op = await resp.json()

                if not op.get("done"):
                    op_name = op.get("name", "")
                    await self._poll_operation(
                        session, headers, FIREBASE_OPERATIONS_URL, op_name
                    )
            except ConfigureError:
                raise
            except Exception as exc:
                raise ConfigureError(
                    "add_firebase", 0, str(exc)
                ) from exc

        # 3. Enable FCM + IAM APIs (must poll until ready)
        step_prefix = "Step 3/5" if not reusing else "Step 1/3"
        _LOGGER.info("%s: Enabling FCM and IAM APIs for %s", step_prefix, project_id)
        await self._enable_api(session, headers, project_id, FCM_SERVICE_NAME)
        await self._enable_api(session, headers, project_id, IAM_SERVICE_NAME)

        # Wait for API enablement to propagate
        await asyncio.sleep(5)

        # 4. Create service account (with retry for propagation delay)
        step_prefix = "Step 4/5" if not reusing else "Step 2/3"
        _LOGGER.info("%s: Creating service account for %s", step_prefix, project_id)
        sa_email = await self._create_service_account(
            session, headers, project_id
        )

        # Wait for SA to propagate before creating key
        await asyncio.sleep(5)

        # 5. Create SA key (with retry)
        step_prefix = "Step 5/5" if not reusing else "Step 3/3"
        _LOGGER.info("%s: Creating SA key for %s", step_prefix, project_id)
        sa_key_json = await self._create_sa_key(
            session, headers, project_id, sa_email
        )

        # Store credentials
        self._data[CONF_FCM_CREDENTIALS] = sa_key_json
        self._data[CONF_FCM_PROJECT_ID] = project_id

        # 6. Create a web app and get Firebase client config for mobile app
        _LOGGER.info("Fetching Firebase client config for mobile app")
        client_config = await self._get_firebase_client_config(
            session, headers, project_id
        )
        if client_config:
            self._data[CONF_FIREBASE_CLIENT_CONFIG] = client_config

        # Revoke the OAuth token
        await self._revoke_token(session)

        _LOGGER.info("Firebase setup complete for %s", project_id)

    # ── Google Cloud API helpers ─────────────────────────────────────

    async def _poll_operation(
        self,
        session: Any,
        headers: dict[str, str],
        url_template: str,
        operation_name: str,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Poll a long-running operation until it completes."""
        url = url_template.format(operation_name=operation_name)
        for _ in range(timeout):
            await asyncio.sleep(2)
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                if data.get("done"):
                    if "error" in data:
                        raise ConfigureError(
                            "operation_timeout", 0,
                            f"Operation failed: {data['error']}"
                        )
                    return data
        raise ConfigureError(
            "operation_timeout", 0,
            f"Operation timed out after {timeout * 2}s: {operation_name}"
        )

    async def _enable_api(
        self,
        session: Any,
        headers: dict[str, str],
        project_id: str,
        service_name: str,
    ) -> None:
        """Enable a Google Cloud API and wait for it to be ready."""
        url = SERVICE_USAGE_ENABLE_URL.format(
            project_id=project_id,
            service_name=service_name,
        )
        try:
            async with session.post(url, headers=headers) as resp:
                if resp.status not in (200, 409):
                    body = await resp.text()
                    raise ConfigureError("enable_api", resp.status, body)
                data = await resp.json()

            # Service Usage returns an Operation — poll until done
            op_name = data.get("name", "") if isinstance(data, dict) else ""
            if op_name and not data.get("done"):
                _LOGGER.debug("Waiting for %s API to enable...", service_name)
                svc_ops_url = (
                    "https://serviceusage.googleapis.com/v1/{operation_name}"
                )
                await self._poll_operation(
                    session, headers, svc_ops_url, op_name
                )
        except ConfigureError:
            raise
        except Exception as exc:
            raise ConfigureError("enable_api", 0, str(exc)) from exc

    async def _create_service_account(
        self,
        session: Any,
        headers: dict[str, str],
        project_id: str,
    ) -> str:
        """Create a service account for FCM push notifications.

        Retries if IAM API is still propagating after enablement.
        """
        url = IAM_CREATE_SA_URL.format(project_id=project_id)
        payload = {
            "accountId": FCM_SA_NAME,
            "serviceAccount": {
                "displayName": FCM_SA_DISPLAY_NAME,
                "description": (
                    "Service account for Frigate Notify Bridge "
                    "push notifications"
                ),
            },
        }

        last_error = ""
        for attempt in range(6):  # Up to 30s of retries
            async with session.post(
                url, json=payload, headers=headers
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _LOGGER.debug("Created service account: %s", data["email"])
                    return data["email"]
                elif resp.status == 409:
                    # Already exists
                    return IAM_SA_EMAIL_TEMPLATE.format(
                        sa_name=FCM_SA_NAME,
                        project_id=project_id,
                    )

                last_error = await resp.text()
                if resp.status in (403, 404) and attempt < 5:
                    _LOGGER.debug(
                        "SA creation attempt %d got %d, retrying...",
                        attempt + 1, resp.status,
                    )
                    await asyncio.sleep(5)
                    continue

                raise ConfigureError(
                    "create_sa", resp.status, last_error
                )

        raise ConfigureError("create_sa", 0, last_error)

    async def _create_sa_key(
        self,
        session: Any,
        headers: dict[str, str],
        project_id: str,
        sa_email: str,
    ) -> str:
        """Create a new key for the service account and return the JSON.

        Retries on 404 since the SA may not have propagated yet.
        """
        url = IAM_CREATE_KEY_URL.format(
            project_id=project_id,
            sa_email=sa_email,
        )

        last_error = ""
        for attempt in range(6):  # Up to 30s of retries
            async with session.post(
                url,
                json={"privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE"},
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    key_data = data.get("privateKeyData", "")
                    return base64.b64decode(key_data).decode("utf-8")

                last_error = await resp.text()
                if resp.status == 404 and attempt < 5:
                    _LOGGER.debug(
                        "SA key creation attempt %d got 404, retrying...",
                        attempt + 1,
                    )
                    await asyncio.sleep(5)
                    continue

                raise ConfigureError(
                    "create_key", resp.status, last_error
                )

        raise ConfigureError("create_key", 0, last_error)

    async def _get_firebase_client_config(
        self,
        session: Any,
        headers: dict[str, str],
        project_id: str,
    ) -> dict[str, str] | None:
        """Get Firebase client config for mobile app dynamic initialization.

        Creates a web app if none exists, then fetches its config
        (apiKey, appId, messagingSenderId, projectId).
        """
        try:
            # List existing web apps
            list_url = FIREBASE_WEB_APPS_URL.format(project_id=project_id)
            async with session.get(list_url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    apps = data.get("apps", [])
                else:
                    apps = []

            # Create a web app if none exists
            app_id = None
            if apps:
                app_id = apps[0].get("appId")
            else:
                _LOGGER.debug("Creating Firebase web app for project %s", project_id)
                create_url = FIREBASE_WEB_APPS_URL.format(project_id=project_id)
                async with session.post(
                    create_url,
                    json={"displayName": "Frigate Mobile Bridge"},
                    headers=headers,
                    timeout=15,
                ) as resp:
                    if resp.status == 200:
                        op = await resp.json()
                        # May be a long-running operation
                        if op.get("done") and "response" in op:
                            app_id = op["response"].get("appId")
                        elif "appId" in op:
                            app_id = op["appId"]
                        elif op.get("name") and not op.get("done"):
                            # Poll operation
                            result = await self._poll_operation(
                                session, headers,
                                "https://firebase.googleapis.com/v1beta1/{operation_name}",
                                op["name"],
                            )
                            app_id = result.get("response", {}).get("appId")
                    else:
                        body = await resp.text()
                        _LOGGER.warning("Failed to create web app: %s %s", resp.status, body)

            if not app_id:
                _LOGGER.warning("Could not get Firebase web app ID")
                return None

            # Fetch web app config
            await asyncio.sleep(2)  # Brief propagation delay
            config_url = FIREBASE_WEB_APP_CONFIG_URL.format(
                project_id=project_id, app_id=app_id
            )
            async with session.get(config_url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    config = await resp.json()
                    return {
                        "project_id": config.get("projectId", project_id),
                        "api_key": config.get("apiKey", ""),
                        "app_id": config.get("appId", app_id),
                        "messaging_sender_id": config.get("messagingSenderId", ""),
                    }
                else:
                    body = await resp.text()
                    _LOGGER.warning("Failed to get web app config: %s %s", resp.status, body)
                    return None

        except Exception as exc:
            _LOGGER.warning("Failed to get Firebase client config: %s", exc)
            return None

    async def _revoke_token(self, session: Any) -> None:
        """Revoke the OAuth access token after setup is complete."""
        if not self._oauth_token:
            return
        try:
            async with session.post(
                GOOGLE_REVOKE_URL,
                params={"token": self._oauth_token},
            ) as resp:
                if resp.status == 200:
                    _LOGGER.debug("OAuth token revoked successfully")
                else:
                    _LOGGER.debug(
                        "OAuth token revocation returned %s", resp.status
                    )
        except Exception as exc:
            _LOGGER.debug("OAuth token revocation failed: %s", exc)
        finally:
            self._oauth_token = None

    # ── Step: ntfy ───────────────────────────────────────────────────

    async def async_step_ntfy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure ntfy."""
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

            if not errors:
                return await self.async_step_relay()

        return self.async_show_form(
            step_id="ntfy",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_NTFY_URL, default=DEFAULT_NTFY_URL
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.URL
                        )
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

    # ── Step: pushover ───────────────────────────────────────────────

    async def async_step_pushover(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure Pushover."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_key = user_input.get(CONF_PUSHOVER_USER_KEY, "").strip()
            api_token = user_input.get(CONF_PUSHOVER_API_TOKEN, "").strip()

            if not user_key:
                errors[CONF_PUSHOVER_USER_KEY] = "user_key_required"
            if not api_token:
                errors[CONF_PUSHOVER_API_TOKEN] = "api_token_required"

            if not errors:
                self._data[CONF_PUSHOVER_USER_KEY] = user_key
                self._data[CONF_PUSHOVER_API_TOKEN] = api_token
                return await self.async_step_relay()

        return self.async_show_form(
            step_id="pushover",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PUSHOVER_USER_KEY
                    ): selector.TextSelector(),
                    vol.Required(
                        CONF_PUSHOVER_API_TOKEN
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            errors=errors,
        )


    # ── Step: relay ──────────────────────────────────────────────────

    async def async_step_relay(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure push relay (auto-generates bridge secret and E2E key)."""
        if user_input is not None:
            self._data[CONF_RELAY_URL] = user_input[CONF_RELAY_URL].rstrip("/")
            self._data[CONF_RELAY_BRIDGE_ID] = user_input[CONF_RELAY_BRIDGE_ID].strip()
            self._data[CONF_RELAY_BRIDGE_SECRET] = user_input[CONF_RELAY_BRIDGE_SECRET]
            self._data[CONF_RELAY_E2E_KEY] = user_input[CONF_RELAY_E2E_KEY]
            return self.async_create_entry(
                title="Frigate Notify Bridge",
                data=self._data,
            )


        # Auto-generate a bridge ID from a random UUID (truncated)
        default_bridge_id = uuid.uuid4().hex[:16]
        # Auto-generate a cryptographically random 32-byte bridge secret (hex)
        default_bridge_secret = secrets.token_hex(32)
        # Auto-generate a 32-byte E2E encryption key (hex)
        default_e2e_key = secrets.token_hex(32)

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
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Required(
                        CONF_RELAY_E2E_KEY, default=default_e2e_key
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            description_placeholders={
                "relay_url": DEFAULT_RELAY_URL,
            },
        )

    # ── Options flow ─────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return FrigateNotifyBridgeOptionsFlow()


class FrigateNotifyBridgeOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Frigate Notify Bridge."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "connection_settings": "Connection Settings",
                "notification_settings": "Notification Settings",
                "device_management": "Manage Devices",
            },
        )

    async def async_step_connection_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure connection settings (Frigate URL, home WiFi SSIDs)."""
        if user_input is not None:
            # Update both data and options
            new_data = {**self.config_entry.data}
            if user_input.get(CONF_FRIGATE_URL):
                new_data[CONF_FRIGATE_URL] = user_input[CONF_FRIGATE_URL]
            new_data[CONF_HOME_SSIDS] = user_input.get(CONF_HOME_SSIDS, [])
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current_url = self.config_entry.data.get(CONF_FRIGATE_URL, "")
        current_ssids = self.config_entry.data.get(CONF_HOME_SSIDS, [])

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
                }
            ),
        )

    async def async_step_notification_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure notification settings."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="notification_settings",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "cooldown_seconds",
                        default=self.config_entry.options.get(
                            "cooldown_seconds", 60
                        ),
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

    async def async_step_device_management(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage paired devices."""
        data = self.hass.data.get(DOMAIN, {}).get(
            self.config_entry.entry_id, {}
        )
        device_manager = data.get("device_manager")

        if device_manager is None:
            return self.async_abort(reason="not_configured")

        devices = await device_manager.async_get_devices()

        if user_input is not None:
            devices_to_remove = user_input.get("remove_devices", [])
            for device_id in devices_to_remove:
                await device_manager.async_remove_device(device_id)
            return self.async_create_entry(title="", data={})

        if not devices:
            return self.async_abort(reason="no_devices")

        device_options = [
            selector.SelectOptionDict(
                value=device_id,
                label=(
                    f"{device['name']} "
                    f"({device.get('platform', 'Unknown')})"
                ),
            )
            for device_id, device in devices.items()
        ]

        return self.async_show_form(
            step_id="device_management",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "remove_devices"
                    ): selector.SelectSelector(
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
