"""Push notification service for standalone mode."""

import json
import logging
from typing import Any

import aiohttp

from .config import Config

logger = logging.getLogger(__name__)


class PushService:
    """Push notification service supporting FCM, ntfy, and Pushover."""

    def __init__(self, config: Config) -> None:
        """Initialize push service."""
        self.config = config
        self._provider = config.push_provider
        self._initialized = False
        self._fcm_app = None
        self._session: aiohttp.ClientSession | None = None

    async def initialize(self) -> bool:
        """Initialize the push service."""
        self._session = aiohttp.ClientSession()

        if self._provider == "fcm":
            return await self._init_fcm()
        elif self._provider == "ntfy":
            return await self._init_ntfy()
        elif self._provider == "pushover":
            return await self._init_pushover()
        else:
            logger.error("Unknown push provider: %s", self._provider)
            return False

    async def _init_fcm(self) -> bool:
        """Initialize Firebase Cloud Messaging."""
        try:
            import firebase_admin
            from firebase_admin import credentials

            if not self.config.fcm_credentials:
                logger.error("FCM credentials not loaded")
                return False

            cred = credentials.Certificate(self.config.fcm_credentials)

            try:
                self._fcm_app = firebase_admin.get_app("frigate_bridge")
            except ValueError:
                self._fcm_app = firebase_admin.initialize_app(
                    cred,
                    name="frigate_bridge",
                )

            self._initialized = True
            logger.info("FCM initialized successfully")
            return True

        except Exception as e:
            logger.exception("Failed to initialize FCM: %s", e)
            return False

    async def _init_ntfy(self) -> bool:
        """Initialize ntfy."""
        if not self.config.ntfy_topic:
            logger.error("ntfy topic not configured")
            return False

        # Test connection to ntfy server
        try:
            async with self._session.get(
                f"{self.config.ntfy_url}/v1/health",
                timeout=10,
            ) as response:
                if response.status == 200:
                    logger.info("ntfy initialized: %s", self.config.ntfy_url)
                else:
                    logger.warning("ntfy health check returned %d", response.status)
        except Exception as e:
            logger.warning("Could not verify ntfy server: %s", e)

        self._initialized = True
        return True

    async def _init_pushover(self) -> bool:
        """Initialize Pushover."""
        if not self.config.pushover_user_key or not self.config.pushover_api_token:
            logger.error("Pushover credentials not configured")
            return False

        # Validate credentials
        try:
            async with self._session.post(
                "https://api.pushover.net/1/users/validate.json",
                data={
                    "token": self.config.pushover_api_token,
                    "user": self.config.pushover_user_key,
                },
                timeout=10,
            ) as response:
                result = await response.json()
                if result.get("status") == 1:
                    logger.info("Pushover initialized successfully")
                    self._initialized = True
                    return True
                else:
                    logger.error("Pushover validation failed: %s", result.get("errors"))
                    return False
        except Exception as e:
            logger.exception("Failed to validate Pushover: %s", e)
            return False

    async def send(
        self,
        device_token: str,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a notification to a device."""
        if not self._initialized:
            return {"success": False, "error": "Service not initialized"}

        if self._provider == "fcm":
            return await self._send_fcm(device_token, notification)
        elif self._provider == "ntfy":
            return await self._send_ntfy(device_token, notification)
        elif self._provider == "pushover":
            return await self._send_pushover(device_token, notification)

        return {"success": False, "error": "Unknown provider"}

    async def send_to_many(
        self,
        device_tokens: list[str],
        notification: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Send notification to multiple devices."""
        results = []
        for token in device_tokens:
            result = await self.send(token, notification)
            results.append(result)
        return results

    async def _send_fcm(
        self,
        device_token: str,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        """Send via Firebase Cloud Messaging."""
        try:
            from firebase_admin import messaging

            # Build FCM notification
            fcm_notification = messaging.Notification(
                title=notification.get("title"),
                body=notification.get("body"),
                image=notification.get("image_url"),
            )

            # Build data payload
            data = notification.get("data", {})
            # Convert all values to strings
            data = {k: str(v) for k, v in data.items()}
            data["click_action"] = "FLUTTER_NOTIFICATION_CLICK"

            # Build message
            message = messaging.Message(
                notification=fcm_notification,
                data=data,
                token=device_token,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        channel_id="frigate_alerts",
                        priority="high",
                    ),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            sound="default",
                            mutable_content=True,
                        ),
                    ),
                ),
            )

            # Send
            response = messaging.send(message, app=self._fcm_app)

            return {
                "success": True,
                "device_id": device_token,
                "message_id": response,
            }

        except Exception as e:
            logger.error("FCM send failed: %s", e)
            return {
                "success": False,
                "device_id": device_token,
                "error": str(e),
            }

    async def _send_ntfy(
        self,
        device_token: str,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        """Send via ntfy."""
        try:
            headers = {
                "Title": notification.get("title", "Frigate Alert"),
                "Priority": "high" if notification.get("priority") == "high" else "default",
                "Tags": "camera,warning",
            }

            if self.config.ntfy_token:
                headers["Authorization"] = f"Bearer {self.config.ntfy_token}"

            if notification.get("image_url"):
                headers["Attach"] = notification["image_url"]

            # Use device_token as topic or fall back to configured topic
            topic = device_token or self.config.ntfy_topic
            url = f"{self.config.ntfy_url}/{topic}"

            async with self._session.post(
                url,
                data=notification.get("body", ""),
                headers=headers,
                timeout=30,
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return {
                        "success": True,
                        "device_id": device_token,
                        "message_id": result.get("id"),
                    }
                else:
                    error = await response.text()
                    return {
                        "success": False,
                        "device_id": device_token,
                        "error": f"HTTP {response.status}: {error}",
                    }

        except Exception as e:
            logger.error("ntfy send failed: %s", e)
            return {
                "success": False,
                "device_id": device_token,
                "error": str(e),
            }

    async def _send_pushover(
        self,
        device_token: str,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        """Send via Pushover."""
        try:
            data = {
                "token": self.config.pushover_api_token,
                "user": self.config.pushover_user_key,
                "title": notification.get("title", "Frigate Alert"),
                "message": notification.get("body", ""),
                "priority": 1 if notification.get("priority") == "high" else 0,
                "sound": "pushover",
            }

            if device_token and device_token != "all":
                data["device"] = device_token

            if notification.get("image_url"):
                data["url"] = notification["image_url"]
                data["url_title"] = "View Image"

            async with self._session.post(
                "https://api.pushover.net/1/messages.json",
                data=data,
                timeout=30,
            ) as response:
                result = await response.json()
                if result.get("status") == 1:
                    return {
                        "success": True,
                        "device_id": device_token,
                        "message_id": result.get("request"),
                    }
                else:
                    return {
                        "success": False,
                        "device_id": device_token,
                        "error": ", ".join(result.get("errors", ["Unknown error"])),
                    }

        except Exception as e:
            logger.error("Pushover send failed: %s", e)
            return {
                "success": False,
                "device_id": device_token,
                "error": str(e),
            }

    async def close(self) -> None:
        """Close the push service."""
        if self._session:
            await self._session.close()

        if self._fcm_app:
            try:
                import firebase_admin
                firebase_admin.delete_app(self._fcm_app)
            except Exception:
                pass

    def get_sender_id(self) -> str | None:
        """Get sender ID for the configured provider."""
        if self._provider == "fcm" and self.config.fcm_credentials:
            return self.config.fcm_credentials.get("project_id")
        elif self._provider == "ntfy":
            return self.config.ntfy_topic
        return None
