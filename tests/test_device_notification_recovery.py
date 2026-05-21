import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components"),
)

homeassistant = types.ModuleType("homeassistant")
homeassistant.core = types.ModuleType("homeassistant.core")
homeassistant.core.HomeAssistant = object
homeassistant.core.callback = lambda func: func
homeassistant.helpers = types.ModuleType("homeassistant.helpers")
homeassistant.helpers.device_registry = types.ModuleType(
    "homeassistant.helpers.device_registry"
)
homeassistant.helpers.device_registry.async_get = lambda hass: None
homeassistant.helpers.dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
homeassistant.helpers.dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
homeassistant.helpers.issue_registry = types.ModuleType(
    "homeassistant.helpers.issue_registry"
)


class IssueSeverity:
    ERROR = types.SimpleNamespace(value="error")


homeassistant.helpers.issue_registry.IssueSeverity = IssueSeverity
homeassistant.helpers.issue_registry.async_create_issue = lambda *args, **kwargs: None
homeassistant.helpers.issue_registry.async_delete_issue = lambda *args, **kwargs: None
sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.core", homeassistant.core)
sys.modules.setdefault("homeassistant.helpers", homeassistant.helpers)
sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    homeassistant.helpers.device_registry,
)
sys.modules.setdefault(
    "homeassistant.helpers.dispatcher",
    homeassistant.helpers.dispatcher,
)
sys.modules.setdefault(
    "homeassistant.helpers.issue_registry",
    homeassistant.helpers.issue_registry,
)
package = types.ModuleType("frigate_notify_bridge")
package.__path__ = [
    str(Path(__file__).resolve().parents[1] / "custom_components" / "frigate_notify_bridge")
]
sys.modules.setdefault("frigate_notify_bridge", package)

from frigate_notify_bridge.device_manager import DeviceManager
from frigate_notify_bridge.issues import (
    BridgeIssueManager,
    ISSUE_DEVICE_NOTIFICATION_UNREACHABLE,
)


class FakeStore:
    def __init__(self):
        self.saved = []

    async def async_save(self, data):
        self.saved.append(data)


class DeviceNotificationRecoveryTest(unittest.TestCase):
    def test_failed_device_is_suspended_until_fresh_token_update(self):
        manager = DeviceManager(
            hass=None,
            store=FakeStore(),
            initial_devices={
                "phone-1": {
                    "id": "phone-1",
                    "name": "Pixel",
                    "platform": "android",
                    "fcm_token": "old-token",
                    "subscription_active": True,
                    "notification_settings": {"enabled": True, "event_kinds": ["alert"]},
                    "failure_count_date": "2026-05-21",
                }
            },
        )

        asyncio.run(
            manager.async_record_delivery_result(
                "phone-1",
                False,
                "UNREGISTERED: Requested entity was not found",
            )
        )
        targets = asyncio.run(
            manager.async_get_devices_for_notification(kind="alert", camera="front")
        )

        self.assertEqual(targets, [])
        suspended = asyncio.run(manager.async_get_device("phone-1"))
        self.assertTrue(suspended["notification_delivery_suspended"])
        self.assertEqual(
            suspended["notification_suspended_reason"],
            "UNREGISTERED: Requested entity was not found",
        )

        asyncio.run(manager.async_update_fcm_token("phone-1", "fresh-token"))
        targets = asyncio.run(
            manager.async_get_devices_for_notification(kind="alert", camera="front")
        )

        self.assertEqual([device["id"] for device in targets], ["phone-1"])
        recovered = asyncio.run(manager.async_get_device("phone-1"))
        self.assertFalse(recovered["notification_delivery_suspended"])
        self.assertEqual(recovered["fcm_token"], "fresh-token")
        self.assertIsNotNone(recovered["notification_token_confirmed_at"])

    def test_provider_auth_failures_do_not_suspend_device(self):
        manager = DeviceManager(
            hass=None,
            store=FakeStore(),
            initial_devices={
                "phone-1": {
                    "id": "phone-1",
                    "name": "Pixel",
                    "platform": "android",
                    "fcm_token": "token",
                    "subscription_active": True,
                    "notification_settings": {"enabled": True, "event_kinds": ["alert"]},
                    "failure_count_date": "2026-05-21",
                }
            },
        )

        asyncio.run(
            manager.async_record_delivery_result(
                "phone-1",
                False,
                "Authentication failed, token invalidated",
            )
        )

        device = asyncio.run(manager.async_get_device("phone-1"))
        self.assertFalse(device.get("notification_delivery_suspended", False))

    def test_unreachable_device_issue_is_persistent(self):
        manager = BridgeIssueManager(hass=object())
        created = {}

        def fake_create_issue(*args, **kwargs):
            created["args"] = args
            created["kwargs"] = kwargs

        with patch("frigate_notify_bridge.issues.ir.async_create_issue", fake_create_issue):
            asyncio.run(
                manager.async_report_device_notification_unreachable(
                    failed_devices=["Pixel"],
                    reason="UNREGISTERED: Requested entity was not found",
                )
            )

        self.assertEqual(created["args"][2], ISSUE_DEVICE_NOTIFICATION_UNREACHABLE)
        self.assertTrue(created["kwargs"]["is_persistent"])
        self.assertEqual(
            manager.active_issues[0]["type"],
            "device_notification_unreachable",
        )


if __name__ == "__main__":
    unittest.main()
