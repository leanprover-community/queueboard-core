from __future__ import annotations

from django.test import SimpleTestCase

from analyzer.models.queue_rule import QueueRuleSet, resolve_ci_gating_mode


class TestQueueRuleCIGatingMode(SimpleTestCase):
    def test_require_ci_false_disables_mode(self) -> None:
        mode = resolve_ci_gating_mode(
            require_ci_success=False,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
        )
        self.assertIsNone(mode)

    def test_require_ci_true_defaults_to_strict_mode(self) -> None:
        mode = resolve_ci_gating_mode(require_ci_success=True, ci_gating_mode=None)
        self.assertEqual(mode, QueueRuleSet.CIGatingMode.ALL_REQUIRED_SUCCESS)

    def test_require_ci_true_keeps_no_fail_mode(self) -> None:
        mode = resolve_ci_gating_mode(
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES,
        )
        self.assertEqual(mode, QueueRuleSet.CIGatingMode.NO_REQUIRED_FAILURES)
