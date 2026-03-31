"""In-memory issue manager for standalone mode.

Tracks active delivery issues without Home Assistant Repairs integration.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ISSUE_HISTORY = 20


class StandaloneIssueManager:
    """Manages bridge issues for standalone mode."""

    def __init__(self) -> None:
        self._active_issues: dict[str, dict[str, Any]] = {}
        self._issue_history: list[dict[str, Any]] = []

    @property
    def active_issues(self) -> list[dict[str, Any]]:
        """Return list of active issues."""
        return list(self._active_issues.values())

    @property
    def active_issue_count(self) -> int:
        return len(self._active_issues)

    def raise_issue(
        self,
        issue_type: str,
        title: str,
        description: str,
        severity: str = "warning",
        affected_devices: list[str] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        suggested_action: str | None = None,
        fingerprint: str | None = None,
    ) -> str:
        """Raise or update a bridge issue. Returns issue_id."""
        # Use fingerprint to deduplicate recurring issues of the same type
        fp = fingerprint or issue_type
        for existing_id, existing in self._active_issues.items():
            if existing.get("fingerprint") == fp:
                # Update existing issue
                existing["timestamp"] = datetime.utcnow().isoformat()
                existing["title"] = title
                existing["description"] = description
                existing["affected_devices"] = affected_devices or []
                existing["error_detail"] = error_detail
                logger.debug("Updated existing issue %s (%s)", existing_id, issue_type)
                return existing_id

        issue_id = str(uuid.uuid4())
        issue: dict[str, Any] = {
            "id": issue_id,
            "type": issue_type,
            "severity": severity,
            "title": title,
            "description": description,
            "affected_devices": affected_devices or [],
            "error_code": error_code,
            "error_detail": error_detail,
            "suggested_action": suggested_action,
            "timestamp": datetime.utcnow().isoformat(),
            "fingerprint": fp,
        }

        self._active_issues[issue_id] = issue
        self._append_history(issue)
        logger.info("Raised issue %s: %s", issue_id, title)
        return issue_id

    def dismiss_issue(self, issue_id: str) -> bool:
        """Dismiss an active issue. Returns True if found and removed."""
        if issue_id in self._active_issues:
            del self._active_issues[issue_id]
            logger.info("Dismissed issue %s", issue_id)
            return True
        return False

    def clear_issues_for_type(self, issue_type: str) -> None:
        """Remove all active issues of a given type (e.g., when resolved)."""
        to_remove = [
            issue_id for issue_id, issue in self._active_issues.items()
            if issue.get("type") == issue_type
        ]
        for issue_id in to_remove:
            del self._active_issues[issue_id]

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return self._active_issues.get(issue_id)

    def _append_history(self, issue: dict[str, Any]) -> None:
        self._issue_history.append(dict(issue))
        if len(self._issue_history) > _MAX_ISSUE_HISTORY:
            self._issue_history = self._issue_history[-_MAX_ISSUE_HISTORY:]
