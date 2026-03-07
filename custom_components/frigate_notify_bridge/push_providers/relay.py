"""Push notification provider that sends through the Frigate Push Relay."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import NotificationPayload, PushProvider, SendResult

_LOGGER = logging.getLogger(__name__)


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
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{self._relay_url}/health", timeout=10
            ) as resp:
                if resp.status == 200:
                    self._initialized = True
                    _LOGGER.info("Push relay reachable at %s", self._relay_url)
                    return True
                _LOGGER.error("Push relay health check failed: %d", resp.status)
        except Exception as e:
            _LOGGER.error("Push relay unreachable: %s", e)
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

        if payload.event_id:
            data["event_id"] = payload.event_id
        if payload.camera:
            data["camera"] = payload.camera
        if payload.label:
            data["label"] = payload.label
        if payload.zones:
            data["zones"] = ",".join(payload.zones)
        if payload.image_url:
            data["image_url"] = payload.image_url
        if payload.thumbnail_url:
            data["thumbnail_url"] = payload.thumbnail_url

        for key, value in (payload.data or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                data[key] = json.dumps(value, separators=(",", ":"))
            else:
                data[key] = str(value)

        return data

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
        """
        encrypted = self._encrypt_payload(payload)

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
        body_json = json.dumps(body, separators=(",", ":"))
        path = "/sendNotification"
        headers = self._build_signed_headers(path, body_json)

        try:
            _LOGGER.debug(
                "Relay send request: bridge_id=%s devices=%s payload_bytes=%d",
                self._bridge_id,
                device_tokens,
                len(encrypted),
            )
            session = async_get_clientsession(self.hass)
            async with session.post(
                f"{self._relay_url}{path}",
                data=body_json,
                headers=headers,
                timeout=15,
            ) as resp:
                data = await resp.json()
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
                    error_by_device: dict[str, str] = {}
                    for raw_error in data.get("errors", []):
                        if isinstance(raw_error, str) and ":" in raw_error:
                            device_id, error = raw_error.split(":", 1)
                            error_by_device[device_id.strip()] = error.strip()
                    results = []
                    for token in device_tokens:
                        results.append(SendResult(
                            success=token not in error_by_device,
                            device_id=token,
                            error=error_by_device.get(token),
                        ))
                    return results
                else:
                    error = data.get("error") or data.get("detail") or f"HTTP {resp.status}"
                    _LOGGER.error("Relay push failed: %s", error)
                    return [
                        SendResult(success=False, device_id=t, error=error)
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
