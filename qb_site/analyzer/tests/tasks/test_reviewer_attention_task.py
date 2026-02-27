from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings

from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport
from analyzer.tasks.reviewer_attention import reviewer_attention_daily_task
from core.models import Repository, User
from core.services.github_assignment import AssignmentMutationError


class ReviewerAttentionDailyTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.user = User.objects.create(github_login="alice", zulip_user_id=101)
        self.other_user = User.objects.create(github_login="bob", zulip_user_id=202)

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
                reviewer_user_id=self.user.id,
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
                        needs_new_assignment_ping=True,
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
        self.assertFalse(res["delivery_enabled"])
        self.assertEqual(res["new_assignment_ping_window_seconds"], 86400)
        self.assertEqual(res["new_assignment_ping_window_source"], "period_seconds")
        self.assertEqual(res["repos"], 1)
        self.assertEqual(res["totals"]["would_nudge"], 1)
        self.assertEqual(res["totals"]["would_auto_unassign"], 1)
        self.assertEqual(res["totals"]["would_new_assignment_ping"], 1)
        self.assertEqual(res["totals"]["reviewers_to_notify"], 1)
        self.assertEqual(res["totals"]["missing_assignment_timestamps"], 1)
        self.assertEqual(res["totals"]["warnings"], 1)
        self.assertEqual(res["delivery"]["stats"]["skipped_delivery_disabled"], 1)
        self.assertEqual(res["enforcement"]["stats"]["skipped_disabled"], 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
    )
    def test_returns_repo_not_found_when_repository_filter_misses(self) -> None:
        res = reviewer_attention_daily_task.apply(kwargs={"repository_id": 999999}).get()

        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")
        self.assertTrue(res["enforcement_enabled"])

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_PERIOD_SECONDS=3600,
        ANALYZER_REVIEWER_ATTENTION_UTC_HOUR=9,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_uses_fixed_utc_clock_for_new_assignment_window(self, mock_build_reports) -> None:
        mock_build_reports.return_value = []

        res = reviewer_attention_daily_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertEqual(res["new_assignment_ping_window_seconds"], 24 * 60 * 60)
        self.assertEqual(res["new_assignment_ping_window_source"], "fixed_utc_clock")
        mock_build_reports.assert_called_once()

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_sends_one_message_per_reviewer_when_delivery_enabled(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(hours=6)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=None,
                        days_on_queue_since_assignment=16,
                        total_queue_seconds=16 * 24 * 60 * 60,
                        total_queue_days=16,
                        needs_new_assignment_ping=True,
                        needs_nudge=True,
                        needs_auto_unassign=False,
                        missing_assignment_timestamp=False,
                    ),
                ),
                warnings=(),
            )
        ]
        mock_client = mock_client_cls.return_value

        res = reviewer_attention_daily_task.apply().get()

        self.assertFalse(res["dry_run"])
        self.assertTrue(res["delivery_enabled"])
        self.assertEqual(res["delivery"]["stats"]["attempted"], 1)
        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        self.assertEqual(mock_client.send_direct_message.call_count, 1)
        kwargs = mock_client.send_direct_message.call_args.kwargs
        self.assertEqual(kwargs["to"], [101])
        self.assertIn("Assigned queue PRs that need your attention", kwargs["content"])
        self.assertIn("Newly assigned (1)", kwargs["content"])
        self.assertIn("Queue attention (1)", kwargs["content"])
        self.assertIn("since <time:", kwargs["content"])
        self.assertIn("Unassign yourself: `unassign #<number>`", kwargs["content"])
        self.assertIn("See all your assigned PRs: `assigned_prs`", kwargs["content"])
        self.assertIn("Change notification settings: `prefs`", kwargs["content"])

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_skips_delivery_when_user_has_no_zulip_user_id(self, mock_client_cls, mock_build_reports) -> None:
        self.user.zulip_user_id = None
        self.user.save(update_fields=["zulip_user_id"])
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
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
                        needs_new_assignment_ping=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertFalse(res["dry_run"])
        self.assertEqual(res["delivery"]["stats"]["skipped_no_zulip_user_id"], 1)
        self.assertEqual(res["delivery"]["stats"]["sent"], 0)
        mock_client = mock_client_cls.return_value
        mock_client.send_direct_message.assert_not_called()

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.resolve_github_app_operation_token", return_value="tok")
    @patch("analyzer.tasks.reviewer_attention.GitHubAssignmentClient")
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_enforcement_unassigns_before_sending_message(
        self,
        mock_zulip_client_cls,
        mock_build_reports,
        mock_assignment_client_cls,
        mock_resolve_token,
    ) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=30)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=assigned_at,
                        days_on_queue_since_assignment=30,
                        total_queue_seconds=30 * 24 * 60 * 60,
                        total_queue_days=30,
                        needs_auto_unassign=True,
                    ),
                ),
                warnings=(),
            )
        ]
        mock_assignment_client = mock_assignment_client_cls.return_value
        mock_zulip_client = mock_zulip_client_cls.return_value

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["enforcement"]["stats"]["candidates"], 1)
        self.assertEqual(res["enforcement"]["stats"]["attempted"], 1)
        self.assertEqual(res["enforcement"]["stats"]["unassigned"], 1)
        mock_resolve_token.assert_called_once_with(operation="unassign_pr", owner=self.repo.owner, repo=self.repo.name)
        mock_assignment_client.unassign.assert_called_once_with(
            owner=self.repo.owner,
            repo=self.repo.name,
            number=101,
            github_login="alice",
        )
        kwargs = mock_zulip_client.send_direct_message.call_args.kwargs
        self.assertIn("Auto-unassigned in this run (1)", kwargs["content"])

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=False,
    )
    @patch("analyzer.tasks.reviewer_attention.resolve_github_app_operation_token", return_value=None)
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_enforcement_skips_when_token_missing(self, mock_build_reports, _mock_resolve_token) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=30)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=assigned_at,
                        days_on_queue_since_assignment=30,
                        total_queue_seconds=30 * 24 * 60 * 60,
                        total_queue_days=30,
                        needs_auto_unassign=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["enforcement"]["stats"]["candidates"], 1)
        self.assertEqual(res["enforcement"]["stats"]["attempted"], 0)
        self.assertEqual(res["enforcement"]["stats"]["skipped_no_token"], 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.resolve_github_app_operation_token", return_value="tok")
    @patch("analyzer.tasks.reviewer_attention.GitHubAssignmentClient")
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_enforcement_partial_failure_reports_unassigned_and_threshold_items(
        self,
        mock_zulip_client_cls,
        mock_build_reports,
        mock_assignment_client_cls,
        _mock_resolve_token,
    ) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=30)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=assigned_at,
                        days_on_queue_since_assignment=30,
                        total_queue_seconds=30 * 24 * 60 * 60,
                        total_queue_days=30,
                        needs_auto_unassign=True,
                    ),
                    ReviewerAttentionItem(
                        pr_number=102,
                        pr_title="PR 102",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=assigned_at,
                        days_on_queue_since_assignment=30,
                        total_queue_seconds=30 * 24 * 60 * 60,
                        total_queue_days=30,
                        needs_auto_unassign=True,
                    ),
                ),
                warnings=(),
            )
        ]

        mock_assignment_client = mock_assignment_client_cls.return_value
        mock_assignment_client.unassign.side_effect = [
            (),
            AssignmentMutationError(code="github_transient", message="temporary"),
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["enforcement"]["stats"]["candidates"], 2)
        self.assertEqual(res["enforcement"]["stats"]["attempted"], 2)
        self.assertEqual(res["enforcement"]["stats"]["unassigned"], 1)
        self.assertEqual(res["enforcement"]["stats"]["failed"], 1)
        self.assertEqual(mock_assignment_client.unassign.call_count, 2)
        kwargs = mock_zulip_client_cls.return_value.send_direct_message.call_args.kwargs
        self.assertIn("Auto-unassigned in this run (1)", kwargs["content"])
        self.assertIn("At auto-unassign threshold (1)", kwargs["content"])

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=False,
    )
    @patch("analyzer.tasks.reviewer_attention.resolve_github_app_operation_token", return_value="tok")
    @patch("analyzer.tasks.reviewer_attention.GitHubAssignmentClient")
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_enforcement_deduplicates_duplicate_candidate_rows(
        self,
        mock_build_reports,
        mock_assignment_client_cls,
        _mock_resolve_token,
    ) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=30)
        duplicate_item = ReviewerAttentionItem(
            pr_number=101,
            pr_title="PR 101",
            is_on_queue=True,
            last_assigned_at=assigned_at,
            queue_anchor_at=assigned_at,
            days_on_queue_since_assignment=30,
            total_queue_seconds=30 * 24 * 60 * 60,
            total_queue_days=30,
            needs_auto_unassign=True,
        )
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(duplicate_item, duplicate_item),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["enforcement"]["stats"]["candidates"], 1)
        self.assertEqual(res["enforcement"]["stats"]["attempted"], 1)
        self.assertEqual(mock_assignment_client_cls.return_value.unassign.call_count, 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=False,
    )
    @patch("analyzer.tasks.reviewer_attention.resolve_github_app_operation_token", return_value="tok")
    @patch("analyzer.tasks.reviewer_attention.GitHubAssignmentClient")
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_runtime_override_enables_enforcement_when_global_flags_disabled(
        self,
        mock_build_reports,
        mock_assignment_client_cls,
        _mock_resolve_token,
    ) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=30)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=False,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=assigned_at,
                        days_on_queue_since_assignment=30,
                        total_queue_seconds=30 * 24 * 60 * 60,
                        total_queue_days=30,
                        needs_auto_unassign=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply(
            kwargs={
                "reports_enabled_override": True,
                "enforcement_enabled_override": True,
            }
        ).get()

        self.assertFalse(res["skipped"])
        self.assertTrue(res["reports_enabled"])
        self.assertTrue(res["enforcement_enabled"])
        self.assertEqual(res["enforcement"]["stats"]["attempted"], 1)
        self.assertEqual(mock_assignment_client_cls.return_value.unassign.call_count, 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_reviewer_filter_limits_message_recipients(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(hours=6)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=None,
                        days_on_queue_since_assignment=1,
                        total_queue_seconds=1 * 24 * 60 * 60,
                        total_queue_days=1,
                        needs_new_assignment_ping=True,
                    ),
                ),
                warnings=(),
            ),
            ReviewerAttentionReport(
                reviewer_login="bob",
                reviewer_user_id=self.other_user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=14,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=102,
                        pr_title="PR 102",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=None,
                        days_on_queue_since_assignment=1,
                        total_queue_seconds=1 * 24 * 60 * 60,
                        total_queue_days=1,
                        needs_new_assignment_ping=True,
                    ),
                ),
                warnings=(),
            ),
        ]

        res = reviewer_attention_daily_task.apply(
            kwargs={
                "delivery_reviewer_user_ids": [self.user.id],
            }
        ).get()

        self.assertEqual(res["delivery"]["stats"]["attempted"], 1)
        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        self.assertEqual(res["delivery"]["stats"]["skipped_by_reviewer_filter"], 1)
        self.assertEqual(mock_client_cls.return_value.send_direct_message.call_count, 1)
        kwargs = mock_client_cls.return_value.send_direct_message.call_args.kwargs
        self.assertEqual(kwargs["to"], [self.user.zulip_user_id])
