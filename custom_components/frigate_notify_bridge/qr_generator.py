"""QR code generation for device pairing."""
from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import get_url

from .const import QR_CODE_VERSION

_LOGGER = logging.getLogger(__name__)


def _get_cloud_url(hass: HomeAssistant) -> str | None:
    """Get the Nabu Casa cloud URL if available."""
    try:
        # Check if cloud component is loaded
        if "cloud" not in hass.config.components:
            return None

        cloud = hass.data.get("cloud")
        if cloud is None:
            return None

        # Check if cloud is connected
        if not cloud.is_logged_in or not cloud.is_connected:
            return None

        # Get the remote UI URL
        # This is the Nabu Casa remote access URL
        if hasattr(cloud, "remote") and cloud.remote:
            remote = cloud.remote
            if hasattr(remote, "is_connected") and remote.is_connected:
                # The cloud remote URL format
                instance_id = cloud.client.prefs.instance_id
                if instance_id:
                    # Sanitize: remove dashes, spaces, and whitespace
                    clean_id = str(instance_id).replace("-", "").replace(" ", "").strip()
                    return f"https://{clean_id}.ui.nabu.casa"

        return None
    except Exception as e:
        _LOGGER.debug("Could not get cloud URL: %s", e)
        return None


def _get_cloud_webrtc_config(hass: HomeAssistant) -> dict[str, Any] | None:
    """Get Nabu Casa WebRTC relay configuration if available."""
    try:
        if "cloud" not in hass.config.components:
            return None

        cloud = hass.data.get("cloud")
        if cloud is None or not cloud.is_logged_in:
            return None

        # Check for WebRTC component/relay support
        # Nabu Casa provides TURN/STUN servers for WebRTC
        if hasattr(cloud, "client") and hasattr(cloud.client, "webrtc"):
            webrtc = cloud.client.webrtc
            if webrtc and hasattr(webrtc, "turn_servers"):
                return {
                    "enabled": True,
                    "provider": "nabu_casa",
                    # Don't include actual credentials in QR - app will request them
                    "relay_available": True,
                }

        return None
    except Exception as e:
        _LOGGER.debug("Could not get WebRTC config: %s", e)
        return None


def generate_pairing_qr_data(
    hass: HomeAssistant,
    pairing_info: dict[str, Any],
    frigate_url: str | None = None,
    frigate_auth_required: bool = False,
    push_provider: str = "fcm",
    fcm_sender_id: str | None = None,
    custom_external_url: str | None = None,
    use_cloud_remote: bool = True,
    relay_url: str | None = None,
    e2e_key: str | None = None,
) -> dict[str, Any]:
    """Generate the data payload for a pairing QR code.

    Args:
        hass: Home Assistant instance
        pairing_info: Pairing token info from DeviceManager
        frigate_url: Frigate server URL
        frigate_auth_required: Whether Frigate requires authentication
        push_provider: Push notification provider
        fcm_sender_id: FCM sender ID (project number)
        custom_external_url: User-specified external URL (overrides auto-detection)
        use_cloud_remote: Whether to use Nabu Casa cloud for remote access

    Returns:
        Dict containing QR code data and metadata
    """
    # Get HA URLs
    try:
        internal_url = get_url(hass, prefer_external=False, allow_cloud=False)
    except Exception:
        internal_url = None

    # Determine external URL with priority:
    # 1. Custom user-specified URL
    # 2. Nabu Casa cloud URL (if enabled and available)
    # 3. Auto-detected external URL
    external_url = None
    cloud_url = None
    webrtc_config = None

    if custom_external_url:
        external_url = custom_external_url
    elif use_cloud_remote:
        cloud_url = _get_cloud_url(hass)
        if cloud_url:
            external_url = cloud_url
            webrtc_config = _get_cloud_webrtc_config(hass)

    if not external_url:
        try:
            external_url = get_url(hass, prefer_external=True, allow_cloud=True)
        except Exception:
            external_url = None

    # Build QR payload
    qr_payload = {
        "v": QR_CODE_VERSION,
        "t": pairing_info["token"],
        "c": pairing_info["code"],
        "e": pairing_info["expires_in"],
        "s": {  # Server info
            "i": internal_url,  # Internal URL
            "x": external_url,  # External URL
            "p": "/api/frigate_notify_bridge",  # API path
        },
        "f": {  # Frigate info
            "u": frigate_url,
            "a": frigate_auth_required,
        },
        "n": {  # Notification config
            "p": push_provider,
        },
    }

    # Add cloud-specific info if using Nabu Casa
    if cloud_url:
        qr_payload["s"]["cloud"] = {
            "enabled": True,
            "url": cloud_url,
        }
        # Add WebRTC relay info if available
        if webrtc_config:
            qr_payload["s"]["cloud"]["webrtc"] = webrtc_config

    # Add FCM sender ID if using FCM
    if push_provider == "fcm" and fcm_sender_id:
        qr_payload["n"]["s"] = fcm_sender_id

    # Add relay URL and E2E key (v3)
    if relay_url:
        qr_payload["n"]["r"] = relay_url
    if e2e_key:
        qr_payload["n"]["k"] = e2e_key

    # Create the QR code URL scheme
    # Using a custom URL scheme that the mobile app will handle
    payload_json = json.dumps(qr_payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()

    qr_url = f"frigate-mobile://pair?d={payload_b64}"

    return {
        "url": qr_url,
        "payload": qr_payload,
        "code": pairing_info["code"],
        "expires_at": pairing_info["expires_at"],
        "expires_in": pairing_info["expires_in"],
        "using_cloud": cloud_url is not None,
        "cloud_url": cloud_url,
        "webrtc_available": webrtc_config is not None,
    }


async def generate_qr_code_image(
    qr_data: dict[str, Any],
    size: int = 300,
    format: str = "png",
) -> bytes:
    """Generate a QR code image.

    Args:
        qr_data: QR data from generate_pairing_qr_data
        size: Image size in pixels
        format: Image format (png, svg)

    Returns:
        Image bytes
    """
    try:
        import qrcode
        from qrcode.image.styledpil import StyledPilImage
        from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
    except ImportError:
        _LOGGER.error("qrcode library not installed")
        raise

    qr = qrcode.QRCode(
        version=None,  # Auto-determine
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )

    qr.add_data(qr_data["url"])
    qr.make(fit=True)

    # Create styled image
    img = qr.make_image(
        image_factory=StyledPilImage,
        module_drawer=RoundedModuleDrawer(),
        fill_color="#1A73E8",  # Frigate Mobile primary color
        back_color="white",
    )

    # Resize to requested size
    img = img.resize((size, size))

    # Convert to bytes
    buffer = io.BytesIO()
    img.save(buffer, format=format.upper())
    buffer.seek(0)

    return buffer.getvalue()


async def generate_qr_code_base64(
    qr_data: dict[str, Any],
    size: int = 300,
) -> str:
    """Generate a QR code as base64-encoded PNG.

    Args:
        qr_data: QR data from generate_pairing_qr_data
        size: Image size in pixels

    Returns:
        Base64-encoded PNG string
    """
    image_bytes = await generate_qr_code_image(qr_data, size, "png")
    return base64.b64encode(image_bytes).decode()


def generate_simple_qr_svg(data: str, size: int = 300) -> str:
    """Generate a simple SVG QR code without external dependencies.

    This is a fallback if qrcode library is not available.
    Uses a minimal QR code implementation.
    """
    # This would need a pure-Python QR implementation
    # For now, return a placeholder that indicates setup is needed
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">
        <rect width="{size}" height="{size}" fill="white"/>
        <text x="50%" y="50%" text-anchor="middle" fill="#666" font-size="14">
            QR Code Generation
        </text>
        <text x="50%" y="60%" text-anchor="middle" fill="#666" font-size="12">
            Install qrcode package
        </text>
    </svg>"""
