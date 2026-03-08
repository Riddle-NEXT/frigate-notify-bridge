"""Push notification provider that sends through the Frigate Push Relay."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import NotificationPayload, PushProvider, SendResult

_LOGGER = logging.getLogger(__name__)

_MAX_FCM_DATA_BYTES = 4096
_MAX_ENCRYPTED_PAYLOAD_BYTES = 4096
_MAX_TITLE_LENGTH = 120
_MAX_BODY_LENGTH = 500
_MAX_IMAGE_URL_LENGTH = 2048
_MAX_THREAD_ID_LENGTH = 64
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")
_NOTIFICATION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_RESERVED_NOTIFICATION_PREFIXES = ("google.", "gcm.")
_RESERVED_NOTIFICATION_KEYS = {"from"}
_MAX_REDUCTION_LEVELS = 3  # Number of payload reduction attempts


class RelayPushProvider(PushProvider):
    """Send notifications through the central push relay with E2E encryption."""

    def __init__(
        self,
        hass: HomeAssistant,
        relay_url: str,
        bridge_id: str,
        bridge_secret: str,
        e2e_key: bytes,
    ) -> None:
        super().__init__(hass)
        self._relay_url = relay_url.rstrip("/")
        self._bridge_id = bridge_id
        self._bridge_secret = bridge_secret
        self._e2e_key = e2e_key

    @property
    def name(self) -> str:
        return "RelayPushProvider"

    async def async_initialize(self) -> bool:
        """Verify relay is reachable."""
        try:
            self._clear_error()
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{self._relay_url}/health", timeout=10
            ) as resp:
                if resp.status == 200:
                    self._initialized = True
                    self._clear_error()
                    _LOGGER.info("Push relay reachable at %s", self._relay_url)
                    return True
                self._set_error(f"Push relay health check failed: HTTP {resp.status}")
                _LOGGER.error(self.last_error)
        except Exception as e:
            self._set_error(f"Push relay unreachable: {e}")
            _LOGGER.error(self.last_error)
        return False

    async def async_register_device(
        self,
        fcm_token: str,
        platform: str = "unknown",
        device_id: str | None = None,
    ) -> str | None:
        """Register device with relay.

        Note: Device registration requires Firebase Auth + App Check which must
        be performed by the mobile app, not the bridge. The app calls
        /registerToken directly after getting its FCM token. This method is a
        no-op stub kept for interface compatibility.
        """
        _LOGGER.debug(
            "async_register_device called on RelayPushProvider — "
            "relay registration is handled by the app directly"
        )
        return device_id

    def _encrypt_payload(self, payload: NotificationPayload) -> str:
        """Encrypt notification payload with AES-256-GCM.

        Returns base64-encoded nonce + ciphertext.
        """
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = json.dumps(payload.to_dict()).encode()
        nonce = os.urandom(12)
        aesgcm = AESGCM(self._e2e_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        return base64.b64encode(nonce + ciphertext).decode()

    def _build_signed_headers(self, path: str, body_json: str) -> dict[str, str]:
        """Build HMAC-signed headers for relay requests."""
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha256(body_json.encode()).hexdigest()
        canonical = "\n".join(["POST", path, timestamp, body_hash])
        signature = hmac.new(
            self._bridge_secret.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": f"Bearer {self._bridge_secret}",
            "Content-Type": "application/json",
            "X-Frigate-Timestamp": timestamp,
            "X-Frigate-Signature": signature,
        }

    def _build_notification_data(
        self,
        payload: NotificationPayload,
    ) -> dict[str, str]:
        """Build compact plaintext routing metadata for OS-visible notifications."""
        data: dict[str, str] = {
            "type": str((payload.data or {}).get("type", "frigate_event")),
        }

        for key in (
            "event_id",
            "review_id",
            "camera",
            "label",
            "event_kind",
            "sub_label",
            "start_time",
        ):
            value = (payload.data or {}).get(key)
            if value is not None and str(value):
                data[key] = str(value)

        if payload.event_id and "event_id" not in data:
            data["event_id"] = payload.event_id
        if payload.camera and "camera" not in data:
            data["camera"] = payload.camera
        if payload.label and "label" not in data:
            data["label"] = payload.label

        return data

    def _reduce_payload(
        self,
        payload: NotificationPayload,
        level: int,
    ) -> NotificationPayload:
        """Progressively reduce payload size for retry after size errors.

        Level 0: No reduction (original payload)
        Level 1: Remove image_url (keep thumbnail if set separately)
        Level 2: Remove all image URLs
        Level 3: Remove optional data fields and truncate zones/objects
        """
        if level <= 0:
            return payload

        # Create a copy of the payload to avoid modifying the original
        reduced = NotificationPayload(
            title=payload.title,
            body=payload.body,
            data=copy.deepcopy(payload.data) if payload.data else None,
            image_url=payload.image_url,
            thumbnail_url=payload.thumbnail_url,
            priority=payload.priority,
            sound=payload.sound,
            badge=payload.badge,
            event_id=payload.event_id,
            camera=payload.camera,
            label=payload.label,
            zones=list(payload.zones) if payload.zones else None,
        )

        if level >= 1:
            # Remove the main image URL (saves ~200-350 bytes)
            reduced.image_url = None
            _LOGGER.debug("Payload reduction level 1: removed image_url")

        if level >= 2:
            # Remove thumbnail URL too (saves another ~200-350 bytes)
            reduced.thumbnail_url = None
            _LOGGER.debug("Payload reduction level 2: removed thumbnail_url")

        if level >= 3:
            # Truncate data fields and limit zones/objects
            if reduced.data:
                # Keep only essential fields
                essential_keys = {"type", "ts", "clip", "snap"}
                reduced.data = {
                    k: v for k, v in reduced.data.items()
                    if k in essential_keys
                }
            # Limit zones to 1
            if reduced.zones and len(reduced.zones) > 1:
                reduced.zones = reduced.zones[:1]
            _LOGGER.debug("Payload reduction level 3: truncated data fields")

        return reduced

    def _is_payload_too_big_error(self, error: str | None) -> bool:
        """Check if an error indicates the payload was too large."""
        if not error:
            return False
        error_lower = error.lower()
        return any(phrase in error_lower for phrase in (
            "4kb",
            "exceeds",
            "too big",
            "too large",
            "payload size",
            "message is too big",
        ))

    def _estimate_fcm_data_bytes(
        self,
        encrypted_payload: str,
        notification_data: dict[str, str],
        device_id: str,
    ) -> int:
        """Estimate the per-device FCM data payload size."""
        message_data = {
            "encrypted": encrypted_payload,
            "bridgeId": self._bridge_id,
            "deviceId": device_id,
            "click_action": "FLUTTER_NOTIFICATION_CLICK",
            **notification_data,
        }
        return len(
            json.dumps(
                message_data,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )

    def _validate_relay_body(
        self,
        body: dict[str, Any],
        device_tokens: list[str],
    ) -> None:
        """Validate relay request body before sending it upstream."""
        encrypted_payload = str(body["encryptedPayload"])
        if len(encrypted_payload.encode("utf-8")) > _MAX_ENCRYPTED_PAYLOAD_BYTES:
            raise ValueError("encryptedPayload exceeds 4KB limit")

        title = body.get("title")
        if title and len(str(title)) > _MAX_TITLE_LENGTH:
            raise ValueError("title is too long")

        message_body = body.get("body")
        if message_body and len(str(message_body)) > _MAX_BODY_LENGTH:
            raise ValueError("body is too long")

        image_url = body.get("imageUrl")
        if image_url and len(str(image_url)) > _MAX_IMAGE_URL_LENGTH:
            raise ValueError("imageUrl is too long")

        category = body.get("category")
        if category and not _NOTIFICATION_KEY_PATTERN.fullmatch(str(category)):
            raise ValueError("category is invalid")

        thread_id = body.get("threadId")
        if thread_id and len(str(thread_id)) > _MAX_THREAD_ID_LENGTH:
            raise ValueError("threadId is too long")

        notification_data = body.get("notificationData") or {}
        if not isinstance(notification_data, dict):
            raise ValueError("notificationData must be an object")

        for key, value in notification_data.items():
            normalized_key = str(key).strip()
            if not _NOTIFICATION_KEY_PATTERN.fullmatch(normalized_key):
                raise ValueError("notificationData contains an invalid key")
            if normalized_key in _RESERVED_NOTIFICATION_KEYS or normalized_key.startswith(
                _RESERVED_NOTIFICATION_PREFIXES
            ):
                raise ValueError("notificationData contains a reserved key")

            normalized_value = str(value or "")
            if len(normalized_value) > 1024:
                raise ValueError("notificationData contains a value that is too long")

        if not device_tokens:
            raise ValueError("At least one target deviceId is required")

        for device_id in device_tokens:
            if not _DEVICE_ID_PATTERN.fullmatch(device_id):
                raise ValueError("deviceId is invalid")

        max_data_bytes = max(
            self._estimate_fcm_data_bytes(encrypted_payload, notification_data, device_id)
            for device_id in device_tokens
        )
        if max_data_bytes > _MAX_FCM_DATA_BYTES:
            raise ValueError(
                f"FCM data payload exceeds 4KB limit ({max_data_bytes} bytes)"
            )

    async def _read_relay_response(self, resp: Any) -> dict[str, Any]:
        """Read a relay response as JSON when possible, else preserve raw text."""
        response_text = await resp.text()
        if not response_text:
            return {}

        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError:
            return {"error": response_text.strip() or f"HTTP {resp.status}"}

        return parsed if isinstance(parsed, dict) else {"response": parsed}

    def _results_from_relay_response(
        self,
        device_tokens: list[str],
        response_data: dict[str, Any],
    ) -> list[SendResult]:
        """Map relay response fields to per-device send results."""
        error_by_device: dict[str, str] = {}

        for raw_error in response_data.get("errors", []):
            if isinstance(raw_error, str) and ":" in raw_error:
                device_id, error = raw_error.split(":", 1)
                error_by_device[device_id.strip()] = error.strip()

        for failure in response_data.get("deliveryFailures", []):
            if not isinstance(failure, dict):
                continue
            device_id = str(failure.get("deviceId", "")).strip()
            if not device_id:
                continue
            error_code = str(failure.get("errorCode") or "").strip()
            error_message = str(failure.get("errorMessage") or "").strip()
            if error_message:
                error_by_device[device_id] = (
                    f"{error_code}: {error_message}" if error_code else error_message
                )
            elif error_code:
                error_by_device[device_id] = error_code
            else:
                error_by_device.setdefault(device_id, "Delivery failed")

        return [
            SendResult(
                success=device_id not in error_by_device,
                device_id=device_id,
                error=error_by_device.get(device_id),
            )
            for device_id in device_tokens
        ]

    async def async_send(
        self,
        device_token: str,
        payload: NotificationPayload,
    ) -> SendResult:
        """Send to a single device via relay."""
        results = await self.async_send_to_many([device_token], payload)
        return results[0] if results else SendResult(
            success=False, device_id=device_token, error="No result"
        )

    async def async_send_to_many(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        """Send encrypted notification to devices via relay.

        Note: device_tokens here are relay device IDs, not FCM tokens.
        The relay maps relay_device_id → FCM token internally.

        Implements auto-retry with progressively reduced payloads when
        payload size errors occur (FCM 4KB limit).
        """
        last_error: str | None = None

        # Try with progressively reduced payloads on size errors
        for reduction_level in range(_MAX_REDUCTION_LEVELS + 1):
            current_payload = self._reduce_payload(payload, reduction_level)
            results = await self._try_send(device_tokens, current_payload)

            # Check if all failures are due to payload size
            all_size_errors = all(
                not r.success and self._is_payload_too_big_error(r.error)
                for r in results
            )

            if not all_size_errors:
                # Either success or non-size-related errors - return results
                if reduction_level > 0 and any(r.success for r in results):
                    _LOGGER.info(
                        "Payload delivered after reduction level %d",
                        reduction_level,
                    )
                return results

            # All failures were size-related - try reducing further
            last_error = results[0].error if results else "Payload too large"
            if reduction_level < _MAX_REDUCTION_LEVELS:
                _LOGGER.warning(
                    "Payload too big (level %d), reducing and retrying: %s",
                    reduction_level,
                    last_error,
                )

        # Exhausted all reduction levels
        _LOGGER.error(
            "Failed to send notification after %d reduction attempts: %s",
            _MAX_REDUCTION_LEVELS,
            last_error,
        )
        return [
            SendResult(success=False, device_id=t, error=last_error)
            for t in device_tokens
        ]

    async def _try_send(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        """Attempt to send a notification payload to devices.

        This is the core send logic extracted to support retry with reduction.
        """
        try:
            encrypted = self._encrypt_payload(payload)
        except Exception as e:
            _LOGGER.error("Failed to encrypt payload: %s", e)
            return [
                SendResult(success=False, device_id=t, error=f"Encryption failed: {e}")
                for t in device_tokens
            ]

        body = {
            "bridgeId": self._bridge_id,
            "deviceIds": device_tokens,
            "encryptedPayload": encrypted,
            "title": payload.title,
            "body": payload.body,
            "imageUrl": payload.image_url or payload.thumbnail_url,
            "category": "frigate_event" if payload.event_id else "frigate_general",
            "threadId": payload.camera or "frigate-mobile",
            "notificationData": self._build_notification_data(payload),
        }

        # Validate and catch size errors early
        try:
            self._validate_relay_body(body, device_tokens)
        except ValueError as e:
            error_msg = str(e)
            _LOGGER.warning("Payload validation failed: %s", error_msg)
            return [
                SendResult(success=False, device_id=t, error=error_msg)
                for t in device_tokens
            ]

        body_json = json.dumps(body, separators=(",", ":"))
        path = "/sendNotification"
        headers = self._build_signed_headers(path, body_json)

        try:
            _LOGGER.debug(
                "Relay send request: bridge_id=%s devices=%s payload_bytes=%d body_bytes=%d",
                self._bridge_id,
                device_tokens,
                len(encrypted),
                len(body_json),
            )
            session = async_get_clientsession(self.hass)
            async with session.post(
                f"{self._relay_url}{path}",
                data=body_json,
                headers=headers,
                timeout=15,
            ) as resp:
                data = await self._read_relay_response(resp)
                _LOGGER.debug(
                    "Relay send response: status=%d bridge_id=%s body=%s",
                    resp.status,
                    self._bridge_id,
                    data,
                )

                if resp.status == 200:
                    sent = data.get("sent", 0)
                    failed = data.get("failed", 0)
                    _LOGGER.debug("Relay push: %d sent, %d failed", sent, failed)
                    if sent + failed != len(device_tokens):
                        _LOGGER.warning(
                            "Relay response count mismatch: requested=%d sent=%s failed=%s",
                            len(device_tokens),
                            sent,
                            failed,
                        )
                    return self._results_from_relay_response(device_tokens, data)

                error = data.get("error") or data.get("detail") or f"HTTP {resp.status}"
                _LOGGER.error("Relay push failed: %s", error)
                return [
                    SendResult(success=False, device_id=t, error=str(error))
                    for t in device_tokens
                ]

        except Exception as e:
            _LOGGER.error("Relay push error: %s", e)
            return [
                SendResult(success=False, device_id=t, error=str(e))
                for t in device_tokens
            ]


    async def async_close(self) -> None:
        """No persistent connections to close."""
        pass

    def get_sender_id(self) -> str | None:
        return self._bridge_id
