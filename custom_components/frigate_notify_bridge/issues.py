"""Home Assistant Repairs issue helpers for Frigate Notify Bridge."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_PUSH_PROVIDER_UNAVAILABLE = "push_provider_unavailable"
ISSUE_NOTIFICATION_DELIVERY = "notification_delivery_failures"
_PUSH_ALERT_COOLDOWN = timedelta(hours=6)

IssueAlertCallback = Callable[[str, str, str], Awaitable[None]]


class BridgeIssueManager:
    """Track active Repairs issues and optional bridge attention alerts."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the issue manager."""
        self.hass = hass
        self._active_fingerprints: dict[str, str] = {}
        self._last_alert_at: dict[str, datetime] = {}

    async def async_report_push_provider_unavailable(
        self,
        provider_name: str,
        reason: str,
    ) -> None:
        """Create or update the provider availability issue."""
        await self.async_raise_issue(
            issue_id=ISSUE_PUSH_PROVIDER_UNAVAILABLE,
            translation_key=ISSUE_PUSH_PROVIDER_UNAVAILABLE,
            translation_placeholders={
                "provider": provider_name,
                "reason": reason,
            },
            severity=ir.IssueSeverity.ERROR,
            fingerprint=f"{provider_name}:{reason}",
        )

    async def async_report_notification_delivery_failure(
        self,
        failed_devices: list[str],
        reason: str,
        send_alert: IssueAlertCallback | None = None,
    ) -> None:
        """Create or update the delivery failure issue."""
        placeholder_devices = ", ".join(failed_devices[:3])
        if len(failed_devices) > 3:
            placeholder_devices = f"{placeholder_devices}, +{len(failed_devices) - 3} more"

        await self.async_raise_issue(
            issue_id=ISSUE_NOTIFICATION_DELIVERY,
            translation_key=ISSUE_NOTIFICATION_DELIVERY,
            translation_placeholders={
                "failed_count": str(len(failed_devices)),
                "devices": placeholder_devices or "unknown devices",
                "reason": reason,
            },
            severity=ir.IssueSeverity.ERROR,
            fingerprint=f"{','.join(sorted(failed_devices))}:{reason}",
            send_alert=send_alert,
            alert_title="Frigate Notify Bridge Needs Attention",
            alert_body=(
                "The bridge detected notification delivery problems. "
                "Open Repairs in Home Assistant for details."
            ),
        )

    async def async_raise_issue(
        self,
        issue_id: str,
        translation_key: str,
        translation_placeholders: dict[str, str],
        severity: ir.IssueSeverity,
        fingerprint: str,
        send_alert: IssueAlertCallback | None = None,
        alert_title: str | None = None,
        alert_body: str | None = None,
    ) -> None:
        """Create or update a Repairs issue."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=severity,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
        )

        should_send_alert = (
            send_alert is not None
            and alert_title is not None
            and alert_body is not None
            and self._should_send_alert(issue_id, fingerprint)
        )
        self._active_fingerprints[issue_id] = fingerprint

        if should_send_alert:
            try:
                await send_alert(issue_id, alert_title, alert_body)
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.warning("Failed to send bridge issue alert for %s: %s", issue_id, err)

    async def async_clear_issue(self, issue_id: str) -> None:
        """Delete a Repairs issue if it is active."""
        try:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        except Exception:  # pragma: no cover - defensive cleanup
            pass
        self._active_fingerprints.pop(issue_id, None)
        self._last_alert_at.pop(issue_id, None)

    async def async_clear_all(self) -> None:
        """Delete all active Repairs issues for this manager."""
        for issue_id in list(self._active_fingerprints):
            await self.async_clear_issue(issue_id)

    def _should_send_alert(self, issue_id: str, fingerprint: str) -> bool:
        """Rate-limit bridge attention alerts for repeated issue updates."""
        previous_fingerprint = self._active_fingerprints.get(issue_id)
        if previous_fingerprint != fingerprint:
            self._last_alert_at[issue_id] = datetime.utcnow()
            return True

        last_alert = self._last_alert_at.get(issue_id)
        if last_alert is None or datetime.utcnow() - last_alert >= _PUSH_ALERT_COOLDOWN:
            self._last_alert_at[issue_id] = datetime.utcnow()
            return True

        return False
