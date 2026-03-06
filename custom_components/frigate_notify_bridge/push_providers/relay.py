"""Push notification provider that sends through the Frigate Push Relay."""
from __future__ import annotations

import base64
import json
import logging
import os
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
            # title/body intentionally omitted — prevents info leakage through relay.
            # The relay falls back to generic "Frigate Alert" / "New Frigate event".
        }

        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                f"{self._relay_url}/sendNotification",
                json=body,
                headers={"Authorization": f"Bearer {self._bridge_secret}"},
                timeout=15,
            ) as resp:
                data = await resp.json()

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
