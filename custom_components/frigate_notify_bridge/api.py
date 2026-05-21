"""REST API for Frigate Notify Bridge mobile app communication."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import ssl
from typing import Any, TYPE_CHECKING
from urllib.parse import quote, urlencode
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
    API_ISSUES_PATH,
    API_ISSUE_DISMISS_PATH,
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
from .issues import ISSUE_DEVICE_NOTIFICATION_UNREACHABLE
from .qr_generator import (
    generate_pairing_qr_data,
    generate_qr_code_base64,
    generate_qr_code_image,
)
from .smart_rules import (
    SmartRule,
    apply_feedback_to_rule,
    discover_smart_rule_candidates,
    normalize_smart_rules,
    sessions_matching_rule,
    smart_rules_removed_or_disabled,
)

if TYPE_CHECKING:
    from .coordinator import FrigateNotifyCoordinator
    from .device_manager import DeviceManager
    from .issues import BridgeIssueManager

_LOGGER = logging.getLogger(__name__)
_SAMPLE_NOTIFICATION_IMAGE_ID = "bridge_sample_alert"
_SAMPLE_NOTIFICATION_IMAGE_PATH = (
    Path(__file__).resolve().parent / "brand" / "icon@2x.png"
)


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


def _proxy_request_headers(
    request: web.Request,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Copy safe client request headers upstream."""
    forwarded = {
        "accept",
        "accept-language",
        "cache-control",
        "if-match",
        "if-modified-since",
        "if-none-match",
        "if-range",
        "range",
    }
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in forwarded:
            headers[key] = value
    if extra:
        headers.update(extra)
    return headers


async def _stream_proxy_response(
    request: web.Request,
    response: aiohttp.ClientResponse,
) -> web.StreamResponse:
    """Stream an upstream response to the client without buffering the body."""
    downstream = web.StreamResponse(
        status=response.status,
        reason=response.reason,
        headers=_proxy_response_headers(response),
    )
    await downstream.prepare(request)
    async for chunk in response.content.iter_chunked(64 * 1024):
        await downstream.write(chunk)
    await downstream.write_eof()
    return downstream


async def async_setup_api(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: FrigateNotifyCoordinator,
    device_manager: DeviceManager,
    issue_manager: BridgeIssueManager | None = None,
) -> None:
    """Set up the REST API endpoints."""
    hass.http.register_view(PairingQRView(entry, coordinator, device_manager))
    hass.http.register_view(PairDeviceView(entry, coordinator, device_manager))
    hass.http.register_view(DevicesView(entry, coordinator, device_manager))
    hass.http.register_view(DeviceView(entry, coordinator, device_manager))
    hass.http.register_view(
        DeviceTokenView(
            entry,
            coordinator,
            device_manager,
            issue_manager=issue_manager,
        )
    )
    hass.http.register_view(DeviceMutesView(entry, coordinator, device_manager))
    hass.http.register_view(DeviceMuteDetailView(entry, coordinator, device_manager))
    hass.http.register_view(DeviceMuteModeView(entry, coordinator, device_manager))
    hass.http.register_view(SmartRulesView(entry, coordinator, device_manager))
    hass.http.register_view(SmartRuleCandidatesView(entry, coordinator, device_manager))
    hass.http.register_view(SmartRuleMatchesView(entry, coordinator, device_manager))
    hass.http.register_view(SmartRuleFeedbackView(entry, coordinator, device_manager))
    hass.http.register_view(ConfigView(entry, coordinator, device_manager))
    hass.http.register_view(StatusView(entry, coordinator, device_manager, issue_manager=issue_manager))
    hass.http.register_view(TestNotificationView(entry, coordinator, device_manager))
    hass.http.register_view(WebRTCCredentialsView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateProxyView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateLiveProxyView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateMediaView(entry, coordinator, device_manager))
    hass.http.register_view(FrigateCredentialsView(entry, coordinator, device_manager))
    if issue_manager is not None:
        hass.http.register_view(IssuesView(entry, coordinator, device_manager, issue_manager=issue_manager))
        hass.http.register_view(IssueDismissView(entry, coordinator, device_manager, issue_manager=issue_manager))

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
        format_type = request.query.get("format", "json")  # json, png, data, payload

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

        elif format_type == "payload":
            return web.json_response({
                "code": qr_data["code"],
                "expires_at": qr_data["expires_at"],
                "expires_in": qr_data["expires_in"],
                "using_cloud": qr_data.get("using_cloud", False),
                "webrtc_available": qr_data.get("webrtc_available", False),
                "payload": qr_data["payload"],
            })

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

        # Remove sensitive data from response; return as list (not dict) so
        # the mobile app can parse it uniformly with the standalone API.
        safe_devices = []
        for device_id, device in devices.items():
            safe_devices.append({
                "device_id": device_id,
                "id": device["id"],
                "name": device["name"],
                "platform": device["platform"],
                "app_version": device.get("app_version"),
                "subscription_active": device.get("subscription_active"),
                "subscription_last_verified_at": device.get("subscription_last_verified_at"),
                "paired_at": device["paired_at"],
                "last_seen": device.get("last_seen"),
                "last_notification_at": device.get("last_notification_at"),
                "last_failure_at": device.get("last_failure_at"),
                "failure_count_today": device.get("failure_count_today", 0),
                "last_error": device.get("last_error"),
                "notification_delivery_suspended": device.get(
                    "notification_delivery_suspended",
                    False,
                ),
                "notification_suspended_at": device.get("notification_suspended_at"),
                "notification_suspended_reason": device.get(
                    "notification_suspended_reason"
                ),
                "notification_token_confirmed_at": device.get(
                    "notification_token_confirmed_at"
                ),
            })

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

        # Return active mutes (prune expired)
        mutes = await self.device_manager.async_get_mutes(device_id)

        # Return device info without sensitive data
        return web.json_response({
            "id": device["id"],
            "name": device["name"],
            "platform": device["platform"],
            "subscription_active": device.get("subscription_active"),
            "subscription_last_verified_at": device.get("subscription_last_verified_at"),
            "notification_settings": device.get("notification_settings", {}),
            "mutes": mutes,
            "last_error": device.get("last_error"),
            "notification_delivery_suspended": device.get(
                "notification_delivery_suspended",
                False,
            ),
            "notification_suspended_at": device.get("notification_suspended_at"),
            "notification_suspended_reason": device.get("notification_suspended_reason"),
            "notification_token_confirmed_at": device.get(
                "notification_token_confirmed_at"
            ),
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

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: FrigateNotifyCoordinator,
        device_manager: DeviceManager,
        *,
        issue_manager: BridgeIssueManager | None = None,
    ) -> None:
        """Initialize token update view."""
        super().__init__(entry, coordinator, device_manager)
        self._issue_manager = issue_manager

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

        if self._issue_manager is not None:
            suspended = await self.device_manager.async_get_notification_suspended_devices()
            if not suspended:
                await self._issue_manager.async_clear_issue(
                    ISSUE_DEVICE_NOTIFICATION_UNREACHABLE
                )

        return web.json_response({"success": True})


class DeviceMutesView(BaseAPIView):
    """Manage notification mutes for a device."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/mutes"
    name = "api:frigate_notify_bridge:device_mutes"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """List active mutes for a device."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        mutes = await self.device_manager.async_get_mutes(device_id)
        return web.json_response({"mutes": mutes})

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Add a mute entry for a camera+label combo."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        camera = data.get("camera")
        label = data.get("label")
        duration_minutes = data.get("duration_minutes", 30)

        if not camera or not label:
            return web.json_response(
                {"error": "Missing camera or label"}, status=400
            )

        try:
            duration_minutes = int(duration_minutes)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "Invalid duration_minutes"}, status=400
            )
        duration_minutes = max(1, min(1440, duration_minutes))  # 1 min to 24 hours

        entry = await self.device_manager.async_add_mute(
            device_id, str(camera).strip(), str(label).strip(), duration_minutes
        )
        if entry is None:
            return web.json_response({"error": "Device not found"}, status=404)

        return web.json_response({"success": True, "mute": entry})

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        """Clear all mutes for a device."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        success = await self.device_manager.async_clear_mutes(device_id)
        if not success:
            return web.json_response({"error": "Device not found"}, status=404)

        return web.json_response({"success": True})


class DeviceMuteDetailView(BaseAPIView):
    """Remove a specific mute entry."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/mutes/{{camera}}/{{label}}"
    name = "api:frigate_notify_bridge:device_mute_detail"

    async def delete(
        self, request: web.Request, device_id: str, camera: str, label: str,
    ) -> web.Response:
        """Remove a specific mute for camera+label."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        success = await self.device_manager.async_remove_mute(device_id, camera, label)
        if not success:
            return web.json_response({"error": "Mute not found"}, status=404)

        return web.json_response({"success": True})


class DeviceMuteModeView(BaseAPIView):
    """Manage device or bridge-wide mute mode."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/mute_mode"
    name = "api:frigate_notify_bridge:device_mute_mode"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Return active mute mode and saved mute-mode settings."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        device = await self.device_manager.async_get_device(device_id)
        if not device:
            return web.json_response({"error": "Device not found"}, status=404)
        settings = self.device_manager.normalize_notification_settings(
            device.get("notification_settings")
        )
        return web.json_response({
            "mute_mode": self.device_manager.active_mute_mode_for_device(device_id),
            "settings": settings.get("mute_mode", {}),
        })

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Activate mute mode for this device or all devices."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        mode = await self.device_manager.async_activate_mute_mode(
            device_id,
            duration_minutes=data.get("duration_minutes", 30),
            scope=str(data.get("scope") or "device"),
            important_labels=(
                data.get("important_labels")
                if isinstance(data.get("important_labels"), list)
                else None
            ),
            important_sub_labels=(
                data.get("important_sub_labels")
                if isinstance(data.get("important_sub_labels"), list)
                else None
            ),
            live_activity_enabled=(
                bool(data.get("live_activity_enabled"))
                if "live_activity_enabled" in data
                else None
            ),
            reason=str(data.get("reason") or "manual"),
        )
        if mode is None:
            return web.json_response({"error": "Device not found"}, status=404)
        await self.coordinator.async_send_mute_mode_status(mode, ended=False)
        return web.json_response({"success": True, "mute_mode": mode})

    async def delete(self, request: web.Request, device_id: str) -> web.Response:
        """End active mute mode for this device or all devices."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        scope = request.query.get("scope")
        ended = await self.device_manager.async_end_mute_mode(device_id, scope=scope)
        if ended is None:
            return web.json_response({"error": "Device not found"}, status=404)
        await self.coordinator.async_send_mute_mode_status(ended, ended=True)
        return web.json_response({"success": True, "mute_mode": None})


async def _fetch_recent_frigate_events(
    request: web.Request,
    coordinator: FrigateNotifyCoordinator,
    entry: ConfigEntry,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch recent Frigate events for smart-rule mining."""
    frigate_url = entry.data.get(CONF_FRIGATE_URL)
    if not frigate_url:
        return []

    session = async_get_clientsession(request.app["hass"])
    headers: dict[str, str] = {}
    token = None
    try:
        token = await coordinator._async_get_frigate_access_token()  # noqa: SLF001
    except Exception as err:
        _LOGGER.debug("Could not get Frigate access token for smart rules: %s", err)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{str(frigate_url).rstrip('/')}/api/events?limit={limit}"
    async with session.get(url, headers=headers, ssl=False, timeout=15) as response:
        if response.status != 200:
            body = await response.text()
            raise web.HTTPBadGateway(
                reason=f"Frigate events request failed: HTTP {response.status} {body[:120]}"
            )
        payload = await response.json()
        return payload if isinstance(payload, list) else []


class SmartRulesView(BaseAPIView):
    """Read and update smart notification rules for a device."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/smart_rules"
    name = "api:frigate_notify_bridge:smart_rules"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Return configured smart rules."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        device = await self.device_manager.async_get_device(device_id)
        if not device:
            return web.json_response({"error": "Device not found"}, status=404)
        settings = self.device_manager.normalize_notification_settings(
            device.get("notification_settings")
        )
        return web.json_response({"smart_rules": settings.get("smart_rules", [])})

    async def patch(self, request: web.Request, device_id: str) -> web.Response:
        """Replace configured smart rules."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        device = await self.device_manager.async_get_device(device_id)
        if not device:
            return web.json_response({"error": "Device not found"}, status=404)
        previous_settings = self.device_manager.normalize_notification_settings(
            device.get("notification_settings")
        )
        previous_rules = previous_settings.get("smart_rules", [])
        next_rules = normalize_smart_rules(data.get("smart_rules"))
        ended_rules = smart_rules_removed_or_disabled(previous_rules, next_rules)
        settings = dict(device.get("notification_settings") or {})
        settings["smart_rules"] = next_rules
        updated = await self.device_manager.async_update_device(
            device_id,
            {"notification_settings": settings},
        )
        if ended_rules and updated:
            for rule in ended_rules:
                await self.coordinator.async_send_smart_mode_status(
                    updated,
                    rule,
                    ended=True,
                )
        return web.json_response({
            "success": True,
            "smart_rules": updated.get("notification_settings", {}).get("smart_rules", []),
            "ended_rules": ended_rules,
        })


class SmartRuleCandidatesView(BaseAPIView):
    """Discover likely smart rules from recent Frigate events."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/smart_rules/candidates"
    name = "api:frigate_notify_bridge:smart_rule_candidates"

    async def get(self, request: web.Request, device_id: str) -> web.Response:
        """Return mined smart-rule candidates."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            limit = int(request.query.get("limit", "500"))
        except (TypeError, ValueError):
            limit = 500
        limit = max(50, min(3000, limit))
        events = await _fetch_recent_frigate_events(
            request,
            self.coordinator,
            self.entry,
            limit=limit,
        )
        return web.json_response({
            "candidates": discover_smart_rule_candidates(events),
            "event_count": len(events),
        })


class SmartRuleMatchesView(BaseAPIView):
    """Preview historical sessions that match a candidate or configured rule."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/smart_rules/matches"
    name = "api:frigate_notify_bridge:smart_rule_matches"

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Return matching sessions for the posted rule."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        rule_data = data.get("rule")
        if not isinstance(rule_data, dict):
            return web.json_response({"error": "Missing rule"}, status=400)
        try:
            limit = int(data.get("limit", 500))
        except (TypeError, ValueError):
            limit = 500
        events = await _fetch_recent_frigate_events(
            request,
            self.coordinator,
            self.entry,
            limit=max(50, min(3000, limit)),
        )
        rule = SmartRule.from_dict(rule_data)
        return web.json_response({
            "matches": sessions_matching_rule(rule, events),
            "event_count": len(events),
        })


class SmartRuleFeedbackView(BaseAPIView):
    """Apply validation feedback to a smart rule."""

    url = f"{API_BASE_PATH}/devices/{{device_id}}/smart_rules/feedback"
    name = "api:frigate_notify_bridge:smart_rule_feedback"

    async def post(self, request: web.Request, device_id: str) -> web.Response:
        """Return an updated rule after user feedback."""
        resolved_device_id = self._resolve_owned_device_id(request, device_id)
        if resolved_device_id != device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        rule_data = data.get("rule")
        feedback = data.get("feedback")
        if not isinstance(rule_data, dict) or not isinstance(feedback, dict):
            return web.json_response({"error": "Missing rule or feedback"}, status=400)
        updated_rule = apply_feedback_to_rule(rule_data, feedback)
        return web.json_response({"rule": updated_rule.to_dict()})


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

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: FrigateNotifyCoordinator,
        device_manager: DeviceManager,
        *,
        issue_manager: BridgeIssueManager | None = None,
    ) -> None:
        """Initialize the view."""
        super().__init__(entry, coordinator, device_manager)
        self._issue_manager = issue_manager

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
            "capabilities": {
                "live_mse_proxy": True,
            },
        }
        if device_id:
            _LOGGER.debug("Returning authenticated status to device %s", device_id)
            devices = await self.device_manager.async_get_devices()
            provider_initialized = self.coordinator.push_provider.is_initialized
            base["push_provider"] = {
                "name": self.coordinator.push_provider.name,
                "initialized": provider_initialized,
            }
            base["push_provider_status"] = (
                f"{self.coordinator.push_provider.name}/available"
                if provider_initialized
                else f"{self.coordinator.push_provider.name}/unavailable"
            )
            base["mqtt_connected"] = bool(self.coordinator.mqtt_subscribed)
            base["last_event_at"] = self.coordinator.last_event_at
            base["active_issue_count"] = (
                len(self._issue_manager.active_issues)
                if self._issue_manager is not None
                else 0
            )
            base["device_count"] = len(devices)
            base["devices_count"] = len(devices)
            base["device_failure_count"] = sum(
                1 for d in devices.values() if d.get("last_error") is not None
            )
            base["notification_suspended_device_count"] = sum(
                1
                for d in devices.values()
                if d.get("notification_delivery_suspended")
            )
        return web.json_response(base)


class IssuesView(BaseAPIView):
    """List active bridge issues."""

    url = f"{API_BASE_PATH}/{API_ISSUES_PATH}"
    name = "api:frigate_notify_bridge:issues"
    requires_auth = True

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: FrigateNotifyCoordinator,
        device_manager: DeviceManager,
        *,
        issue_manager: BridgeIssueManager,
    ) -> None:
        """Initialize the view."""
        super().__init__(entry, coordinator, device_manager)
        self._issue_manager = issue_manager

    async def get(self, request: web.Request) -> web.Response:
        """Return active issues."""
        return web.json_response({
            "issues": self._issue_manager.active_issues,
        })


class IssueDismissView(BaseAPIView):
    """Dismiss a specific bridge issue."""

    url = f"{API_BASE_PATH}/{API_ISSUES_PATH}/{{issue_id}}/{API_ISSUE_DISMISS_PATH}"
    name = "api:frigate_notify_bridge:issue_dismiss"
    requires_auth = True

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: FrigateNotifyCoordinator,
        device_manager: DeviceManager,
        *,
        issue_manager: BridgeIssueManager,
    ) -> None:
        """Initialize the view."""
        super().__init__(entry, coordinator, device_manager)
        self._issue_manager = issue_manager

    async def post(self, request: web.Request, issue_id: str) -> web.Response:
        """Dismiss an issue by ID."""
        await self._issue_manager.async_dismiss_issue(issue_id)
        return web.json_response({"ok": True})


class TestNotificationView(BaseAPIView):
    """Send test notification."""

    url = f"{API_BASE_PATH}/test"
    name = "api:frigate_notify_bridge:test"

    async def post(self, request: web.Request) -> web.Response:
        """Send a test notification with optional parameters."""
        device_id = self._resolve_owned_device_id(request)
        if not device_id:
            return web.json_response(
                {"error": "Unauthorized"},
                status=401,
            )

        # Parse query parameters for image type and recent event usage
        image_type = request.query.get("image_type", "thumbnail")
        use_recent_event_str = request.query.get("use_recent_event", "true")
        use_recent_event = use_recent_event_str.lower() in ("true", "1", "yes")

        try:
            device = await self.device_manager.async_get_device(device_id)
            if not device:
                return web.json_response(
                    {"success": False, "error": "Device not found"},
                    status=404,
                )

            _, metadata = await self.coordinator.build_test_notification_payload(
                device,
                image_type=image_type,
                use_recent_event=use_recent_event,
            )
            results = await self.coordinator.async_test_notification(
                device_id,
                image_type=image_type,
                use_recent_event=use_recent_event,
            )
            first_result = results[0] if results else None
            if first_result is None or not first_result.success:
                return web.json_response({
                    "success": False,
                    "error": first_result.error if first_result else "No push token available for device",
                    "source": metadata.get("source"),
                    "has_image": metadata.get("has_image"),
                    "message": "Test notification failed",
                }, status=500)

            return web.json_response({
                "success": True,
                "message": f"Test notification sent with {image_type} image",
                "source": metadata.get("source"),
                "has_image": metadata.get("has_image"),
            })
        except Exception as err:
            _LOGGER.error("Failed to send test notification: %s", err)
            return web.json_response({
                "success": False,
                "error": str(err),
            }, status=500)


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

        # Build target URL. Most bridge proxy requests target Frigate's `/api/*`
        # namespace, but live WebRTC websocket requests are rooted at `/live/*`.
        path = request.match_info.get("path", "").lstrip("/")
        query_string = request.query_string
        if path.startswith("live/"):
            target = f"{frigate_url}/{path}"
        else:
            target = f"{frigate_url}/api/{path}"
        if query_string:
            target = f"{target}?{query_string}"

        session = async_get_clientsession(request.app["hass"])
        headers = _proxy_request_headers(request)
        frigate_token = await self._get_frigate_token(
            session, frigate_url, device_id
        )
        if frigate_token:
            headers["Authorization"] = f"Bearer {frigate_token}"
        else:
            _LOGGER.warning(
                "Frigate proxy proceeding without Frigate token: device_id=%s path=%s",
                device_id,
                path,
            )

        ws_probe = web.WebSocketResponse()
        if method == "GET" and ws_probe.can_prepare(request).ok:
            return await self._proxy_websocket(
                request,
                target=target,
                session=session,
                headers=headers,
                device_id=device_id,
            )

        # Read request body if present
        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = await request.read()

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
                            if method == "GET":
                                return await _stream_proxy_response(
                                    request, retry_resp
                                )
                            resp_body = await retry_resp.read()
                            return web.Response(
                                body=resp_body,
                                status=retry_resp.status,
                                headers=_proxy_response_headers(retry_resp),
                            )

                if method == "GET" and resp.status in (500, 502, 503, 504):
                    await resp.read()
                    await asyncio.sleep(0.25)
                    async with session.request(
                        method, target, headers=headers, data=body
                    ) as retry_resp:
                        if method == "GET":
                            if retry_resp.status >= 400:
                                _LOGGER.warning(
                                    "Frigate proxy retry response: device_id=%s method=%s target=%s status=%s",
                                    device_id,
                                    method,
                                    target,
                                    retry_resp.status,
                                )
                            return await _stream_proxy_response(
                                request, retry_resp
                            )
                        retry_body = await retry_resp.read()
                        if retry_resp.status >= 400:
                            _LOGGER.warning(
                                "Frigate proxy retry response: device_id=%s method=%s target=%s status=%s",
                                device_id,
                                method,
                                target,
                                retry_resp.status,
                            )
                        return web.Response(
                            body=retry_body,
                            status=retry_resp.status,
                            headers=_proxy_response_headers(retry_resp),
                        )

                if resp.status >= 400:
                    _LOGGER.warning(
                        "Frigate proxy response: device_id=%s method=%s target=%s status=%s",
                        device_id,
                        method,
                        target,
                        resp.status,
                    )
                if method == "GET":
                    return await _stream_proxy_response(request, resp)
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

    async def _proxy_websocket(
        self,
        request: web.Request,
        *,
        target: str,
        session: aiohttp.ClientSession,
        headers: dict[str, str],
        device_id: str,
    ) -> web.StreamResponse:
        """Proxy a websocket request to Frigate/go2rtc."""
        _LOGGER.warning(
            "Frigate websocket proxy opening: device_id=%s target=%s",
            device_id,
            target,
        )
        server_ws = web.WebSocketResponse(heartbeat=30)
        await server_ws.prepare(request)

        upstream_ws = None
        try:
            upstream_ws = await session.ws_connect(
                target,
                headers=headers,
                heartbeat=30,
                autoclose=False,
                autoping=True,
            )

            async def client_to_upstream() -> None:
                async for message in server_ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(message.data)
                            msg_type = payload.get("type")
                        except Exception:  # pragma: no cover - debug logging only
                            msg_type = "unparsed"
                        _LOGGER.warning(
                            "Frigate websocket proxy client->upstream: device_id=%s type=%s",
                            device_id,
                            msg_type,
                        )
                        await upstream_ws.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        _LOGGER.warning(
                            "Frigate websocket proxy client->upstream: device_id=%s type=binary size=%s",
                            device_id,
                            len(message.data),
                        )
                        await upstream_ws.send_bytes(message.data)
                    elif message.type == aiohttp.WSMsgType.CLOSE:
                        _LOGGER.warning(
                            "Frigate websocket proxy client->upstream: device_id=%s type=close",
                            device_id,
                        )
                        await upstream_ws.close()

            async def upstream_to_client() -> None:
                async for message in upstream_ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(message.data)
                            msg_type = payload.get("type")
                        except Exception:  # pragma: no cover - debug logging only
                            msg_type = "unparsed"
                        _LOGGER.warning(
                            "Frigate websocket proxy upstream->client: device_id=%s type=%s payload=%s",
                            device_id,
                            msg_type,
                            message.data[:300],
                        )
                        await server_ws.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        _LOGGER.warning(
                            "Frigate websocket proxy upstream->client: device_id=%s type=binary size=%s",
                            device_id,
                            len(message.data),
                        )
                        await server_ws.send_bytes(message.data)
                    elif message.type == aiohttp.WSMsgType.CLOSE:
                        _LOGGER.warning(
                            "Frigate websocket proxy upstream->client: device_id=%s type=close",
                            device_id,
                        )
                        await server_ws.close()

            await asyncio.gather(client_to_upstream(), upstream_to_client())
        except aiohttp.WSServerHandshakeError as err:
            _LOGGER.error("Frigate websocket proxy handshake failed: %s", err)
            if upstream_ws is None:
                if device_id in self._frigate_tokens:
                    self._frigate_tokens.pop(device_id, None)
                await server_ws.send_json({"error": "WebRTC handshake failed"})
        except aiohttp.ClientError as err:
            _LOGGER.error("Frigate websocket proxy error: %s", err)
            await server_ws.send_json({"error": "Failed to reach Frigate"})
        finally:
            if upstream_ws is not None and not upstream_ws.closed:
                await upstream_ws.close()
            if not server_ws.closed:
                await server_ws.close()
            _LOGGER.warning(
                "Frigate websocket proxy closed: device_id=%s target=%s",
                device_id,
                target,
            )

        return server_ws

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


class FrigateLiveProxyView(FrigateProxyView):
    """Proxy signed live websocket/media requests to Frigate without HA auth."""

    requires_auth = False
    url = f"{API_BASE_PATH}/live/{{path:.*}}"
    name = "api:frigate_notify_bridge:frigate_live_proxy"

    @staticmethod
    def _build_signed_live_media_id(
        path: str,
        request: web.Request,
    ) -> str:
        """Build the canonical signed path used by the mobile app."""
        filtered_query = [
            (key, value)
            for key, value in request.query.items()
            if key not in {"device_id", "expires", "sig"}
        ]
        filtered_query.sort(key=lambda item: (item[0], item[1]))
        live_path = f"live/{path.lstrip('/')}"
        if not filtered_query:
            return live_path
        return f"{live_path}?{urlencode(filtered_query)}"

    def _resolve_signed_device_id(
        self,
        request: web.Request,
        path: str,
    ) -> str | None:
        """Resolve a device using the signed live proxy URL."""
        if not path.startswith("mse/"):
            _LOGGER.warning("Rejected unsigned live proxy path: %s", path)
            return None

        device_id = request.query.get("device_id", "").strip()
        signature = request.query.get("sig", "").strip()
        expires_raw = request.query.get("expires", "").strip()
        if not device_id or not signature or not expires_raw:
            return None

        try:
            expires = int(expires_raw)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Invalid live proxy expiration for device %s path=%s value=%s",
                device_id,
                path,
                expires_raw,
            )
            return None

        media_id = self._build_signed_live_media_id(path, request)
        if not self.device_manager.validate_media_signature(
            device_id=device_id,
            media_kind="live_proxy",
            media_id=media_id,
            expires=expires,
            signature=signature,
        ):
            _LOGGER.warning(
                "Invalid live proxy signature: device_id=%s path=%s media_id=%s",
                device_id,
                path,
                media_id,
            )
            return None

        return device_id

    async def _proxy_live_request(
        self,
        request: web.Request,
        method: str,
    ) -> web.Response:
        """Proxy a signed live request to the Frigate live namespace."""
        path = request.match_info.get("path", "").lstrip("/")
        device_id = self._resolve_signed_device_id(request, path)
        if not device_id:
            return web.json_response({"error": "Unauthorized"}, status=401)

        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        if not frigate_url:
            return web.json_response(
                {"error": "Frigate URL not configured"}, status=503
            )

        query_pairs = [
            (key, value)
            for key, value in request.query.items()
            if key not in {"device_id", "expires", "sig"}
        ]
        target = f"{frigate_url}/live/{path}"
        if query_pairs:
            target = f"{target}?{urlencode(query_pairs)}"

        session = async_get_clientsession(request.app["hass"])
        headers = _proxy_request_headers(request)
        frigate_token = await self._get_frigate_token(
            session, frigate_url, device_id
        )
        if frigate_token:
            headers["Authorization"] = f"Bearer {frigate_token}"

        ws_probe = web.WebSocketResponse()
        if method == "GET" and ws_probe.can_prepare(request).ok:
            return await self._proxy_websocket(
                request,
                target=target,
                session=session,
                headers=headers,
                device_id=device_id,
            )

        body = None
        if method in ("POST", "PUT", "PATCH"):
            body = await request.read()

        content_type = request.content_type
        if content_type and body:
            headers["Content-Type"] = content_type

        try:
            async with session.request(
                method, target, headers=headers, data=body
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.warning(
                        "Frigate live proxy response: device_id=%s method=%s target=%s status=%s",
                        device_id,
                        method,
                        target,
                        resp.status,
                    )
                if method == "GET":
                    return await _stream_proxy_response(request, resp)
                resp_body = await resp.read()
                return web.Response(
                    body=resp_body,
                    status=resp.status,
                    headers=_proxy_response_headers(resp),
                )
        except aiohttp.ClientError as err:
            _LOGGER.error("Frigate live proxy error: %s", err)
            return web.json_response(
                {"error": "Failed to reach Frigate"}, status=502
            )

    async def get(self, request: web.Request, path: str = "") -> web.Response:
        """Handle signed GET live requests."""
        return await self._proxy_live_request(request, "GET")

    async def post(self, request: web.Request, path: str = "") -> web.Response:
        """Handle signed POST live requests."""
        return await self._proxy_live_request(request, "POST")

    async def put(self, request: web.Request, path: str = "") -> web.Response:
        """Handle signed PUT live requests."""
        return await self._proxy_live_request(request, "PUT")

    async def delete(self, request: web.Request, path: str = "") -> web.Response:
        """Handle signed DELETE live requests."""
        return await self._proxy_live_request(request, "DELETE")

    async def patch(self, request: web.Request, path: str = "") -> web.Response:
        """Handle signed PATCH live requests."""
        return await self._proxy_live_request(request, "PATCH")


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
        if media_kind == "sample_image" and media_id == _SAMPLE_NOTIFICATION_IMAGE_ID:
            return str(_SAMPLE_NOTIFICATION_IMAGE_PATH)

        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        if not frigate_url:
            return None

        if media_kind == "event_thumbnail":
            return f"{frigate_url}/api/events/{media_id}/thumbnail.jpg"
        if media_kind == "event_snapshot":
            return f"{frigate_url}/api/events/{media_id}/snapshot.jpg"
        if media_kind == "event_snapshot_bbox":
            return f"{frigate_url}/api/events/{media_id}/snapshot.jpg?bbox=1"
        if media_kind == "event_clip":
            return f"{frigate_url}/api/events/{media_id}/clip.mp4"
        if media_kind == "event_preview_gif":
            return f"{frigate_url}/api/events/{media_id}/preview.gif"
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
        if media_kind == "review_thumbnail":
            return None
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

    @staticmethod
    def _resolve_thumb_path_target(
        frigate_url: str,
        thumb_path: str | None,
    ) -> str | None:
        """Translate a Frigate review thumb_path into a fetchable upstream URL."""
        if not thumb_path:
            return None

        normalized = thumb_path.strip()
        if not normalized:
            return None

        if normalized.startswith("/api/") or normalized.startswith("/clips/"):
            return f"{frigate_url}{normalized}"
        if normalized.startswith("/media/frigate/"):
            return f"{frigate_url}{normalized.removeprefix('/media/frigate')}"
        if normalized.startswith("media/frigate/"):
            return f"{frigate_url}/{normalized.removeprefix('media/frigate/')}"
        return None

    async def _resolve_review_thumbnail_url(
        self,
        session: aiohttp.ClientSession,
        frigate_url: str,
        review_id: str,
        headers: dict[str, str],
    ) -> str | None:
        """Resolve the real thumbnail path for a review item."""
        try:
            async with session.get(
                f"{frigate_url}/api/review/{review_id}",
                headers=headers,
                timeout=20,
            ) as resp:
                if resp.status == 401 and headers.get("Authorization"):
                    return None
                if resp.status >= 400:
                    _LOGGER.warning(
                        "Review thumbnail lookup failed: review_id=%s status=%s",
                        review_id,
                        resp.status,
                    )
                    return None
                payload = await resp.json()
        except Exception as err:
            _LOGGER.error(
                "Review thumbnail lookup error for %s: %s",
                review_id,
                err,
            )
            return None

        detections = payload.get("data", {}).get("detections") or []
        if detections:
            return f"{frigate_url}/api/events/{detections[0]}/thumbnail.jpg"

        thumb_path = payload.get("thumb_path")
        thumb_target = self._resolve_thumb_path_target(frigate_url, thumb_path)
        if thumb_target:
            return thumb_target

        return f"{frigate_url}/api/review/{review_id}/preview?format=gif"

    async def _resolve_review_fallback_url(
        self,
        session: aiohttp.ClientSession,
        frigate_url: str,
        review_id: str,
        headers: dict[str, str],
    ) -> str | None:
        """Resolve a stable fallback image for a review preview."""
        try:
            async with session.get(
                f"{frigate_url}/api/review/{review_id}",
                headers=headers,
                timeout=20,
            ) as resp:
                if resp.status >= 400:
                    return None
                payload = await resp.json()
        except Exception:
            return None

        detections = payload.get("data", {}).get("detections") or []
        if detections:
            return f"{frigate_url}/api/events/{detections[0]}/thumbnail.jpg"
        thumb_target = self._resolve_thumb_path_target(
            frigate_url,
            payload.get("thumb_path"),
        )
        if thumb_target:
            return thumb_target
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
        has_signature = bool(device_id and signature and expires_raw)

        if has_signature:
            try:
                expires = int(expires_raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "Invalid expiration"}, status=400)

            if not self.device_manager.validate_media_signature(
                device_id=device_id,
                media_kind=media_kind,
                media_id=media_id,
                expires=expires,
                signature=signature,
            ):
                expected = self.device_manager.create_media_signature(
                    device_id,
                    media_kind,
                    media_id,
                    expires,
                )
                _LOGGER.warning(
                    "Invalid media signature: device_id=%s media_kind=%s media_id=%s expires=%s received=%s expected=%s",
                    device_id,
                    media_kind,
                    media_id,
                    expires,
                    signature,
                    expected,
                )
                return web.json_response({"error": "Invalid signature"}, status=401)
        else:
            authenticated_device_id = self._resolve_owned_device_id(
                request,
                requested_device_id=device_id or None,
            )
            if not authenticated_device_id:
                return web.json_response({"error": "Missing signature"}, status=401)
            device_id = authenticated_device_id

        session = async_get_clientsession(request.app["hass"])
        headers = _proxy_request_headers(request)
        frigate_url = self.entry.data.get(CONF_FRIGATE_URL)
        if frigate_url:
            token = await self._get_frigate_token(session, frigate_url, device_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"

        target_url = self._build_target_url(media_kind, media_id)
        if media_kind == "review_thumbnail":
            if not frigate_url:
                return web.json_response({"error": "Unsupported media"}, status=404)
            target_url = await self._resolve_review_thumbnail_url(
                session,
                frigate_url,
                media_id,
                headers,
            )
        if not target_url:
            return web.json_response({"error": "Unsupported media"}, status=404)

        if media_kind == "sample_image":
            sample_path = Path(target_url)
            if not sample_path.exists():
                return web.json_response({"error": "Sample image missing"}, status=404)
            return web.FileResponse(path=sample_path)

        try:
            async with session.get(target_url, headers=headers, timeout=20) as resp:
                if resp.status == 401 and device_id in FrigateProxyView._frigate_tokens:
                    FrigateProxyView._frigate_tokens.pop(device_id, None)
                    token = await self._get_frigate_token(session, frigate_url, device_id)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        async with session.get(target_url, headers=headers, timeout=20) as retry:
                            return await _stream_proxy_response(request, retry)

                if resp.status >= 400 and media_kind == "review_gif":
                    fallback_url = await self._resolve_review_fallback_url(
                        session,
                        frigate_url,
                        media_id,
                        headers,
                    )
                    if fallback_url:
                        async with session.get(
                            fallback_url,
                            headers=headers,
                            timeout=20,
                        ) as fallback_resp:
                            return await _stream_proxy_response(
                                request, fallback_resp
                            )

                if resp.status >= 400:
                    _LOGGER.warning(
                        "Frigate media proxy response: device_id=%s media_kind=%s media_id=%s target=%s status=%s",
                        device_id,
                        media_kind,
                        media_id,
                        target_url,
                        resp.status,
                    )
                return await _stream_proxy_response(request, resp)
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
