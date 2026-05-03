"""Smart notification rule discovery and matching.

This module is intentionally Home Assistant-free so the rule miner can be
tested against Frigate event fixtures and live-event exports.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any


DEFAULT_SESSION_GAP_SECONDS = 8 * 60

_MOWING_KEYWORDS = {
    "mow",
    "mower",
    "mowing",
    "lawn",
    "grass cutting",
    "lawn maintenance",
}
_DOG_PLAY_KEYWORDS = {
    "dog",
    "outside",
    "play",
    "playing",
    "running",
    "patio",
    "yard",
}
_WORK_KEYWORDS = {
    "tool",
    "tools",
    "equipment",
    "yard work",
    "garden",
    "gardening",
    "hose",
    "ladder",
    "edger",
    "repair",
    "chipper",
    "branches",
    "mulch",
    "trimmer",
    "weed",
    "weeding",
}
_OWNED_ACTIVITY_ZONES = {"backyard", "driveway", "patio", "property", "yard"}
_PUBLIC_ZONES = {"public", "public2", "street"}


@dataclass
class SmartRule:
    """A user-confirmed smart alert mode rule."""

    id: str
    name: str
    mode_type: str
    enabled: bool
    activation_policy: str
    scope: dict[str, list[str]]
    required_labels: list[str]
    description_keywords: list[str]
    suppress_labels: list[str]
    update_instead_of_push: bool
    quiet_after_seconds: int
    max_duration_seconds: int
    confidence_threshold: float
    excluded_cameras: list[str] = field(default_factory=list)
    excluded_zones: list[str] = field(default_factory=list)
    context_labels: list[str] = field(default_factory=list)
    training_examples: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SmartRule":
        """Deserialize a rule from bridge/app JSON."""
        return cls(
            id=str(data.get("id") or f"rule-{int(time.time())}"),
            name=str(data.get("name") or "Smart mode"),
            mode_type=str(data.get("mode_type") or data.get("type") or "custom"),
            enabled=bool(data.get("enabled", True)),
            activation_policy=str(data.get("activation_policy") or "ask"),
            scope=_scope_dict(data.get("scope")),
            required_labels=_string_list(data.get("required_labels")),
            description_keywords=_string_list(data.get("description_keywords")),
            suppress_labels=_string_list(data.get("suppress_labels")),
            update_instead_of_push=bool(data.get("update_instead_of_push", True)),
            quiet_after_seconds=_bounded_int(
                data.get("quiet_after_seconds"),
                default=10 * 60,
                minimum=60,
                maximum=24 * 3600,
            ),
            max_duration_seconds=_bounded_int(
                data.get("max_duration_seconds"),
                default=60 * 60,
                minimum=5 * 60,
                maximum=24 * 3600,
            ),
            confidence_threshold=_bounded_float(
                data.get("confidence_threshold"),
                default=0.65,
                minimum=0,
                maximum=1,
            ),
            excluded_cameras=_string_list(data.get("excluded_cameras")),
            excluded_zones=_string_list(data.get("excluded_zones")),
            context_labels=_string_list(data.get("context_labels")),
            training_examples=list(data.get("training_examples") or []),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize a rule for mobile and bridge storage."""
        return asdict(self)


def normalize_smart_rules(raw: Any) -> list[dict[str, Any]]:
    """Normalize a list of smart-rule dicts for device storage."""
    if not isinstance(raw, list):
        return []
    return [SmartRule.from_dict(item).to_dict() for item in raw if isinstance(item, dict)]


def discover_smart_rule_candidates(
    events: list[dict[str, Any]],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Discover likely smart alert rules from recent Frigate events."""
    normalized = sorted((_normalize_event(event) for event in events), key=lambda item: item["start_time"])
    sessions = _sessionize(normalized)
    candidates: list[dict[str, Any]] = []

    for session in sessions:
        candidates.extend(_candidates_from_session(session, now=now))

    candidates.sort(
        key=lambda item: (
            float(item.get("confidence", 0)),
            int(item.get("event_count", 0)),
        ),
        reverse=True,
    )
    return _dedupe_candidates(candidates)


def sessions_matching_rule(
    rule: SmartRule | dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return recent grouped sessions that match a configured rule."""
    parsed = SmartRule.from_dict(rule) if isinstance(rule, dict) else rule
    normalized = sorted((_normalize_event(event) for event in events), key=lambda item: item["start_time"])
    sessions = _sessionize(normalized)
    matches: list[dict[str, Any]] = []
    for session in sessions:
        confidence, reasons = _score_rule_session(parsed, session)
        if confidence >= parsed.confidence_threshold:
            matches.append(_session_summary(session, confidence, reasons))
    return matches


def apply_feedback_to_rule(
    rule: SmartRule | dict[str, Any],
    feedback: dict[str, Any],
) -> SmartRule:
    """Tune a rule from a validation verdict."""
    parsed = SmartRule.from_dict(rule if isinstance(rule, dict) else rule.to_dict())
    verdict = str(feedback.get("verdict") or "").strip().lower()
    example = {
        "verdict": verdict,
        "event_ids": _string_list(feedback.get("event_ids")),
        "cameras": _string_list(feedback.get("cameras")),
        "zones": _string_list(feedback.get("zones")),
        "labels": _string_list(feedback.get("labels")),
        "recorded_at": int(time.time()),
    }
    parsed.training_examples.append(example)

    if verdict == "wrong":
        parsed.confidence_threshold = min(0.95, parsed.confidence_threshold + 0.1)
        for zone in example["zones"]:
            if zone not in parsed.excluded_zones:
                parsed.excluded_zones.append(zone)
        for camera in example["cameras"]:
            if camera not in parsed.excluded_cameras and not _scope_has_only_camera(parsed, camera):
                parsed.excluded_cameras.append(camera)
    elif verdict == "correct":
        parsed.confidence_threshold = max(0.35, parsed.confidence_threshold - 0.03)

    parsed.excluded_cameras = sorted(set(parsed.excluded_cameras))
    parsed.excluded_zones = sorted(set(parsed.excluded_zones))
    return parsed


def smart_rule_runtime_match(
    rules: list[dict[str, Any]],
    event: dict[str, Any],
    *,
    recent_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return the best active rule match for an incoming event."""
    event_session = _runtime_session(
        _normalize_event(event),
        [_normalize_event(item) for item in recent_events or []],
    )
    best: tuple[float, SmartRule, list[str]] | None = None
    for raw_rule in rules:
        rule = SmartRule.from_dict(raw_rule)
        if not rule.enabled:
            continue
        confidence, reasons = _score_rule_session(rule, event_session)
        if confidence < rule.confidence_threshold:
            continue
        if best is None or confidence > best[0]:
            best = (confidence, rule, reasons)
    if best is None:
        return None
    confidence, rule, reasons = best
    return {
        "rule": rule.to_dict(),
        "confidence": confidence,
        "reasons": reasons,
        "action": "update" if rule.update_instead_of_push else "suppress",
    }


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    description = str(data.get("description") or event.get("description") or "")
    zones = _string_list(event.get("zones"))
    return {
        "id": str(event.get("id") or event.get("event_id") or ""),
        "camera": str(event.get("camera") or ""),
        "label": str(event.get("label") or "object").strip().lower(),
        "sub_label": str(event.get("sub_label") or "").strip(),
        "zones": zones,
        "start_time": float(event.get("start_time") or event.get("timestamp") or 0),
        "end_time": float(event.get("end_time") or event.get("start_time") or event.get("timestamp") or 0),
        "description": description,
        "description_lc": description.lower(),
    }


def _sessionize(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    sessions: list[list[dict[str, Any]]] = []
    for event in events:
        if event["start_time"] <= 0:
            continue
        if not sessions:
            sessions.append([event])
            continue
        last = sessions[-1][-1]
        same_scope = _event_scope(last) == _event_scope(event)
        gap = event["start_time"] - max(last["end_time"], last["start_time"])
        if same_scope and gap <= DEFAULT_SESSION_GAP_SECONDS:
            sessions[-1].append(event)
        else:
            sessions.append([event])
    return sessions


def _candidates_from_session(
    session: list[dict[str, Any]],
    *,
    now: float | None,
) -> list[dict[str, Any]]:
    labels = {event["label"] for event in session}
    text = " ".join(event["description_lc"] for event in session)
    cameras = sorted({event["camera"] for event in session if event["camera"]})
    zones = sorted({zone for event in session for zone in event["zones"]})
    label_counts = {label: sum(1 for event in session if event["label"] == label) for label in labels}
    candidates: list[dict[str, Any]] = []

    if "robot_lawnmower" in labels or _contains_any(text, _MOWING_KEYWORDS):
        confidence = 0.75 + (0.1 if "robot_lawnmower" in labels else 0)
        candidates.append(
            _candidate(
                "mowing",
                "Mowing",
                confidence,
                session,
                cameras,
                _owned_scope_zones(zones) or zones,
                ["robot_lawnmower"] if "robot_lawnmower" in labels else ["person"],
                _matched_keywords(text, _MOWING_KEYWORDS) or ["mow", "lawn"],
                ["person", "robot_lawnmower"],
                "Mower or lawn-maintenance activity repeated in the same area.",
                now=now,
            )
        )

    if (
        "dog" in labels
        and ("person" in labels or any(event["sub_label"] for event in session))
        and _is_owned_activity_area(zones, cameras, text)
    ):
        confidence = 0.76 + (0.04 if "person" in labels else 0)
        candidates.append(
            _candidate(
                "dog_play",
                "Outside with dogs",
                confidence,
                session,
                cameras,
                _owned_scope_zones(zones) or zones,
                ["dog"],
                _matched_keywords(text, _DOG_PLAY_KEYWORDS) or ["dog", "outside", "yard"],
                ["person", "dog"],
                "Person and dog activity appeared close together.",
                now=now,
                context_labels=["person"],
            )
        )

    if (
        "person" in labels
        and _contains_any(text, _WORK_KEYWORDS)
        and _is_owned_activity_area(zones, cameras, text)
        and not _only_public_zones(zones)
    ):
        candidates.append(
            _candidate(
                "outdoor_work",
                "Outdoor work",
                0.72 + min(0.15, label_counts.get("person", 0) * 0.03),
                session,
                cameras,
                _owned_scope_zones(zones) or zones,
                ["person"],
                _matched_keywords(text, _WORK_KEYWORDS) or ["tool", "yard work"],
                ["person"],
                "Person activity includes yard, tool, or outdoor-work descriptions.",
                now=now,
            )
        )

    return candidates


def _candidate(
    mode_type: str,
    name: str,
    confidence: float,
    session: list[dict[str, Any]],
    cameras: list[str],
    zones: list[str],
    required_labels: list[str],
    description_keywords: list[str],
    suppress_labels: list[str],
    explanation: str,
    *,
    now: float | None,
    context_labels: list[str] | None = None,
) -> dict[str, Any]:
    candidate_id = f"{mode_type}:{','.join(cameras)}:{','.join(zones)}"
    rule = SmartRule(
        id=f"smart-{mode_type}-{abs(hash(candidate_id)) % 100000}",
        name=name,
        mode_type=mode_type,
        enabled=True,
        activation_policy="ask",
        scope={"cameras": cameras, "zones": zones},
        required_labels=required_labels,
        description_keywords=description_keywords,
        suppress_labels=suppress_labels,
        update_instead_of_push=True,
        quiet_after_seconds=10 * 60 if mode_type != "mowing" else 15 * 60,
        max_duration_seconds=60 * 60,
        confidence_threshold=0.65,
        context_labels=context_labels or [],
    )
    return {
        "id": candidate_id,
        "type": mode_type,
        "suggested_name": name,
        "confidence": round(min(0.95, confidence), 2),
        "event_count": len(session),
        "explanation": explanation,
        "matched_session": _session_summary(session, confidence, [explanation]),
        "suggested_rule": rule.to_dict(),
        "created_at": int(now or time.time()),
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    type_counts: dict[str, int] = {}
    for candidate in candidates:
        key = str(candidate["id"])
        if key in seen:
            continue
        candidate_type = str(candidate.get("type") or "custom")
        if type_counts.get(candidate_type, 0) >= 4:
            continue
        seen.add(key)
        type_counts[candidate_type] = type_counts.get(candidate_type, 0) + 1
        result.append(candidate)
    return result[:12]


def _score_rule_session(rule: SmartRule, session: list[dict[str, Any]]) -> tuple[float, list[str]]:
    labels = {event["label"] for event in session}
    cameras = {event["camera"] for event in session if event["camera"]}
    zones = {zone for event in session for zone in event["zones"]}
    text = " ".join(event["description_lc"] for event in session)
    reasons: list[str] = []
    score = 0.0

    if rule.excluded_cameras and cameras.intersection(rule.excluded_cameras):
        return 0, ["excluded camera"]
    if rule.excluded_zones and zones.intersection(rule.excluded_zones):
        return 0, ["excluded zone"]

    scope = _scope_dict(rule.scope)
    scope_cameras = set(scope.get("cameras", []))
    scope_zones = set(scope.get("zones", []))
    if scope_cameras:
        if not cameras.intersection(scope_cameras):
            return 0, ["outside camera scope"]
        score += 0.2
        reasons.append("camera scope matched")
    if scope_zones:
        if not zones.intersection(scope_zones):
            return 0, ["outside zone scope"]
        score += 0.25
        reasons.append("zone scope matched")

    required_labels = set(rule.required_labels)
    if required_labels:
        matched_labels = labels.intersection(required_labels)
        if not matched_labels:
            return 0, ["required label missing"]
        score += 0.35
        reasons.append(f"label matched: {', '.join(sorted(matched_labels))}")

    context_labels = set(rule.context_labels)
    if context_labels:
        missing_context = context_labels.difference(labels)
        if missing_context:
            return 0, [
                f"context label missing: {', '.join(sorted(missing_context))}"
            ]
        score += 0.18
        reasons.append(
            f"context label matched: {', '.join(sorted(context_labels))}"
        )

    keywords = [keyword.lower() for keyword in rule.description_keywords if keyword.strip()]
    matched_keywords = [keyword for keyword in keywords if keyword in text]
    if matched_keywords:
        score += min(0.3, 0.08 * len(matched_keywords))
        reasons.append(f"description matched: {', '.join(matched_keywords[:4])}")

    if not required_labels and not matched_keywords:
        return 0, ["no rule signals matched"]

    if len(session) > 1:
        score += min(0.1, len(session) * 0.02)
        reasons.append("repeated session activity")

    return min(1.0, score), reasons


def _session_summary(
    session: list[dict[str, Any]],
    confidence: float,
    reasons: list[str],
) -> dict[str, Any]:
    start = min(event["start_time"] for event in session)
    end = max(max(event["end_time"], event["start_time"]) for event in session)
    return {
        "start_time": start,
        "end_time": end,
        "duration_seconds": max(0, end - start),
        "event_ids": [event["id"] for event in session if event["id"]],
        "cameras": sorted({event["camera"] for event in session if event["camera"]}),
        "zones": sorted({zone for event in session for zone in event["zones"]}),
        "labels": sorted({event["label"] for event in session if event["label"]}),
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "examples": [
            {
                "id": event["id"],
                "camera": event["camera"],
                "label": event["label"],
                "zones": event["zones"],
                "sub_label": event["sub_label"] or None,
                "description": event["description"][:240],
                "start_time": event["start_time"],
            }
            for event in session[:6]
        ],
    }


def _runtime_session(
    current: dict[str, Any],
    recent_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_start = current["start_time"]
    scope = _event_scope(current)
    session = [
        event
        for event in recent_events
        if event["start_time"] > 0
        and _event_scope(event) == scope
        and current_start - max(event["end_time"], event["start_time"])
        <= DEFAULT_SESSION_GAP_SECONDS
    ]
    session.append(current)
    session.sort(key=lambda item: item["start_time"])

    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in session:
        event_id = event.get("id")
        if event_id and event_id in seen_ids:
            continue
        if event_id:
            seen_ids.add(event_id)
        deduped.append(event)
    return deduped


def _scope_dict(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {"cameras": [], "zones": []}
    return {
        "cameras": _string_list(raw.get("cameras")),
        "zones": _string_list(raw.get("zones")),
    }


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return sorted({str(item).strip() for item in raw if str(item).strip()})


def _event_scope(event: dict[str, Any]) -> str:
    zones = set(event["zones"])
    if zones.intersection({"yard", "patio", "backyard"}):
        return "backyard"
    if zones.intersection({"Property", "driveway"}):
        return "property"
    return event["camera"] or "unknown"


def _contains_any(text: str, keywords: set[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _matched_keywords(text: str, keywords: set[str]) -> list[str]:
    return sorted(keyword for keyword in keywords if keyword in text)


def _is_owned_activity_area(zones: list[str], cameras: list[str], text: str) -> bool:
    normalized_zones = {zone.strip().lower() for zone in zones}
    if normalized_zones.intersection(_OWNED_ACTIVITY_ZONES):
        return True
    if normalized_zones and normalized_zones.issubset(_PUBLIC_ZONES):
        return False
    camera_words = " ".join(cameras).lower()
    return any(
        signal in text or signal in camera_words
        for signal in ("backyard", "driveway", "garden", "lawn", "patio", "yard")
    )


def _only_public_zones(zones: list[str]) -> bool:
    normalized_zones = {zone.strip().lower() for zone in zones if zone.strip()}
    return bool(normalized_zones) and normalized_zones.issubset(_PUBLIC_ZONES)


def _owned_scope_zones(zones: list[str]) -> list[str]:
    return [
        zone
        for zone in zones
        if zone.strip().lower() in _OWNED_ACTIVITY_ZONES
    ]


def _scope_has_only_camera(rule: SmartRule, camera: str) -> bool:
    cameras = set(rule.scope.get("cameras", []))
    return cameras == {camera}


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
