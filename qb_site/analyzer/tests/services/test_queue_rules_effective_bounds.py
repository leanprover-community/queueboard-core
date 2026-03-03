from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet
from analyzer.services.queue_rules import load_rules_for_repo


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestQueueRulesEffectiveBounds(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)

    def test_load_ruleset_with_effective_bounds(self) -> None:
        # Legacy label-only ruleset for history before 2024-01-01
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
            effective_from=None,
            effective_to=_dt(2024, 1, 1),
        )
        # CI-gated ruleset for history from 2024-01-01 onward
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=2,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
            effective_from=_dt(2024, 1, 1),
            effective_to=None,
        )

        before = _dt(2023, 12, 31)
        at_cutoff = _dt(2024, 1, 1)
        after = _dt(2024, 6, 1)

        rules_before = load_rules_for_repo(self.repo, at=before)
        self.assertFalse(rules_before.require_ci_success)
        self.assertIsNone(rules_before.ci_gating_mode)

        rules_at_cutoff = load_rules_for_repo(self.repo, at=at_cutoff)
        self.assertTrue(rules_at_cutoff.require_ci_success)
        self.assertEqual(rules_at_cutoff.ci_gating_mode, QueueRuleSet.CIGatingMode.ALL_REQUIRED_SUCCESS)

        rules_after = load_rules_for_repo(self.repo, at=after)
        self.assertTrue(rules_after.require_ci_success)
        self.assertEqual(rules_after.ci_gating_mode, QueueRuleSet.CIGatingMode.ALL_REQUIRED_SUCCESS)

    def test_no_required_failures_mode_exposed_in_loaded_rules(self) -> None:
        QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
            required_ci_contexts=["lint"],
        )

        rules = load_rules_for_repo(self.repo, at=_dt(2025, 1, 1))
        self.assertTrue(rules.require_ci_success)
        self.assertEqual(rules.ci_gating_mode, QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES)
