"""REST API for Frigate Notify Bridge mobile app communication."""
from __future__ import annotations

import hashlib
import json
import logging
import ssl
from typing import Any, TYPE_CHECKING
from urllib.parse import quote
from http.cookies import SimpleCookie

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    API_BASE_PATH,
    API_FRIGATE_PROXY_PATH,
    API_MEDIA_PROXY_PATH,
    CONF_FRIGATE_URL,
    CONF_FRIGATE_USERNAME,
    CONF_FRIGATE_PASSWORD,
    CONF_HOME_SSIDS,
    CONF_PUSH_PROVIDER,
    CONF_FIREBASE_CLIENT_CONFIG,
    CONF_RELAY_URL,
    CONF_RELAY_BRIDGE_ID,
    CONF_RELAY_BRIDGE_SECRET,
    CONF_RELAY_E2E_KEY,
    SIGNAL_DEVICE_UPDATED,
)
from .qr_generator import (
    generate_pairing_qr_data,
    generate_qr_code_base64,
    generate_qr_code_image,
)

if TYPE_CHECKING:
    from .coordinator import FrigateNotifyCoordinator
    from .device_manager import DeviceManager

_LOGGER = logging.getLogger(__name__)


def _extract_frigate_token(
    response: aiohttp.ClientResponse,
    payload: dict[str, Any] | str | None,
) -> str | None:
    """Extract a Frigate JWT from JSON, plain-text bodies, or cookies."""
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if token:
        return str(token)

    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped:
            if stripped.startswith("{"):
                try:
                    decoded = json.loads(stripped)
                except Exception:
                    decoded = None
                if isinstance(decoded, dict):
                    token = decoded.get("access_token")
                    if token:
                        return str(token)
            elif "." in stripped:
                return stripped

    raw_cookies = response.headers.getall("Set-Cookie", [])
    for raw_cookie in raw_cookies:
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            continue
        morsel = cookie.get("frigate_token")
        if morsel and morsel.value:
            return morsel.value

    return None


async def _read_frigate_login_payload(
    response: aiohttp.ClientResponse,
) -> dict[str, Any] | str | None:
    """Read a Frigate login response without assuming a JSON content type."""
    body = await response.text()
    if not body:
        return None
    stripped = body.strip()
    if not stripped:
        return None
    try:
        decoded = json.loads(stripped)
    except Exception:
        return stripped
    if isinstance(decoded, dict):
        return decoded
    return stripped


def _proxy_response_headers(response: aiohttp.ClientResponse) -> dict[str, str]:
    """Copy safe upstream headers into the Home Assistant proxy response."""
    excluded = {
        "content-length",
        "transfer-encoding",
        "content-encoding",
        "connection",
        "keep-alive",
    }
    headers: dict[str, str] = {}
    for key, value in response.headers.items():
        if key.lower() in excluded:
            continue
        headers[key] = value
    return headers


async def async_setup_api(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: FrigateNotifyCoordinator,
    device_manager: DeviceManager,
) -> None:
    """Set up the REST API endpoints."""
    hass.http.register_view(PairingQRView(entry, coordinator, device_manager))
    hass.http.register_view(PairDeviceView(entry, coordinator, device_manager))
    hass.http.register_view(DevicesView(entry, coordinator, device_manager))
    hass.http.register_view(DeviceView(entry, coordinator, device_manager))
    hass.http.register_view(DeviceTokenView(entry, coordinator, device_manager))
    hass.http.register_view(ConfigView(entry, coordinator, device_manager))
    hass.http.register_view(StatusView(entry, coordinator, device_manager))
    hass.http.register_view(TestNotificationView(entry, coordinator, device_manager))
    hass.http.register_view(WebRTCCredentialsView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateProxyView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateMediaView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateCredentialsView(entry, coordinator, device_manager))

    _LOGGER.info("Frigate Notify Bridge API endpoints registered")


class BaseAPIView(HomeAssistantView):
    """Base class for API views."""

    requires_auth = True

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: FrigateNotifyCoordinator,
        device_manager: DeviceManager,
    ) -> None:
        """Initialize the view."""
        self.entry = entry
        self.coordinator = coordinator
        self.device_manager = device_manager

    def _get_authenticated_user_id(self, request: web.Request) -> str | None:
        """Return the authenticated HA user ID for the request."""
        user = request.get("hass_user")
        return getattr(user, "id", None)

    def _resolve_owned_device_id(
        self,
        request: web.Request,
        requested_device_id: str | None = None,
    ) -> str | None:
        """Resolve and authorize a bridge device ID for the authenticated user."""
        user_id = self._get_authenticated_user_id(request)
        if not user_id:
            return None

        device_id = requested_device_id or request.headers.get("X-Frigate-Device-Id")
        if not device_id:
            _LOGGER.debug(
                "Missing X-Frigate-Device-Id on %s %s for user %s",
                request.method,
                request.path,
                user_id,
            )
            return None

        if self.device_manager.user_owns_device(user_id, device_id):
            return device_id

        _LOGGER.warning(
            "User %s attempted to access unauthorized device %s on %s %s",
            user_id,
            device_id,
            request.method,
            request.path,
        )
        return None


class PairingQRView(BaseAPIView):
    """Generate pairing QR code."""

    url = f"{API_BASE_PATH}/pairing/qr"
    name = "api:frigate_notify_bridge:pairing_qr"

    async def get(self, request: web.Request) -> web.Response:
        """Generate and return a pairing QR code."""
        # Get query parameters
        size = int(request.query.get("size", "300"))
        format_type = request.query.get("format", "json")  # json, png, or data

        # Generate pairing code
        pairing_info = self.device_manager.generate_pairing_code()

        # Get configuration — auto-detect direct API URL for the app
        configured_url = self.entry.data.get(CONF_FRIGATE_URL)
        frigate_url = await _resolve_frigate_api_url(
            request.app["hass"], configured_url
        )
        push_provider = self.entry.data.get(CONF_PUSH_PROVIDER)
        fcm_sender_id = self.coordinator.push_provider.get_sender_id()

        # Check for custom external URL in options
        custom_external_url = self.entry.options.get("external_url")
        use_cloud = self.entry.options.get("use_cloud_remote", True)

        # Relay info for QR v3
        relay_url = self.entry.data.get(CONF_RELAY_URL)
        e2e_key = self.entry.data.get(CONF_RELAY_E2E_KEY)

        # Generate QR data
        qr_data = await generate_pairing_qr_data(
            hass=request.app["hass"],
            pairing_info=pairing_info,
            frigate_url=frigate_url,
            frigate_auth_required=bool(self.entry.data.get("frigate_username")),
            push_provider=push_provider,
            fcm_sender_id=fcm_sender_id,
            custom_external_url=custom_external_url,
            use_cloud_remote=use_cloud,
            relay_url=relay_url,
            e2e_key=e2e_key,
        )

        if format_type == "png":
            # Return raw PNG image
            try:
                image_bytes = await generate_qr_code_image(qr_data, size, "png")
                return web.Response(
                    body=image_bytes,
                    content_type="image/png",
                )
            except Exception as e:
                _LOGGER.error("Failed to generate QR image: %s", e)
                return web.json_response(
                    {"error": "Failed to generate QR code image"},
                    status=500,
                )

        elif format_type == "data":
            # Return base64-encoded image
            try:
                image_b64 = await generate_qr_code_base64(qr_data, size)
                return web.json_response({
                    "code": qr_data["code"],
                    "expires_at": qr_data["expires_at"],
                    "expires_in": qr_data["expires_in"],
                    "image": f"data:image/png;base64,{image_b64}",
                })
            except Exception as e:
                _LOGGER.error("Failed to generate QR image: %s", e)
                return web.json_response(
                    {"error": "Failed to generate QR code"},
                    status=500,
                )

        else:
            # Return JSON with QR URL
            return web.json_response({
                "code": qr_data["code"],
                "url": qr_data["url"],
                "expires_at": qr_data["expires_at"],
                "expires_in": qr_data["expires_in"],
                "using_cloud": qr_data.get("using_cloud", False),
                "webrtc_available": qr_data.get("webrtc_available", False),
            })


class PairDeviceView(BaseAPIView):
    """Complete device pairing."""

    url = f"{API_BASE_PATH}/pair"
    name = "api:frigate_notify_bridge:pair"

    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Complete device pairing with token/code."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON"},
                status=400,
            )

        token_or_code = data.get("token") or data.get("code")
        if not token_or_code:
            return web.json_response(
                {"error": "Missing token or code"},
                status=400,
            )

        device_info = {
            "name": data.get("name", "Unknown Device"),
            "platform": data.get("platform", "unknown"),
            "fcm_token": data.get("fcm_token"),
            "app_version": data.get("app_version"),
            "mobile_app_device_id": data.get("mobile_app_device_id"),
            "mobile_app_webhook_id": data.get("mobile_app_webhook_id"),
            "mobile_app_secret": data.get("mobile_app_secret"),
            "mobile_app_cloudhook_url": data.get("mobile_app_cloudhook_url"),
            "mobile_app_remote_ui_url": data.get("mobile_app_remote_ui_url"),
        }

        _LOGGER.debug(
            "Pair request received: device_name=%s platform=%s has_fcm_token=%s token_length=%s",
            device_info["name"],
            device_info["platform"],
            bool(device_info["fcm_token"]),
            len(token_or_code),
        )

        try:
            user_id = self._get_authenticated_user_id(request)
            if not user_id:
                return web.json_response({"error": "Unauthorized"}, status=401)
            result = await self.device_manager.async_complete_pairing(
                token_or_code,
                device_info,
                user_id=user_id,
            )
            _LOGGER.info(
                "Pair request succeeded: device_id=%s platform=%s name=%s",
                result["device_id"],
                device_info["platform"],
                device_info["name"],
            )

            # Include additional config for the app
            # Auto-detect direct Frigate API URL (e.g. port 5000) for local access
            configured_url = self.entry.data.get(CONF_FRIGATE_URL)
            frigate_url = await _resolve_frigate_api_url(
                request.app["hass"], configured_url
            )
            home_ssids = self.entry.options.get(
                CONF_HOME_SSIDS,
                self.entry.data.get(CONF_HOME_SSIDS, []),
            )
            paired_device = await self.device_manager.async_get_device(result["device_id"])

            config_response = {
                "frigate_url": frigate_url,
                "proxy_path": API_FRIGATE_PROXY_PATH,
                "push_provider": self.entry.data.get(CONF_PUSH_PROVIDER),
                "fcm_sender_id": self.coordinator.push_provider.get_sender_id(),
                "home_ssids": home_ssids,
                "frigate_auth_required": bool(
                    self.entry.data.get(CONF_FRIGATE_USERNAME)
                ),
                "remote_ui_url": (
                    paired_device.get("mobile_app_remote_ui_url")
                    if paired_device
                    else None
                ),
            }

            # Include Firebase client options for dynamic app initialization
            firebase_client_config = self.entry.data.get(CONF_FIREBASE_CLIENT_CONFIG)
            if firebase_client_config:
                config_response["firebase_options"] = firebase_client_config

            # Include relay info for push notification relay
            relay_url = self.entry.data.get(CONF_RELAY_URL)
            relay_bridge_id = self.entry.data.get(CONF_RELAY_BRIDGE_ID)
            relay_bridge_secret = self.entry.data.get(CONF_RELAY_BRIDGE_SECRET)
            e2e_key = self.entry.data.get(CONF_RELAY_E2E_KEY)
            if relay_url and relay_bridge_id:
                config_response["relay_url"] = relay_url
                config_response["relay_bridge_id"] = relay_bridge_id
            if relay_bridge_secret:
                config_response["relay_bridge_secret"] = relay_bridge_secret
            if e2e_key:
                config_response["e2e_key"] = e2e_key

            _LOGGER.debug(
                "Pairing response config for device %s: push_provider=%s has_firebase_options=%s relay_url=%s relay_bridge_id_present=%s relay_secret_present=%s",
                result["device_id"],
                config_response.get("push_provider"),
                "firebase_options" in config_response,
                bool(config_response.get("relay_url")),
                bool(config_response.get("relay_bridge_id")),
                bool(config_response.get("relay_bridge_secret")),
            )

            # Register device with push relay if available
            relay_device_id = None
            fcm_token = device_info.get("fcm_token")
            if relay_url and fcm_token:
                from .push_providers.relay import RelayPushProvider

                bridge_secret = self.entry.data.get(CONF_RELAY_BRIDGE_SECRET)
                if bridge_secret:
                    try:
                        relay_provider = self.coordinator.push_provider
                        if isinstance(relay_provider, RelayPushProvider):
                            relay_device_id = await relay_provider.async_register_device(
                                fcm_token=fcm_token,
                                platform=device_info.get("platform", "unknown"),
                            )
                    except Exception as e:
                        _LOGGER.warning("Failed to register device with relay: %s", e)

            if relay_device_id:
                config_response["relay_device_id"] = relay_device_id
                # Store relay_device_id on the device record
                await self.device_manager.async_update_device(
                    result["device_id"],
                    {"relay_device_id": relay_device_id},
                )

            # Compute TLS cert fingerprint for certificate pinning
            cert_fingerprint = await _get_tls_fingerprint(request.app["hass"])

            return web.json_response({
                "success": True,
                "device_id": result["device_id"],
                "cert_fingerprint": cert_fingerprint,
                "config": config_response,
            })

        except ValueError as e:
            return web.json_response(
                {"error": str(e)},
                status=400,
            )


class DevicesView(BaseAPIView):
    """List and manage devices."""

    url = f"{API_BASE_PATH}/devices"
    name = "api:frigate_notify_bridge:devices"
    requires_auth = True  # Requires HA auth for admin operations

    async def get(self, request: web.Request) -> web.Response:
        """List all paired devices (admin only)."""
        devices = await self.device_manager.async_get_devices()

        # Remove sensitive data from response
        safe_devices = {}
        for device_id, device in devices.items():
            safe_devices[device_id] = {
                "id": device["id"],
                "name": device["name"],
                "platform": device["platform"],
                "app_version": device.get("app_version"),
                "subscription_active": device.get("subscription_active"),
                "subscription_last_verified_at": device.get("subscription_last_verified_at"),
                "paired_at": device["paired_at"],
                "last_seen": device.get("last_seen"),
            }

        return web.json_response({
            "devices": safe_devices,
            "count": len(safe_devices),
        })


class DeviceView(BaseAPIView):
    """Manage individual device."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}"
    name = "api:frigate_notify_bridge:device"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Get device details."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        device = await self.device_manager.async_get_device(device_id)
        if not device:
            return web.json_response(
                {"error": "Device not found"},
                status=404,
            )

        _LOGGER.debug("Returning device details for %s", device_id)
        # Return device info without sensitive data
        return web.json_response({
            "id": device["id"],
            "name": device["name"],
            "platform": device["platform"],
            "subscription_active": device.get("subscription_active"),
            "subscription_last_verified_at": device.get("subscription_last_verified_at"),
            "notification_settings": device.get("notification_settings", {}),
        })

    async def patch(self, request: web.Request, device_id: str) -> web.Response:
        """Update device settings."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        try:
            updates = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON"},
                status=400,
            )

        _LOGGER.debug("Updating device %s with keys=%s", device_id, list(updates.keys()))

        device = await self.device_manager.async_update_device(device_id, updates)
        if not device:
            return web.json_response(
                {"error": "Device not found"},
                status=404,
            )

        # Notify HA entities of the change
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        async_dispatcher_send(request.app["hass"], SIGNAL_DEVICE_UPDATED, device_id)

        return web.json_response({
            "success": True,
            "device": {
                "id": device["id"],
                "name": device["name"],
                "subscription_active": device.get("subscription_active"),
                "subscription_last_verified_at": device.get("subscription_last_verified_at"),
                "notification_settings": device.get("notification_settings", {}),
            },
        })

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        """Remove/unpair device."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        success = await self.device_manager.async_remove_device(device_id)
        if not success:
            return web.json_response(
                {"error": "Device not found"},
                status=404,
            )

        return web.json_response({"success": True})


class DeviceTokenView(BaseAPIView):
    """Update device push token."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/token"
    name = "api:frigate_notify_bridge:device_token"

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Update device's FCM token."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Invalid JSON"},
                status=400,
            )

        fcm_token = data.get("fcm_token")
        if not fcm_token:
            return web.json_response(
                {"error": "Missing fcm_token"},
                status=400,
            )

        _LOGGER.debug(
            "Received FCM token update for device %s token_length=%s",
            device_id,
            len(fcm_token),
        )
        success = await self.device_manager.async_update_fcm_token(
            device_id,
            fcm_token,
        )

        if not success:
            return web.json_response(
                {"error": "Device not found"},
                status=404,
            )

        return web.json_response({"success": True})


class ConfigView(BaseAPIView):
    """Get bridge configuration."""

    url = f"{API_BASE_PATH}/config"
    name = "api:frigate_notify_bridge:config"

    async def get(self, request: web.Request) -> web.Response:
        """Get configuration for mobile app."""
        device_id = self._resolve_owned_device_id(request)
        if not device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        _LOGGER.debug("Returning config to device %s", device_id)

        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        push_provider = self.entry.data.get(CONF_PUSH_PROVIDER)
        device = await self.device_manager.async_get_device(device_id)

        config_response = {
            "frigate_url": frigate_url,
            "proxy_path": API_FRIGATE_PROXY_PATH,
            "push_provider": push_provider,
            "fcm_sender_id": self.coordinator.push_provider.get_sender_id(),
            "version": "0.1.0",
            "protocol_version": 2,
            "remote_ui_url": device.get("mobile_app_remote_ui_url") if device else None,
        }
        firebase_client_config = self.entry.data.get(CONF_FIREBASE_CLIENT_CONFIG)
        if firebase_client_config:
            config_response["firebase_options"] = firebase_client_config
        relay_url = self.entry.data.get(CONF_RELAY_URL)
        relay_bridge_id = self.entry.data.get(CONF_RELAY_BRIDGE_ID)
        relay_bridge_secret = self.entry.data.get(CONF_RELAY_BRIDGE_SECRET)
        e2e_key = self.entry.data.get(CONF_RELAY_E2E_KEY)
        if relay_url:
            config_response["relay_url"] = relay_url
        if relay_bridge_id:
            config_response["relay_bridge_id"] = relay_bridge_id
        if relay_bridge_secret:
            config_response["relay_bridge_secret"] = relay_bridge_secret
        if e2e_key:
            config_response["e2e_key"] = e2e_key
        return web.json_response(config_response)


class StatusView(BaseAPIView):
    """Get bridge status."""

    url = f"{API_BASE_PATH}/status"
    name = "api:frigate_notify_bridge:status"

    async def get(self, request: web.Request) -> web.Response:
        """Get bridge status.

        Always returns a minimal public status. Device count and push provider
        details are only included when a valid API token is presented, to avoid
        leaking configuration info to unauthenticated callers.
        """
        device_id = self._resolve_owned_device_id(request)
        base: dict = {
            "status": "ok",
            "version": "0.1.0",
        }
        if device_id:
            _LOGGER.debug("Returning authenticated status to device %s", device_id)
            devices = await self.device_manager.async_get_devices()
            base["push_provider"] = {
                "name": self.coordinator.push_provider.name,
                "initialized": self.coordinator.push_provider.is_initialized,
            }
            base["devices_count"] = len(devices)
        return web.json_response(base)


class TestNotificationView(BaseAPIView):
    """Send test notification."""

    url = f"{API_BASE_PATH}/test"
    name = "api:frigate_notify_bridge:test"

    async def post(self, request: web.Request) -> web.Response:
        """Send a test notification."""
        device_id = self._resolve_owned_device_id(request)
        if not device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        results = await self.coordinator.async_test_notification(device_id)

        if not results:
            return web.json_response(
                {"error": "No FCM token configured"},
                status=400,
            )

        result = results[0]
        return web.json_response({
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
        })


class WebRTCCredentialsView(BaseAPIView):
    """Get WebRTC credentials for Nabu Casa relay."""

    url = f"{API_BASE_PATH}/webrtc/credentials"
    name = "api:frigate_notify_bridge:webrtc_credentials"

    async def get(self, request: web.Request) -> web.Response:
        """Get WebRTC TURN/STUN credentials.

        This endpoint provides credentials for the Nabu Casa WebRTC relay
        if the user has Home Assistant Cloud configured.
        """
        device_id = self._resolve_owned_device_id(request)
        if not device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        hass = request.app["hass"]

        # Check if cloud is available
        if "cloud" not in hass.config.components:
            return web.json_response(
                {"error": "Home Assistant Cloud not configured"},
                status=404,
            )

        try:
            cloud = hass.data.get("cloud")
            if not cloud or not cloud.is_logged_in:
                return web.json_response(
                    {"error": "Not logged into Home Assistant Cloud"},
                    status=404,
                )

            # Get ICE servers using the stable HA 2026+ web_rtc API.
            # async_get_ice_servers returns the merged list: user config +
            # default STUN + Nabu Casa cloud-provided TURN servers.
            if "web_rtc" in hass.config.components:
                from homeassistant.components.web_rtc import async_get_ice_servers
                ice_servers = async_get_ice_servers(hass)
                ice_servers_json = [
                    {
                        "urls": s.urls if isinstance(s.urls, list) else [s.urls],
                        **(({"username": s.username} if s.username else {})),
                        **(({"credential": s.credential} if s.credential else {})),
                    }
                    for s in ice_servers
                ]
                return web.json_response({
                    "ice_servers": ice_servers_json,
                    "expires_in": 3600,
                })

            return web.json_response(
                {"error": "WebRTC not available"},
                status=404,
            )

        except Exception as e:
            _LOGGER.error("Failed to get WebRTC credentials: %s", e)
            return web.json_response(
                {"error": "Failed to get credentials"},
                status=500,
            )


class FrigateProxyView(BaseAPIView):
    """Proxy requests to the Frigate API.

    Catches all requests to /api/frigate_notify_bridge/frigate/{path} and
    forwards them to {CONF_FRIGATE_URL}/api/{path}. Uses per-device Frigate
    credentials when available, falling back to integration-level credentials.
    """

    url = f"{API_FRIGATE_PROXY_PATH}/{{path:.*}}"
    name = "api:frigate_notify_bridge:frigate_proxy"

    # Cache Frigate JWTs per device_id
    _frigate_tokens: dict[str, str] = {}

    async def _get_frigate_token(
        self,
        session: aiohttp.ClientSession,
        frigate_url: str,
        device_id: str,
    ) -> str | None:
        """Get or refresh a Frigate JWT for the given device."""
        # Check cache first
        cached = self._frigate_tokens.get(device_id)
        if cached:
            return cached

        # Get credentials: per-device first, then integration default
        username, password = self.device_manager.get_frigate_credentials(device_id)
        if not username:
            username = self.entry.data.get(CONF_FRIGATE_USERNAME)
            password = self.entry.data.get(CONF_FRIGATE_PASSWORD)

        if not username or not password:
            return None

        # Login to Frigate
        try:
            async with session.post(
                f"{frigate_url}/api/login",
                json={"user": username, "password": password},
            ) as resp:
                if resp.status == 200:
                    data = await _read_frigate_login_payload(resp)
                    token = _extract_frigate_token(resp, data)
                    if token:
                        self._frigate_tokens[device_id] = token
                        return token
                _LOGGER.warning(
                    "Frigate login failed for device %s: %s", device_id, resp.status
                )
        except Exception as e:
            _LOGGER.error("Frigate login error for device %s: %s", device_id, e)

        return None

    async def _proxy_request(
        self,
        request: web.Request,
        method: str,
    ) -> web.Response:
        """Proxy a request to the Frigate API."""
        device_id = self._resolve_owned_device_id(request)
        if not device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        if not frigate_url:
            return web.json_response(
                {"error": "Frigate URL not configured"}, status=503
            )

        # Build target URL
        path = request.match_info.get("path", "")
        query_string = request.query_string
        target = f"{frigate_url}/api/{path}"
        if query_string:
            target = f"{target}?{query_string}"

        # Read request body if present
        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = await request.read()

        session = async_get_clientsession(request.app["hass"])
        # Get Frigate auth token if needed
        headers = {}
        frigate_token = await self._get_frigate_token(
            session, frigate_url, device_id
        )
        if frigate_token:
            headers["Authorization"] = f"Bearer {frigate_token}"

        # Forward content-type from original request
        content_type = request.content_type
        if content_type and body:
            headers["Content-Type"] = content_type

        try:
            async with session.request(
                method, target, headers=headers, data=body
            ) as resp:
                # On 401, clear cached token and retry once
                if resp.status == 401 and device_id in self._frigate_tokens:
                    del self._frigate_tokens[device_id]
                    frigate_token = await self._get_frigate_token(
                        session, frigate_url, device_id
                    )
                    if frigate_token:
                        headers["Authorization"] = f"Bearer {frigate_token}"
                        async with session.request(
                            method, target, headers=headers, data=body
                        ) as retry_resp:
                            resp_body = await retry_resp.read()
                            return web.Response(
                                body=resp_body,
                                status=retry_resp.status,
                                headers=_proxy_response_headers(retry_resp),
                            )

                resp_body = await resp.read()
                return web.Response(
                    body=resp_body,
                    status=resp.status,
                    headers=_proxy_response_headers(resp),
                )
        except aiohttp.ClientError as e:
            _LOGGER.error("Frigate proxy error: %s", e)
            return web.json_response(
                {"error": "Failed to reach Frigate"}, status=502
            )

    async def get(self, request: web.Request, path: str = "") -> web.Response:
        """Handle GET."""
        return await self._proxy_request(request, "GET")

    async def post(self, request: web.Request, path: str = "") -> web.Response:
        """Handle POST."""
        return await self._proxy_request(request, "POST")

    async def put(self, request: web.Request, path: str = "") -> web.Response:
        """Handle PUT."""
        return await self._proxy_request(request, "PUT")

    async def delete(self, request: web.Request, path: str = "") -> web.Response:
        """Handle DELETE."""
        return await self._proxy_request(request, "DELETE")

    async def patch(self, request: web.Request, path: str = "") -> web.Response:
        """Handle PATCH."""
        return await self._proxy_request(request, "PATCH")


class FrigateMediaView(BaseAPIView):
    """Serve signed media URLs for notification attachments."""

    requires_auth = False
    url = f"{API_MEDIA_PROXY_PATH}/{{media_kind}}/{{media_id:.*}}"
    name = "api:frigate_notify_bridge:frigate_media_proxy"

    async def _get_frigate_token(
        self,
        session: aiohttp.ClientSession,
        frigate_url: str,
        device_id: str,
    ) -> str | None:
        """Get or refresh a Frigate JWT for the given device."""
        cached = FrigateProxyView._frigate_tokens.get(device_id)
        if cached:
            return cached

        username, password = self.device_manager.get_frigate_credentials(device_id)
        if not username:
            username = self.entry.data.get(CONF_FRIGATE_USERNAME)
            password = self.entry.data.get(CONF_FRIGATE_PASSWORD)

        if not username or not password:
            return None

        try:
            async with session.post(
                f"{frigate_url}/api/login",
                json={"user": username, "password": password},
            ) as resp:
                if resp.status == 200:
                    data = await _read_frigate_login_payload(resp)
                    token = _extract_frigate_token(resp, data)
                    if token:
                        FrigateProxyView._frigate_tokens[device_id] = token
                        return token
        except Exception as err:
            _LOGGER.error("Frigate media login error for device %s: %s", device_id, err)

        return None

    def _build_target_url(self, media_kind: str, media_id: str) -> str | None:
        """Translate a signed media path into the upstream Frigate URL."""
        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        if not frigate_url:
            return None

        if media_kind == "event_thumbnail":
            return f"{frigate_url}/api/events/{media_id}/thumbnail.jpg"
        if media_kind == "event_snapshot":
            return f"{frigate_url}/api/events/{media_id}/snapshot.jpg"
        if media_kind == "event_preview_gif":
            return f"{frigate_url}/api/events/{media_id}/preview?format=gif"
        if media_kind == "classification_image":
            parts = media_id.split("/")
            if len(parts) < 3:
                return None
            return (
                f"{frigate_url}/clips/"
                f"{'/'.join(quote(part, safe='') for part in parts)}"
            )
        if media_kind == "review_gif":
            return f"{frigate_url}/api/review/{media_id}/preview?format=gif"
        if media_kind == "review_mp4":
            return f"{frigate_url}/api/review/{media_id}/preview?format=mp4"
        if media_kind == "recording_clip":
            camera_name, start_ts, end_ts = (media_id.split("/", 2) + ["", ""])[:3]
            if not camera_name or not start_ts or not end_ts:
                return None
            return (
                f"{frigate_url}/api/{quote(camera_name, safe='')}/start/"
                f"{quote(start_ts, safe='')}/end/{quote(end_ts, safe='')}/clip.mp4"
            )
        if media_kind == "face_image":
            face_name, _, image_id = media_id.partition("/")
            if not face_name or not image_id:
                return None
            return (
                f"{frigate_url}/clips/faces/"
                f"{quote(face_name, safe='')}/{quote(image_id, safe='')}"
            )
        return None

    async def get(
        self,
        request: web.Request,
        media_kind: str,
        media_id: str,
    ) -> web.Response:
        """Serve a signed notification media URL."""
        device_id = request.query.get("device_id", "").strip()
        signature = request.query.get("sig", "").strip()
        expires_raw = request.query.get("expires", "").strip()
        try:
            expires = int(expires_raw)
        except (TypeError, ValueError):
            return web.json_response({"error": "Invalid expiration"}, status=400)

        if not device_id or not signature:
            return web.json_response({"error": "Missing signature"}, status=401)

        if not self.device_manager.validate_media_signature(
            device_id=device_id,
            media_kind=media_kind,
            media_id=media_id,
            expires=expires,
            signature=signature,
        ):
            return web.json_response({"error": "Invalid signature"}, status=401)

        target_url = self._build_target_url(media_kind, media_id)
        if not target_url:
            return web.json_response({"error": "Unsupported media"}, status=404)

        session = async_get_clientsession(request.app["hass"])
        headers: dict[str, str] = {}
        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        if frigate_url:
            token = await self._get_frigate_token(session, frigate_url, device_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        try:
            async with session.get(target_url, headers=headers, timeout=20) as resp:
                if resp.status == 401 and device_id in FrigateProxyView._frigate_tokens:
                    FrigateProxyView._frigate_tokens.pop(device_id, None)
                    token = await self._get_frigate_token(session, frigate_url, device_id)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        async with session.get(target_url, headers=headers, timeout=20) as retry:
                            retry_body = await retry.read()
                            return web.Response(
                                status=retry.status,
                                body=retry_body,
                                content_type=retry.content_type,
                            )

                body = await resp.read()
                return web.Response(
                    status=resp.status,
                    body=body,
                    content_type=resp.content_type,
                )
        except Exception as err:
            _LOGGER.error("Frigate media proxy error: %s", err)
            return web.json_response({"error": "Media proxy failed"}, status=502)


class FrigateCredentialsView(BaseAPIView):
    """Set per-device Frigate credentials for proxy authentication."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/frigate_credentials"
    name = "api:frigate_notify_bridge:frigate_credentials"

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Store Frigate credentials for a device."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return web.json_response(
                {"error": "Missing username or password"}, status=400
            )

        success = await self.device_manager.async_set_frigate_credentials(
            device_id, username, password
        )
        if not success:
            return web.json_response({"error": "Device not found"}, status=404)

        # Clear any cached Frigate token for this device so new creds are used
        FrigateProxyView._frigate_tokens.pop(device_id, None)

        return web.json_response({"success": True})


async def _resolve_frigate_api_url(
    hass: HomeAssistant, configured_url: str | None
) -> str | None:
    """Try to resolve the direct Frigate API URL.

    If the configured URL uses a non-standard port (e.g. nginx on 8971),
    probe the same host on port 5000 to find the direct API endpoint.
    The direct URL is preferred for mobile app local access (lower latency).
    """
    if not configured_url:
        return None

    from urllib.parse import urlparse

    parsed = urlparse(configured_url)
    host = parsed.hostname
    if not host:
        return configured_url

    # If already on port 5000, no detection needed
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port == 5000:
        return configured_url

    # Probe http://{host}:5000/api/version
    direct_url = f"http://{host}:5000"
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False),
        timeout=aiohttp.ClientTimeout(total=3),
    )
    try:
        async with session.get(f"{direct_url}/api/version") as resp:
            if resp.status == 200:
                body = await resp.text()
                # Validate it looks like a Frigate version
                import re
                if re.match(r"^\d+\.\d+\.\d+", body.strip()):
                    _LOGGER.info(
                        "Auto-detected Frigate API at %s (configured: %s)",
                        direct_url,
                        configured_url,
                    )
                    return direct_url
    except Exception:
        pass
    finally:
        await session.close()

    return configured_url


async def _get_tls_fingerprint(hass: HomeAssistant) -> str | None:
    """Compute SHA-256 fingerprint of HA's TLS certificate.

    Returns base64url-encoded (no padding) fingerprint, or None if no TLS.
    """
    import base64

    try:
        # Check if HA has SSL configured
        ssl_cert_path = hass.config.api.ssl_certificate if hass.config.api else None
        if not ssl_cert_path:
            return None

        # Read the PEM certificate and extract DER bytes
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding

        with open(ssl_cert_path, "rb") as f:
            pem_data = f.read()

        cert = x509.load_pem_x509_certificate(pem_data)
        der_bytes = cert.public_bytes(Encoding.DER)

        # SHA-256 of the DER-encoded certificate
        digest = hashlib.sha256(der_bytes).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    except Exception as e:
        _LOGGER.debug("Could not compute TLS fingerprint: %s", e)
        return None
