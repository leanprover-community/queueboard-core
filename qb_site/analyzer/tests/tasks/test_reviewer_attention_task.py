from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport
from analyzer.tasks.reviewer_attention import reviewer_attention_daily_task
from core.models import Repository


class ReviewerAttentionDailyTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_skips_when_feature_disabled(self, mock_build_reports) -> None:
        res = reviewer_attention_daily_task.apply().get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "feature_disabled")
        mock_build_reports.assert_not_called()

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_returns_dry_run_summary(self, mock_build_reports) -> None:
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=1,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=None,
                        queue_anchor_at=None,
                        days_on_queue_since_assignment=16,
                        total_queue_seconds=16 * 24 * 60 * 60,
                        total_queue_days=16,
                        needs_nudge=True,
                        needs_auto_unassign=False,
                        missing_assignment_timestamp=False,
                    ),
                    ReviewerAttentionItem(
                        pr_number=102,
                        pr_title="PR 102",
                        is_on_queue=True,
                        last_assigned_at=None,
                        queue_anchor_at=None,
                        days_on_queue_since_assignment=25,
                        total_queue_seconds=25 * 24 * 60 * 60,
                        total_queue_days=25,
                        needs_nudge=False,
                        needs_auto_unassign=True,
                        missing_assignment_timestamp=True,
                    ),
                ),
                warnings=("Missing assignment timestamp for PR #102.",),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["repos"], 1)
        self.assertEqual(res["totals"]["would_nudge"], 1)
        self.assertEqual(res["totals"]["would_auto_unassign"], 1)
        self.assertEqual(res["totals"]["reviewers_to_notify"], 1)
        self.assertEqual(res["totals"]["missing_assignment_timestamps"], 1)
        self.assertEqual(res["totals"]["warnings"], 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
    )
    def test_returns_repo_not_found_when_repository_filter_misses(self) -> None:
        res = reviewer_attention_daily_task.apply(kwargs={"repository_id": 999999}).get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")
        self.assertTrue(res["enforcement_enabled"])
