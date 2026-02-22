from __future__ import annotations

from django.test import SimpleTestCase

from core.services.reviewer_notification_settings import (
    DEFAULT_AUTO_UNASSIGN_DAYS,
    DEFAULT_STALE_NUDGE_DAYS,
    parse_notification_policy,
)


class ReviewerNotificationSettingsTests(SimpleTestCase):
    def test_defaults_when_payload_missing(self) -> None:
        policy = parse_notification_policy(None)

        self.assertEqual(policy.stale_nudge_days, DEFAULT_STALE_NUDGE_DAYS)
        self.assertEqual(policy.auto_unassign_days, DEFAULT_AUTO_UNASSIGN_DAYS)

    def test_defaults_when_values_invalid(self) -> None:
        policy = parse_notification_policy({"stale_nudge_days": "abc", "auto_unassign_days": 0})

        self.assertEqual(policy.stale_nudge_days, DEFAULT_STALE_NUDGE_DAYS)
        self.assertEqual(policy.auto_unassign_days, DEFAULT_AUTO_UNASSIGN_DAYS)

    def test_normalizes_order_when_y_not_greater_than_x(self) -> None:
        policy = parse_notification_policy({"stale_nudge_days": 5, "auto_unassign_days": 5})

        self.assertEqual(policy.stale_nudge_days, 5)
        self.assertEqual(policy.auto_unassign_days, 6)

    def test_accepts_valid_values(self) -> None:
        policy = parse_notification_policy({"stale_nudge_days": "4", "auto_unassign_days": "9"})

        self.assertEqual(policy.stale_nudge_days, 4)
        self.assertEqual(policy.auto_unassign_days, 9)
