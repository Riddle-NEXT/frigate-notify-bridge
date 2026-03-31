"""REST API routes for standalone mode."""

import base64
import json
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


def setup_routes(app: web.Application) -> None:
    """Set up API routes."""
    app.router.add_get("/health", health_check)
    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/pairing/qr", get_pairing_qr)
    app.router.add_post("/api/pair", pair_device)
    # Device management (device API token OR admin token)
    app.router.add_get("/api/devices", list_devices)
    app.router.add_get("/api/devices/{device_id}", get_device)
    app.router.add_patch("/api/devices/{device_id}", update_device)
    app.router.add_delete("/api/devices/{device_id}", delete_device)
    app.router.add_post("/api/devices/{device_id}/token", update_token)
    # Issues (admin token required)
    app.router.add_get("/api/issues", list_issues)
    app.router.add_post("/api/issues/{issue_id}/dismiss", dismiss_issue)
    # Config and test
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/test", test_notification)


def _get_bearer_token(request: web.Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def _validate_api_token(request: web.Request) -> str | None:
    """Validate device API token. Returns device_id or None."""
    token = _get_bearer_token(request)
    if not token:
        return None
    device_store = request.app["device_store"]
    return device_store.validate_api_token(token)


def _validate_admin_token(request: web.Request) -> bool:
    """Validate admin token. Returns True if valid."""
    token = _get_bearer_token(request)
    if not token:
        return False
    config = request.app["config"]
    device_store = request.app["device_store"]
    return device_store.validate_admin_token(token, config.admin_token)


def _validate_device_or_admin(request: web.Request, device_id: str) -> bool:
    """Return True if the request is from the device itself OR an admin."""
    if _validate_admin_token(request):
        return True
    token_device_id = _validate_api_token(request)
    return token_device_id == device_id


async def health_check(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def get_status(request: web.Request) -> web.Response:
    """Get bridge status — mirrors HA StatusView fields."""
    device_store = request.app["device_store"]
    push_service = request.app["push_service"]
    issue_manager = request.app.get("issue_manager")
    devices = await device_store.get_all_devices()

    # Count devices with failures today
    device_failure_count = sum(
        1 for d in devices.values()
        if d.get("failure_count_today", 0) > 0
    )

    provider = push_service._provider
    provider_initialized = provider.is_initialized if provider else False
    provider_last_error = provider.last_error if provider else None

    status: dict[str, Any] = {
        "status": "ok",
        "version": "0.1.0",
        "push_provider": push_service._provider_name,
        "push_provider_status": "ok" if provider_initialized else "error",
        "push_provider_error": provider_last_error,
        "mqtt_connected": request.app.get("mqtt_connected", False),
        "last_event_at": request.app.get("last_event_at"),
        "devices_count": len(devices),
        "device_failure_count": device_failure_count,
        "active_issue_count": issue_manager.active_issue_count if issue_manager else 0,
    }

    return web.json_response(status)


async def get_pairing_qr(request: web.Request) -> web.Response:
    """Generate pairing QR code data."""
    device_store = request.app["device_store"]
    push_service = request.app["push_service"]
    config = request.app["config"]

    pairing_info = device_store.generate_pairing_code()

    qr_payload = {
        "v": 1,
        "t": pairing_info["token"],
        "c": pairing_info["code"],
        "e": pairing_info["expires_in"],
        "s": {
            "i": f"http://{config.mqtt_host}:{config.api_port}",
            "x": config.external_url or None,
            "p": "/api",
        },
        "f": {
            "u": config.frigate_url,
            "a": bool(config.frigate_username),
        },
        "n": {
            "p": config.push_provider,
            "s": push_service.get_sender_id(),
        },
    }

    payload_json = json.dumps(qr_payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
    qr_url = f"frigate-mobile://pair?d={payload_b64}"

    format_type = request.query.get("format", "json")

    if format_type == "data":
        try:
            import qrcode
            import io

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=2,
            )
            qr.add_data(qr_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="#1A73E8", back_color="white")

            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            buffer.seek(0)
            image_b64 = base64.b64encode(buffer.getvalue()).decode()

            return web.json_response({
                "code": pairing_info["code"],
                "expires_at": pairing_info["expires_at"],
                "expires_in": pairing_info["expires_in"],
                "image": f"data:image/png;base64,{image_b64}",
            })
        except ImportError:
            return web.json_response({
                "code": pairing_info["code"],
                "url": qr_url,
                "expires_at": pairing_info["expires_at"],
                "expires_in": pairing_info["expires_in"],
                "error": "QR code generation unavailable - install qrcode package",
            })

    return web.json_response({
        "code": pairing_info["code"],
        "url": qr_url,
        "expires_at": pairing_info["expires_at"],
        "expires_in": pairing_info["expires_in"],
    })


async def pair_device(request: web.Request) -> web.Response:
    device_store = request.app["device_store"]
    push_service = request.app["push_service"]
    config = request.app["config"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token_or_code = data.get("token") or data.get("code")
    if not token_or_code:
        return web.json_response({"error": "Missing token or code"}, status=400)

    device_info = {
        "name": data.get("name", "Unknown Device"),
        "platform": data.get("platform", "unknown"),
        "fcm_token": data.get("fcm_token"),
        "app_version": data.get("app_version"),
    }

    try:
        result = await device_store.complete_pairing(token_or_code, device_info)
        return web.json_response({
            "success": True,
            "device_id": result["device_id"],
            "api_token": result["api_token"],
            "config": {
                "frigate_url": config.frigate_url,
                "push_provider": config.push_provider,
                "fcm_sender_id": push_service.get_sender_id(),
            },
        })
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)


async def list_devices(request: web.Request) -> web.Response:
    """List all devices (admin token required)."""
    if not _validate_admin_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    device_store = request.app["device_store"]
    devices = await device_store.get_all_devices()

    device_list = []
    for device_id, device in devices.items():
        device_list.append({
            "id": device["id"],
            "name": device["name"],
            "platform": device["platform"],
            "paired_at": device["paired_at"],
            "last_seen": device.get("last_seen"),
            "notification_enabled": device.get("notification_settings", {}).get("enabled", True),
            # Delivery stats
            "last_notification_at": device.get("last_notification_at"),
            "last_failure_at": device.get("last_failure_at"),
            "failure_count_today": device.get("failure_count_today", 0),
            "last_error": device.get("last_error"),
        })

    return web.json_response({
        "devices": device_list,
        "count": len(device_list),
    })


async def get_device(request: web.Request) -> web.Response:
    """Get device details."""
    device_id = request.match_info["device_id"]

    if not _validate_device_or_admin(request, device_id):
        return web.json_response({"error": "Unauthorized"}, status=401)

    device_store = request.app["device_store"]
    device = await device_store.get_device(device_id)

    if not device:
        return web.json_response({"error": "Device not found"}, status=404)

    return web.json_response({
        "id": device["id"],
        "name": device["name"],
        "platform": device["platform"],
        "notification_settings": device.get("notification_settings", {}),
        "last_notification_at": device.get("last_notification_at"),
        "last_failure_at": device.get("last_failure_at"),
        "failure_count_today": device.get("failure_count_today", 0),
        "last_error": device.get("last_error"),
    })


async def update_device(request: web.Request) -> web.Response:
    """Update device settings."""
    device_id = request.match_info["device_id"]

    if not _validate_device_or_admin(request, device_id):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        updates = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    device_store = request.app["device_store"]
    device = await device_store.update_device(device_id, updates)

    if not device:
        return web.json_response({"error": "Device not found"}, status=404)

    return web.json_response({
        "success": True,
        "device": {
            "id": device["id"],
            "name": device["name"],
            "notification_settings": device.get("notification_settings", {}),
        },
    })


async def delete_device(request: web.Request) -> web.Response:
    """Remove/unpair device."""
    device_id = request.match_info["device_id"]

    if not _validate_device_or_admin(request, device_id):
        return web.json_response({"error": "Unauthorized"}, status=401)

    device_store = request.app["device_store"]
    success = await device_store.remove_device(device_id)

    if not success:
        return web.json_response({"error": "Device not found"}, status=404)

    return web.json_response({"success": True})


async def update_token(request: web.Request) -> web.Response:
    """Update device push token."""
    device_id = request.match_info["device_id"]

    if not _validate_device_or_admin(request, device_id):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    fcm_token = data.get("fcm_token")
    if not fcm_token:
        return web.json_response({"error": "Missing fcm_token"}, status=400)

    device_store = request.app["device_store"]
    device = await device_store.update_device(device_id, {"fcm_token": fcm_token})

    if not device:
        return web.json_response({"error": "Device not found"}, status=404)

    return web.json_response({"success": True})


async def list_issues(request: web.Request) -> web.Response:
    """List active bridge issues (admin token required)."""
    if not _validate_admin_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    issue_manager = request.app.get("issue_manager")
    if not issue_manager:
        return web.json_response({"issues": [], "count": 0})

    issues = issue_manager.active_issues
    return web.json_response({
        "issues": issues,
        "count": len(issues),
    })


async def dismiss_issue(request: web.Request) -> web.Response:
    """Dismiss an active bridge issue (admin token required)."""
    if not _validate_admin_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)

    issue_id = request.match_info["issue_id"]
    issue_manager = request.app.get("issue_manager")

    if not issue_manager:
        return web.json_response({"error": "Issue manager not available"}, status=503)

    success = issue_manager.dismiss_issue(issue_id)
    if not success:
        return web.json_response({"error": "Issue not found"}, status=404)

    return web.json_response({"success": True})


async def get_config(request: web.Request) -> web.Response:
    """Get configuration for mobile app (device API token required)."""
    device_id = _validate_api_token(request)
    if not device_id:
        return web.json_response({"error": "Unauthorized"}, status=401)

    config = request.app["config"]
    push_service = request.app["push_service"]

    return web.json_response({
        "frigate_url": config.frigate_url,
        "push_provider": config.push_provider,
        "fcm_sender_id": push_service.get_sender_id(),
        "version": "0.1.0",
    })


async def test_notification(request: web.Request) -> web.Response:
    """Send test notification."""
    device_id = _validate_api_token(request)
    if not device_id:
        return web.json_response({"error": "Unauthorized"}, status=401)

    device_store = request.app["device_store"]
    push_service = request.app["push_service"]

    device = await device_store.get_device(device_id)
    if not device or not device.get("fcm_token"):
        return web.json_response({"error": "No push token configured"}, status=400)

    notification = {
        "title": "Test Notification",
        "body": "This is a test from Frigate Notify Bridge",
        "data": {"type": "test"},
        "priority": "normal",
    }

    result = await push_service.send(device["fcm_token"], notification)

    return web.json_response({
        "success": result.get("success", False),
        "message_id": result.get("message_id"),
        "error": result.get("error"),
    })
