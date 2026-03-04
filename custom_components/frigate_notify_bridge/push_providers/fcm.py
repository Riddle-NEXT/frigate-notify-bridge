"""Firebase Cloud Messaging push provider using HTTP v1 API.

Uses PyJWT + cryptography (both in HA core) instead of firebase-admin,
avoiding grpcio compilation issues on ARM/Pi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import jwt
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import PushProvider, NotificationPayload, SendResult
from ..const import (
    FCM_SEND_URL,
    GOOGLE_TOKEN_URL,
    FCM_SCOPE,
    FCM_TOKEN_CACHE_BUFFER_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class FCMProvider(PushProvider):
    """Firebase Cloud Messaging provider using HTTP v1 API with JWT auth."""

    def __init__(
        self,
        hass: HomeAssistant,
        credentials_json: str,
    ) -> None:
        """Initialize FCM provider."""
        super().__init__(hass)
        self._credentials_json = credentials_json
        self._credentials: dict[str, Any] | None = None
        self._project_id: str | None = None
        self._sender_id: str | None = None
        self._access_token: str | None = None
        self._token_expiry: float = 0
        self._token_lock = asyncio.Lock()

    async def async_initialize(self) -> bool:
        """Initialize provider by parsing credentials."""
        try:
            self._credentials = json.loads(self._credentials_json)
            self._project_id = self._credentials.get("project_id")

            if not self._project_id:
                _LOGGER.error("Firebase credentials missing project_id")
                return False

            private_key = self._credentials.get("private_key")
            if not private_key:
                _LOGGER.error("Firebase credentials missing private_key")
                return False

            client_email = self._credentials.get("client_email")
            if not client_email:
                _LOGGER.error("Firebase credentials missing client_email")
                return False

            self._initialized = True
            _LOGGER.info("FCM provider initialized for project: %s", self._project_id)
            return True

        except json.JSONDecodeError as e:
            _LOGGER.error("Failed to parse FCM credentials JSON: %s", e)
            return False
        except Exception as e:
            _LOGGER.exception("Failed to initialize FCM provider: %s", e)
            return False

    async def _async_get_access_token(self) -> str:
        """Get a valid access token, refreshing if needed.

        Uses RS256 JWT signed with the service account private key,
        exchanged at Google's token endpoint for an access token.
        Token is cached and refreshed 5 minutes before expiry.
        """
        async with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expiry:
                return self._access_token

            _LOGGER.debug("Refreshing FCM access token")

            # Build JWT claims
            iat = int(now)
            exp = iat + 3600  # 1 hour
            claims = {
                "iss": self._credentials["client_email"],
                "sub": self._credentials["client_email"],
                "aud": GOOGLE_TOKEN_URL,
                "iat": iat,
                "exp": exp,
                "scope": FCM_SCOPE,
            }

            # Sign JWT with RS256 using the service account private key
            signed_jwt = jwt.encode(
                claims,
                self._credentials["private_key"],
                algorithm="RS256",
            )

            # Exchange JWT for access token
            session = async_get_clientsession(self.hass)
            async with session.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": signed_jwt,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Token exchange failed ({resp.status}): {body}"
                    )
                token_data = await resp.json()

            self._access_token = token_data["access_token"]
            # Cache with buffer so we refresh before actual expiry
            expires_in = token_data.get("expires_in", 3600)
            self._token_expiry = now + expires_in - FCM_TOKEN_CACHE_BUFFER_SECONDS

            _LOGGER.debug("FCM access token refreshed, expires in %ds", expires_in)
            return self._access_token

    async def async_send(
        self,
        device_token: str,
        payload: NotificationPayload,
    ) -> SendResult:
        """Send notification via FCM HTTP v1 API."""
        if not self._initialized:
            return SendResult(
                success=False,
                device_id=device_token,
                error="FCM provider not initialized",
            )

        try:
            access_token = await self._async_get_access_token()

            # Build data payload
            data: dict[str, str] = {
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
                "event_id": payload.event_id or "",
                "camera": payload.camera or "",
                "label": payload.label or "",
            }

            if payload.zones:
                data["zones"] = ",".join(payload.zones)
            if payload.thumbnail_url:
                data["thumbnail_url"] = payload.thumbnail_url
            if payload.data:
                for key, value in payload.data.items():
                    if isinstance(value, (dict, list)):
                        data[key] = json.dumps(value)
                    else:
                        data[key] = str(value)

            # Build FCM v1 message
            message: dict[str, Any] = {
                "message": {
                    "token": device_token,
                    "notification": {
                        "title": payload.title,
                        "body": payload.body,
                    },
                    "data": data,
                    "android": {
                        "priority": "HIGH" if payload.priority == "high" else "NORMAL",
                        "notification": {
                            "channel_id": "frigate_alerts",
                            "default_sound": True,
                            "default_vibrate_timings": True,
                        },
                    },
                    "apns": {
                        "payload": {
                            "aps": {
                                "alert": {
                                    "title": payload.title,
                                    "body": payload.body,
                                },
                                "sound": "default" if payload.sound is None else payload.sound,
                                "mutable-content": 1,
                            },
                        },
                    },
                }
            }

            if payload.image_url:
                message["message"]["notification"]["image"] = payload.image_url

            if payload.badge is not None:
                message["message"]["apns"]["payload"]["aps"]["badge"] = payload.badge

            # Send via HTTP v1 API
            url = FCM_SEND_URL.format(project_id=self._project_id)
            session = async_get_clientsession(self.hass)

            async with session.post(
                url,
                json=message,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                body = await resp.json()

                if resp.status == 200:
                    message_name = body.get("name", "")
                    _LOGGER.debug("FCM message sent: %s", message_name)
                    return SendResult(
                        success=True,
                        device_id=device_token,
                        message_id=message_name,
                    )

                # Handle FCM error responses
                error_details = body.get("error", {})
                error_status = error_details.get("status", "")
                error_msg = error_details.get("message", str(body))

                if error_status == "NOT_FOUND":
                    error_msg = "Device token is no longer valid"
                elif error_status == "INVALID_ARGUMENT":
                    error_msg = "Invalid device token"
                elif resp.status == 401:
                    # Token expired mid-request, invalidate cache
                    self._access_token = None
                    self._token_expiry = 0
                    error_msg = "Authentication failed, token invalidated"

                _LOGGER.error("FCM send failed (%s): %s", resp.status, error_msg)
                return SendResult(
                    success=False,
                    device_id=device_token,
                    error=error_msg,
                )

        except Exception as e:
            _LOGGER.error("FCM send failed: %s", e)
            return SendResult(
                success=False,
                device_id=device_token,
                error=str(e),
            )

    async def async_send_to_many(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        """Send notification to multiple devices sequentially.

        HTTP v1 API has no batch endpoint; send one at a time.
        """
        if not self._initialized:
            return [
                SendResult(
                    success=False,
                    device_id=token,
                    error="FCM provider not initialized",
                )
                for token in device_tokens
            ]

        if not device_tokens:
            return []

        return await super().async_send_to_many(device_tokens, payload)

    async def async_close(self) -> None:
        """Clean up — nothing to tear down for HTTP-based provider."""
        self._access_token = None
        self._token_expiry = 0
        self._initialized = False

    def get_sender_id(self) -> str | None:
        """Get the FCM sender ID (project number)."""
        return self._sender_id or self._project_id
