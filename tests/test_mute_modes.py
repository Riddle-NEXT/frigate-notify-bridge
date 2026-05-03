import unittest
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "frigate_notify_bridge"),
)

from mute_modes import (
    active_mute_mode_for_device,
    normalize_mute_mode_settings,
    should_mute_event,
)


class MuteModesTest(unittest.TestCase):
    def test_global_mute_mode_blocks_non_important_event_for_every_device(self):
        mode = {
            "active": True,
            "scope": "all",
            "expires_at": 2000,
            "important_labels": ["package"],
        }

        self.assertTrue(
            should_mute_event(
                mode,
                device_id="phone-1",
                camera="front",
                label="person",
                sub_label=None,
                now=1000,
            )
        )
        self.assertFalse(
            should_mute_event(
                mode,
                device_id="phone-2",
                camera="front",
                label="package",
                sub_label=None,
                now=1000,
            )
        )

    def test_device_mute_mode_only_applies_to_target_device(self):
        mode = {
            "active": True,
            "scope": "device",
            "device_id": "phone-1",
            "expires_at": 2000,
            "important_labels": [],
        }

        self.assertTrue(
            should_mute_event(
                mode,
                device_id="phone-1",
                camera="front",
                label="person",
                sub_label=None,
                now=1000,
            )
        )
        self.assertFalse(
            should_mute_event(
                mode,
                device_id="phone-2",
                camera="front",
                label="person",
                sub_label=None,
                now=1000,
            )
        )

    def test_normalizes_per_device_mute_mode_defaults(self):
        settings = normalize_mute_mode_settings({
            "important_labels": ["package", "person", "package"],
            "live_activity_enabled": True,
            "default_scope": "all",
        })

        self.assertEqual(settings["important_labels"], ["package", "person"])
        self.assertTrue(settings["live_activity_enabled"])
        self.assertEqual(settings["default_scope"], "all")
        self.assertEqual(settings["default_duration_minutes"], 30)

    def test_active_mute_mode_drops_expired_mode(self):
        mode = {"active": True, "scope": "all", "expires_at": 900}

        self.assertIsNone(
            active_mute_mode_for_device(mode, device_id="phone-1", now=1000)
        )


if __name__ == "__main__":
    unittest.main()
