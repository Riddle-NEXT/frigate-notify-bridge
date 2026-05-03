import unittest
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "frigate_notify_bridge"),
)

from smart_rules import (
    SmartRule,
    apply_feedback_to_rule,
    discover_smart_rule_candidates,
    smart_rule_runtime_match,
    smart_rules_removed_or_disabled,
    sessions_matching_rule,
)


class SmartRulesTest(unittest.TestCase):
    def test_removed_or_disabled_rules_detects_active_modes_to_end(self):
        previous = [
            {"id": "mowing", "name": "Mowing", "enabled": True},
            {"id": "dogs", "name": "Dogs", "enabled": True},
            {"id": "old", "name": "Old", "enabled": False},
        ]
        current = [
            {"id": "dogs", "name": "Dogs", "enabled": False},
            {"id": "new", "name": "New", "enabled": True},
        ]

        ended = smart_rules_removed_or_disabled(previous, current)

        self.assertEqual([rule["id"] for rule in ended], ["mowing", "dogs"])

    def test_removed_or_disabled_rules_ignores_unchanged_enabled_rules(self):
        previous = [{"id": "mowing", "name": "Mowing", "enabled": True}]
        current = [{"id": "mowing", "name": "Mowing", "enabled": True}]

        self.assertEqual(smart_rules_removed_or_disabled(previous, current), [])

    def test_discovers_mowing_candidate_from_robot_mower_events(self):
        events = [
            {
                "id": "mower-1",
                "camera": "back",
                "label": "robot_lawnmower",
                "zones": ["yard"],
                "start_time": 1000,
                "end_time": 1020,
                "data": {"description": "Robot lawnmower is mowing the lawn."},
            },
            {
                "id": "mower-2",
                "camera": "back",
                "label": "robot_lawnmower",
                "zones": ["yard"],
                "start_time": 1120,
                "end_time": 1140,
                "data": {"description": "Autonomous lawn maintenance continues."},
            },
        ]

        candidates = discover_smart_rule_candidates(events)

        self.assertEqual(candidates[0]["type"], "mowing")
        self.assertEqual(candidates[0]["suggested_rule"]["scope"]["zones"], ["yard"])
        self.assertGreaterEqual(candidates[0]["confidence"], 0.7)

    def test_discovers_dog_play_candidate_from_person_and_dog_session(self):
        events = [
            {
                "id": "person-1",
                "camera": "patio",
                "label": "person",
                "zones": ["patio"],
                "sub_label": "Parker",
                "start_time": 2000,
                "end_time": 2010,
                "data": {"description": "Known person walking around the patio."},
            },
            {
                "id": "dog-1",
                "camera": "patio",
                "label": "dog",
                "zones": ["patio"],
                "sub_label": "Gizmo",
                "start_time": 2040,
                "end_time": 2050,
                "data": {"description": "Dog running around outside."},
            },
        ]

        candidates = discover_smart_rule_candidates(events)

        dog_candidate = next(item for item in candidates if item["type"] == "dog_play")
        self.assertEqual(dog_candidate["suggested_rule"]["scope"]["cameras"], ["patio"])
        self.assertEqual(dog_candidate["suggested_rule"]["context_labels"], ["person"])
        self.assertIn("person and dog", dog_candidate["explanation"].lower())

    def test_discovers_dog_play_even_when_mower_is_in_same_session(self):
        events = [
            {
                "id": "person-1",
                "camera": "patio",
                "label": "person",
                "zones": ["yard"],
                "start_time": 2000,
                "end_time": 2010,
                "data": {"description": "Person is playing outside in the yard."},
            },
            {
                "id": "dog-1",
                "camera": "patio",
                "label": "dog",
                "zones": ["yard"],
                "start_time": 2040,
                "end_time": 2050,
                "data": {"description": "Dog running around outside."},
            },
            {
                "id": "mower-1",
                "camera": "patio",
                "label": "robot_lawnmower",
                "zones": ["yard"],
                "start_time": 2060,
                "end_time": 2070,
                "data": {"description": "Robot lawnmower is mowing nearby."},
            },
        ]

        candidates = discover_smart_rule_candidates(events)

        self.assertIn("dog_play", {item["type"] for item in candidates})
        self.assertIn("mowing", {item["type"] for item in candidates})

    def test_dog_play_rule_needs_recent_person_context(self):
        events = [
            {
                "id": "dog-only",
                "camera": "patio",
                "label": "dog",
                "zones": ["yard"],
                "start_time": 1000,
                "end_time": 1010,
                "data": {"description": "Dog running outside."},
            },
            {
                "id": "person-dog-1",
                "camera": "patio",
                "label": "person",
                "zones": ["yard"],
                "start_time": 2300,
                "end_time": 2310,
                "data": {"description": "Person outside in the yard."},
            },
            {
                "id": "person-dog-2",
                "camera": "patio",
                "label": "dog",
                "zones": ["yard"],
                "start_time": 2320,
                "end_time": 2330,
                "data": {"description": "Dog playing in the yard."},
            },
        ]
        rule = next(
            item["suggested_rule"]
            for item in discover_smart_rule_candidates(events)
            if item["type"] == "dog_play"
        )

        matches = sessions_matching_rule(rule, events)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["event_ids"], ["person-dog-1", "person-dog-2"])

    def test_runtime_match_uses_recent_context_for_session_rules(self):
        rule = {
            "id": "dog-rule",
            "name": "Outside with dogs",
            "mode_type": "dog_play",
            "enabled": True,
            "activation_policy": "ask",
            "scope": {"cameras": ["patio"], "zones": ["yard"]},
            "required_labels": ["dog"],
            "context_labels": ["person"],
            "description_keywords": ["dog", "yard"],
            "suppress_labels": ["person", "dog"],
            "update_instead_of_push": True,
            "quiet_after_seconds": 600,
            "max_duration_seconds": 3600,
            "confidence_threshold": 0.65,
        }

        match = smart_rule_runtime_match(
            [rule],
            {
                "id": "dog-1",
                "camera": "patio",
                "label": "dog",
                "zones": ["yard"],
                "start_time": 3040,
                "end_time": 3050,
                "data": {"description": "Dog playing in the yard."},
            },
            recent_events=[
                {
                    "id": "person-1",
                    "camera": "patio",
                    "label": "person",
                    "zones": ["yard"],
                    "start_time": 3000,
                    "end_time": 3010,
                    "data": {"description": "Person outside in the yard."},
                }
            ],
        )

        self.assertIsNotNone(match)

    def test_ignores_generic_public_person_activity_as_outdoor_work(self):
        events = [
            {
                "id": "person-1",
                "camera": "front",
                "label": "person",
                "zones": ["Public", "Street"],
                "start_time": 2100,
                "end_time": 2110,
                "data": {"description": "A worker walks down the street."},
            }
        ]

        candidates = discover_smart_rule_candidates(events)

        self.assertNotIn("outdoor_work", {item["type"] for item in candidates})

    def test_rule_matching_returns_recent_sessions_for_validation(self):
        rule = SmartRule(
            id="rule-1",
            name="Backyard mowing",
            mode_type="mowing",
            enabled=True,
            activation_policy="ask",
            scope={"zones": ["yard"], "cameras": []},
            required_labels=["robot_lawnmower"],
            description_keywords=["mow", "lawn"],
            suppress_labels=["person", "robot_lawnmower"],
            update_instead_of_push=True,
            quiet_after_seconds=600,
            max_duration_seconds=3600,
            confidence_threshold=0.6,
        )
        events = [
            {
                "id": "mower-1",
                "camera": "side",
                "label": "robot_lawnmower",
                "zones": ["yard"],
                "start_time": 3000,
                "end_time": 3010,
                "data": {"description": "Mowing the lawn."},
            },
            {
                "id": "person-1",
                "camera": "front",
                "label": "person",
                "zones": ["Public"],
                "start_time": 3015,
                "end_time": 3020,
                "data": {"description": "Walking by."},
            },
        ]

        matches = sessions_matching_rule(rule, events)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["event_ids"], ["mower-1"])

    def test_negative_feedback_tightens_rule_scope(self):
        rule = SmartRule(
            id="rule-1",
            name="Outdoor work",
            mode_type="outdoor_work",
            enabled=True,
            activation_policy="ask",
            scope={"zones": ["yard", "street"], "cameras": ["side"]},
            required_labels=["person"],
            description_keywords=["tool", "work"],
            suppress_labels=["person"],
            update_instead_of_push=True,
            quiet_after_seconds=900,
            max_duration_seconds=3600,
            confidence_threshold=0.55,
        )

        updated = apply_feedback_to_rule(
            rule,
            {
                "verdict": "wrong",
                "cameras": ["side"],
                "zones": ["street"],
                "labels": ["person"],
            },
        )

        self.assertIn("street", updated.excluded_zones)
        self.assertGreater(updated.confidence_threshold, rule.confidence_threshold)


if __name__ == "__main__":
    unittest.main()
