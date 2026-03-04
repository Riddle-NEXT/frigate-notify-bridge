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
    app.router.add_get("/api/devices", list_devices)
    app.router.add_get("/api/devices/{device_id}", get_device)
    app.router.add_patch("/api/devices/{device_id}", update_device)
    app.router.add_delete("/api/devices/{device_id}", delete_device)
    app.router.add_post("/api/devices/{device_id}/token", update_token)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/test", test_notification)


def _validate_api_token(request: web.Request) -> str | None:
    """Validate API token from request."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        device_store = request.app["device_store"]
        return device_store.validate_api_token(token)
    return None


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


async def get_status(request: web.Request) -> web.Response:
    """Get bridge status."""
    device_store = request.app["device_store"]
    push_service = request.app["push_service"]
    devices = await device_store.get_all_devices()

    return web.json_response({
        "status": "ok",
        "version": "0.1.0",
        "push_provider": push_service._provider,
        "devices_count": len(devices),
    })


async def get_pairing_qr(request: web.Request) -> web.Response:
    """Generate pairing QR code data."""
    device_store = request.app["device_store"]
    push_service = request.app["push_service"]
    config = request.app["config"]

    # Generate pairing code
    pairing_info = device_store.generate_pairing_code()

    # Build QR payload
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
        # Generate QR code image
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
    """Complete device pairing."""
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
    """List all devices (requires auth)."""
    device_store = request.app["device_store"]
    devices = await device_store.get_all_devices()

    safe_devices = {}
    for device_id, device in devices.items():
        safe_devices[device_id] = {
            "id": device["id"],
            "name": device["name"],
            "platform": device["platform"],
            "paired_at": device["paired_at"],
            "last_seen": device.get("last_seen"),
        }

    return web.json_response({
        "devices": safe_devices,
        "count": len(safe_devices),
    })


async def get_device(request: web.Request) -> web.Response:
    """Get device details."""
    device_id = request.match_info["device_id"]
    token_device_id = _validate_api_token(request)

    if token_device_id != device_id:
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
    })


async def update_device(request: web.Request) -> web.Response:
    """Update device settings."""
    device_id = request.match_info["device_id"]
    token_device_id = _validate_api_token(request)

    if token_device_id != device_id:
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
    token_device_id = _validate_api_token(request)

    if token_device_id != device_id:
        return web.json_response({"error": "Unauthorized"}, status=401)

    device_store = request.app["device_store"]
    success = await device_store.remove_device(device_id)

    if not success:
        return web.json_response({"error": "Device not found"}, status=404)

    return web.json_response({"success": True})


async def update_token(request: web.Request) -> web.Response:
    """Update device FCM token."""
    device_id = request.match_info["device_id"]
    token_device_id = _validate_api_token(request)

    if token_device_id != device_id:
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


async def get_config(request: web.Request) -> web.Response:
    """Get configuration for mobile app."""
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
        return web.json_response({"error": "No FCM token configured"}, status=400)

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
