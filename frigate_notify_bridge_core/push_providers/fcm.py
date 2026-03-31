"""Firebase Cloud Messaging push provider using HTTP v1 API.

Uses PyJWT + cryptography instead of firebase-admin.
No Home Assistant dependency — uses an injected aiohttp.ClientSession.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import jwt

from .base import NotificationPayload, PushProvider, SendResult

if TYPE_CHECKING:
    import aiohttp

_LOGGER = logging.getLogger(__name__)

_FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_TOKEN_CACHE_BUFFER_SECONDS = 300


class FCMProvider(PushProvider):
    """Firebase Cloud Messaging provider using HTTP v1 API with JWT auth."""

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        credentials_json: str,
    ) -> None:
        super().__init__(session)
        self._credentials_json = credentials_json
        self._credentials: dict[str, Any] | None = None
        self._project_id: str | None = None
        self._sender_id: str | None = None
        self._access_token: str | None = None
        self._token_expiry: float = 0
        self._token_lock = asyncio.Lock()

    async def async_initialize(self) -> bool:
        try:
            self._clear_error()
            self._credentials = json.loads(self._credentials_json)
            self._project_id = self._credentials.get("project_id")

            if not self._project_id:
                self._set_error("Firebase credentials missing project_id")
                _LOGGER.error(self.last_error)
                return False

            if not self._credentials.get("private_key"):
                self._set_error("Firebase credentials missing private_key")
                _LOGGER.error(self.last_error)
                return False

            if not self._credentials.get("client_email"):
                self._set_error("Firebase credentials missing client_email")
                _LOGGER.error(self.last_error)
                return False

            self._initialized = True
            self._clear_error()
            _LOGGER.info("FCM provider initialized for project: %s", self._project_id)
            return True

        except json.JSONDecodeError as e:
            self._set_error(f"Failed to parse FCM credentials JSON: {e}")
            _LOGGER.error(self.last_error)
            return False
        except Exception as e:
            self._set_error(f"Failed to initialize FCM provider: {e}")
            _LOGGER.exception("Failed to initialize FCM provider: %s", e)
            return False

    async def _async_get_access_token(self) -> str:
        async with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expiry:
                return self._access_token

            _LOGGER.debug("Refreshing FCM access token")

            iat = int(now)
            exp = iat + 3600
            claims = {
                "iss": self._credentials["client_email"],
                "sub": self._credentials["client_email"],
                "aud": _GOOGLE_TOKEN_URL,
                "iat": iat,
                "exp": exp,
                "scope": _FCM_SCOPE,
            }

            signed_jwt = jwt.encode(
                claims,
                self._credentials["private_key"],
                algorithm="RS256",
            )

            async with self._session.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": signed_jwt,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Token exchange failed ({resp.status}): {body}")
                token_data = await resp.json()

            self._access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            self._token_expiry = now + expires_in - _FCM_TOKEN_CACHE_BUFFER_SECONDS

            _LOGGER.debug("FCM access token refreshed, expires in %ds", expires_in)
            return self._access_token

    async def async_send(self, device_token: str, payload: NotificationPayload) -> SendResult:
        if not self._initialized:
            return SendResult(
                success=False,
                device_id=device_token,
                error="FCM provider not initialized",
            )

        try:
            access_token = await self._async_get_access_token()

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

            url = _FCM_SEND_URL.format(project_id=self._project_id)
            async with self._session.post(
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
                    return SendResult(success=True, device_id=device_token, message_id=message_name)

                error_details = body.get("error", {})
                error_status = error_details.get("status", "")
                error_msg = error_details.get("message", str(body))

                if error_status == "NOT_FOUND":
                    error_msg = "Device token is no longer valid"
                elif error_status == "INVALID_ARGUMENT":
                    error_msg = "Invalid device token"
                elif resp.status == 401:
                    self._access_token = None
                    self._token_expiry = 0
                    error_msg = "Authentication failed, token invalidated"

                _LOGGER.error("FCM send failed (%s): %s", resp.status, error_msg)
                return SendResult(success=False, device_id=device_token, error=error_msg)

        except Exception as e:
            _LOGGER.error("FCM send failed: %s", e)
            return SendResult(success=False, device_id=device_token, error=str(e))

    async def async_send_to_many(
        self,
        device_tokens: list[str],
        payload: NotificationPayload,
    ) -> list[SendResult]:
        if not self._initialized:
            return [
                SendResult(success=False, device_id=token, error="FCM provider not initialized")
                for token in device_tokens
            ]
        if not device_tokens:
            return []
        return await super().async_send_to_many(device_tokens, payload)

    async def async_close(self) -> None:
        self._access_token = None
        self._token_expiry = 0
        self._initialized = False

    def get_sender_id(self) -> str | None:
        return self._sender_id or self._project_id
