from __future__ import annotations

from django.test import TestCase

from analyzer.models import QueueRuleSet
from analyzer.services.queue_rules import default_rule_set_for_repo, load_rules_for_repo
from core.models import Repository


def _mk_ruleset(repo: Repository, version: int, *, is_active: bool = True, is_default: bool = False) -> QueueRuleSet:
    return QueueRuleSet.objects.create(
        repository=repo,
        version=version,
        is_active=is_active,
        is_default=is_default,
    )


class DefaultRuleSetForRepoTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master")

    def test_no_rulesets_returns_none(self) -> None:
        self.assertIsNone(default_rule_set_for_repo(self.repo))

    def test_only_inactive_rulesets_returns_none(self) -> None:
        _mk_ruleset(self.repo, 1, is_active=False)
        self.assertIsNone(default_rule_set_for_repo(self.repo))

    def test_single_active_no_default_flag_falls_back_to_highest_version(self) -> None:
        rs1 = _mk_ruleset(self.repo, 1)
        rs2 = _mk_ruleset(self.repo, 2)
        result = default_rule_set_for_repo(self.repo)
        self.assertEqual(result, rs2)
        _ = rs1  # referenced to satisfy linter

    def test_is_default_flag_preferred_over_higher_version(self) -> None:
        _mk_ruleset(self.repo, 2)  # higher version but not default
        rs1 = _mk_ruleset(self.repo, 1, is_default=True)
        result = default_rule_set_for_repo(self.repo)
        self.assertEqual(result, rs1)

    def test_inactive_is_default_ignored(self) -> None:
        _mk_ruleset(self.repo, 1, is_active=False, is_default=True)
        rs2 = _mk_ruleset(self.repo, 2, is_active=True, is_default=False)
        result = default_rule_set_for_repo(self.repo)
        self.assertEqual(result, rs2)

    def test_load_rules_for_repo_prefers_is_default(self) -> None:
        # Version 2 is higher, but version 1 is marked as default.
        rs1 = _mk_ruleset(self.repo, 1, is_default=True)
        rs1.require_ci_success = True
        rs1.save()
        _mk_ruleset(self.repo, 2, is_default=False)

        rules = load_rules_for_repo(self.repo)
        self.assertTrue(rules.require_ci_success)

    def test_load_rules_for_repo_no_rulesets_returns_defaults(self) -> None:
        rules = load_rules_for_repo(self.repo)
        self.assertTrue(rules.require_open)
        self.assertTrue(rules.require_not_draft)
        self.assertFalse(rules.require_ci_success)
