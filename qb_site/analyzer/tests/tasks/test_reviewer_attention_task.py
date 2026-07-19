from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import (
    AssignmentProposal,
    QueueRuleSet,
    QueueSnapshot,
    ReviewerAttentionAutoUnassignRecord,
    ReviewerAttentionNotificationRecord,
)
from analyzer.services.reviewer_attention import ReviewerAttentionItem, ReviewerAttentionReport, ReviewerProposalItem
from analyzer.tasks.reviewer_attention import reviewer_attention_daily_task
from core.models import Repository, ReviewerPreference, User
from core.services.github_assignment import AssignmentMutationError
from zulip_bot.services.zulip_client import ZulipApiError


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
        ANALYZER_REVIEWER_ATTENTION_POLICY_START_AT="2026-02-20",
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_parses_policy_start_at_setting_and_passes_to_service(self, mock_build_reports) -> None:
        mock_build_reports.return_value = []

        res = reviewer_attention_daily_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertEqual(res["policy_start_at"], "2026-02-20T00:00:00+00:00")
        kwargs = mock_build_reports.call_args.kwargs
        self.assertEqual(str(kwargs["policy_start_at"].isoformat()), "2026-02-20T00:00:00+00:00")

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
        self.assertIn("Queue PRs that may need your attention", kwargs["content"])
        self.assertIn("Settings:", kwargs["content"])
        self.assertIn("#### Newly assigned (1)", kwargs["content"])
        self.assertIn("#### Queue attention (1)", kwargs["content"])
        self.assertIn("Consecutive time on queue since latest assignment: 16d", kwargs["content"])
        self.assertIn("Total queue time: 16d", kwargs["content"])
        self.assertIn("since <time:", kwargs["content"])
        self.assertIn("`unassign #<number>`", kwargs["content"])
        self.assertIn("`assigned-prs`", kwargs["content"])
        self.assertIn("`prefs`", kwargs["content"])

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_message_includes_load_line_from_snapshot(self, mock_client_cls, mock_build_reports) -> None:
        rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=False,
            required_label_names=[],
            forbidden_label_names=[],
            is_active=True,
        )
        ReviewerPreference.objects.create(user=self.user, repository=self.repo, maximum_capacity=10)
        QueueSnapshot.objects.create(
            repository=self.repo,
            cache_key=str(rules.id),
            generated_at=datetime.now(dt_timezone.utc),
            payload={"prs": {"77": {"assignees": ["alice"], "author": "bob", "pr_status": "AwaitingReview"}}},
            etag="etag",
            pr_count=1,
            queue_count=0,
        )
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
                        needs_nudge=False,
                        needs_auto_unassign=False,
                        missing_assignment_timestamp=False,
                    ),
                ),
                warnings=(),
            )
        ]
        mock_client = mock_client_cls.return_value

        reviewer_attention_daily_task.apply().get()

        content = mock_client.send_direct_message.call_args.kwargs["content"]
        self.assertIn("Load: 1 / 10 (9 free) · 1 assigned", content)

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
        self.assertIn("#### Auto-unassigned in this run (1)", kwargs["content"])
        self.assertIn("#### At auto-unassign threshold (1)", kwargs["content"])
        self.assertIn("At least 21 consecutive days on queue since assignment.", kwargs["content"])
        self.assertIn("Consecutive time on queue since latest assignment: 30d", kwargs["content"])
        self.assertIn("Total queue time: 30d", kwargs["content"])

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

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_deduplicates_notifications_across_retry_runs(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(hours=3)
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
                        days_on_queue_since_assignment=1,
                        total_queue_seconds=1 * 24 * 60 * 60,
                        total_queue_days=1,
                        needs_new_assignment_ping=True,
                    ),
                ),
                warnings=(),
            )
        ]

        first = reviewer_attention_daily_task.apply().get()
        second = reviewer_attention_daily_task.apply().get()

        self.assertEqual(mock_client_cls.return_value.send_direct_message.call_count, 1)
        self.assertEqual(first["delivery"]["stats"]["sent"], 1)
        self.assertEqual(second["delivery"]["stats"]["skipped_already_sent"], 1)
        self.assertEqual(
            ReviewerAttentionNotificationRecord.objects.filter(
                run_date=first["run_date"],
                repository=self.repo,
                reviewer=self.user,
                pr_number=101,
                category=ReviewerAttentionNotificationRecord.CATEGORY_NEW_ASSIGNMENT,
                status=ReviewerAttentionNotificationRecord.STATUS_SENT,
            ).count(),
            1,
        )

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_retries_failed_notification_record(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(hours=3)
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
                        days_on_queue_since_assignment=1,
                        total_queue_seconds=1 * 24 * 60 * 60,
                        total_queue_days=1,
                        needs_new_assignment_ping=True,
                    ),
                ),
                warnings=(),
            )
        ]

        mock_client = mock_client_cls.return_value
        mock_client.send_direct_message.side_effect = [ZulipApiError("temporary"), {"result": "success"}]

        first = reviewer_attention_daily_task.apply().get()
        second = reviewer_attention_daily_task.apply().get()

        self.assertEqual(first["delivery"]["stats"]["failed"], 1)
        self.assertEqual(second["delivery"]["stats"]["sent"], 1)
        self.assertEqual(mock_client.send_direct_message.call_count, 2)
        record = ReviewerAttentionNotificationRecord.objects.get(
            run_date=first["run_date"],
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NEW_ASSIGNMENT,
        )
        self.assertEqual(record.status, ReviewerAttentionNotificationRecord.STATUS_SENT)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_dedupe_is_once_per_queue_window_across_days(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=10)
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=7,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=assigned_at,
                        days_on_queue_since_assignment=10,
                        total_queue_seconds=10 * 24 * 60 * 60,
                        total_queue_days=10,
                        needs_nudge=True,
                    ),
                ),
                warnings=(),
            )
        ]
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timedelta(days=1)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
            cycle_anchor_at=assigned_at,
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 0)
        self.assertEqual(res["delivery"]["stats"]["skipped_already_sent"], 1)
        mock_client_cls.return_value.send_direct_message.assert_not_called()

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=False,
    )
    @patch("analyzer.tasks.reviewer_attention.resolve_github_app_operation_token", return_value="tok")
    @patch("analyzer.tasks.reviewer_attention.GitHubAssignmentClient")
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    def test_enforcement_deduplicates_auto_unassign_across_retry_runs(
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

        first = reviewer_attention_daily_task.apply().get()
        second = reviewer_attention_daily_task.apply().get()

        self.assertEqual(mock_assignment_client_cls.return_value.unassign.call_count, 1)
        self.assertEqual(first["enforcement"]["stats"]["unassigned"], 1)
        self.assertEqual(second["enforcement"]["stats"]["skipped_already_recorded"], 1)
        record = ReviewerAttentionAutoUnassignRecord.objects.get(
            run_date=first["run_date"],
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
        )
        self.assertEqual(record.status, ReviewerAttentionAutoUnassignRecord.STATUS_UNASSIGNED)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_sends_again_when_reassigned_with_new_assignment_anchor(self, mock_client_cls, mock_build_reports) -> None:
        old_assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=6)
        new_assigned_at = datetime.now(dt_timezone.utc) - timedelta(hours=2)
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timedelta(days=1)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NEW_ASSIGNMENT,
            cycle_anchor_at=old_assigned_at,
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )
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
                        last_assigned_at=new_assigned_at,
                        queue_anchor_at=new_assigned_at,
                        days_on_queue_since_assignment=0,
                        total_queue_seconds=0,
                        total_queue_days=0,
                        needs_new_assignment_ping=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        self.assertEqual(mock_client_cls.return_value.send_direct_message.call_count, 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_sends_again_when_pr_reenters_queue_with_new_queue_anchor(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=20)
        old_queue_anchor = datetime.now(dt_timezone.utc) - timedelta(days=12)
        new_queue_anchor = datetime.now(dt_timezone.utc) - timedelta(days=8)
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timedelta(days=3)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
            cycle_anchor_at=old_queue_anchor,
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=7,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=new_queue_anchor,
                        days_on_queue_since_assignment=8,
                        total_queue_seconds=8 * 24 * 60 * 60,
                        total_queue_days=8,
                        needs_nudge=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        self.assertEqual(mock_client_cls.return_value.send_direct_message.call_count, 1)

    def _make_pending_proposal(self, *, pr_number: int, reviewer_login: str = "alice") -> AssignmentProposal:
        return AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=pr_number,
            reviewer_login=reviewer_login,
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=timezone.now() + timedelta(days=5),
        )

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
        QUEUEBOARD_BASE_URL="https://queueboard.example.org",
    )
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_sends_proposal_section_stamps_notified_and_dedupes(self, mock_client_cls) -> None:
        # End-to-end through the real report builder: a muted reviewer with a pending proposal
        # still gets the transactional proposal DM (no nudge machinery involved), the proposal is
        # stamped notified, and the next run has nothing new to send.
        ReviewerPreference.objects.create(user=self.user, repository=self.repo, notifications_enabled=False)
        proposal = self._make_pending_proposal(pr_number=555, reviewer_login="Alice")

        first = reviewer_attention_daily_task.apply().get()
        second = reviewer_attention_daily_task.apply().get()

        self.assertEqual(first["totals"]["pending_proposals"], 1)
        self.assertEqual(first["totals"]["unnotified_proposals"], 1)
        self.assertEqual(first["delivery"]["stats"]["sent"], 1)
        self.assertEqual(first["delivery"]["stats"]["proposals_notified"], 1)
        self.assertEqual(mock_client_cls.return_value.send_direct_message.call_count, 1)
        content = mock_client_cls.return_value.send_direct_message.call_args.kwargs["content"]
        self.assertIn("#### Proposed to you, awaiting your response (1)", content)
        self.assertIn("https://queueboard.example.org/console/", content)
        self.assertIn("PR #555", content)
        self.assertIn("expires <time:", content)
        self.assertIn("Declining a proposal opts you out", content)
        proposal.refresh_from_db()
        self.assertIsNotNone(proposal.notified_at)
        # No optional nudge categories were claimed for the muted reviewer.
        self.assertEqual(ReviewerAttentionNotificationRecord.objects.count(), 0)
        self.assertEqual(second["delivery"]["stats"]["sent"], 0)
        self.assertEqual(second["delivery"]["stats"]["skipped_already_sent"], 1)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_failed_send_leaves_proposal_unstamped_for_retry(self, mock_client_cls) -> None:
        ReviewerPreference.objects.create(user=self.user, repository=self.repo, notifications_enabled=True)
        proposal = self._make_pending_proposal(pr_number=556)
        mock_client = mock_client_cls.return_value
        mock_client.send_direct_message.side_effect = [ZulipApiError("temporary"), {"result": "success"}]

        first = reviewer_attention_daily_task.apply().get()
        proposal.refresh_from_db()
        self.assertEqual(first["delivery"]["stats"]["failed"], 1)
        self.assertIsNone(proposal.notified_at)

        second = reviewer_attention_daily_task.apply().get()
        proposal.refresh_from_db()
        self.assertEqual(second["delivery"]["stats"]["sent"], 1)
        self.assertEqual(second["delivery"]["stats"]["proposals_notified"], 1)
        self.assertIsNotNone(proposal.notified_at)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_muted_reviewer_dm_carries_proposals_but_no_nudge_sections(self, mock_client_cls, mock_build_reports) -> None:
        proposal = self._make_pending_proposal(pr_number=557)
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
                        last_assigned_at=datetime.now(dt_timezone.utc) - timedelta(days=16),
                        queue_anchor_at=datetime.now(dt_timezone.utc) - timedelta(days=16),
                        days_on_queue_since_assignment=16,
                        total_queue_seconds=16 * 24 * 60 * 60,
                        total_queue_days=16,
                        needs_nudge=True,
                    ),
                ),
                warnings=(),
                proposal_items=(
                    ReviewerProposalItem(
                        proposal_id=proposal.id,
                        pr_number=557,
                        pr_title="PR 557",
                        expires_at=proposal.expires_at,
                        notified=False,
                    ),
                ),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        content = mock_client_cls.return_value.send_direct_message.call_args.kwargs["content"]
        self.assertIn("#### Proposed to you, awaiting your response (1)", content)
        self.assertNotIn("#### Queue attention", content)
        self.assertNotIn("PR #101", content)
        # Muted -> the optional nudge was neither claimed nor recorded.
        self.assertEqual(ReviewerAttentionNotificationRecord.objects.count(), 0)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_already_notified_proposal_rides_along_with_triggered_nudge(self, mock_client_cls, mock_build_reports) -> None:
        proposal = self._make_pending_proposal(pr_number=558)
        AssignmentProposal.objects.filter(id=proposal.id).update(notified_at=timezone.now() - timedelta(days=1))
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
                        last_assigned_at=datetime.now(dt_timezone.utc) - timedelta(days=16),
                        queue_anchor_at=datetime.now(dt_timezone.utc) - timedelta(days=16),
                        days_on_queue_since_assignment=16,
                        total_queue_seconds=16 * 24 * 60 * 60,
                        total_queue_days=16,
                        needs_nudge=True,
                    ),
                ),
                warnings=(),
                proposal_items=(
                    ReviewerProposalItem(
                        proposal_id=proposal.id,
                        pr_number=558,
                        pr_title="PR 558",
                        expires_at=proposal.expires_at,
                        notified=True,
                    ),
                ),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        self.assertEqual(res["delivery"]["stats"]["proposals_notified"], 0)
        content = mock_client_cls.return_value.send_direct_message.call_args.kwargs["content"]
        self.assertIn("#### Queue attention (1)", content)
        self.assertIn("#### Proposed to you, awaiting your response (1)", content)

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_does_not_resend_nudge_in_same_queue_window_after_threshold_change(
        self, mock_client_cls, mock_build_reports
    ) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=20)
        queue_anchor = datetime.now(dt_timezone.utc) - timedelta(days=12)
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timedelta(days=2)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NUDGE,
            cycle_anchor_at=queue_anchor,
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=10,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=queue_anchor,
                        days_on_queue_since_assignment=12,
                        total_queue_seconds=12 * 24 * 60 * 60,
                        total_queue_days=12,
                        needs_nudge=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 0)
        self.assertEqual(res["delivery"]["stats"]["skipped_already_sent"], 1)
        mock_client_cls.return_value.send_direct_message.assert_not_called()

    @override_settings(
        ANALYZER_REVIEWER_ATTENTION_ENABLED=True,
        ANALYZER_REVIEWER_ATTENTION_ENFORCEMENT_ENABLED=False,
        ANALYZER_REVIEWER_ATTENTION_DELIVERY_ENABLED=True,
    )
    @patch("analyzer.tasks.reviewer_attention.build_reviewer_attention_reports")
    @patch("analyzer.tasks.reviewer_attention.ZulipClient")
    def test_delivery_sends_new_category_in_same_queue_window(self, mock_client_cls, mock_build_reports) -> None:
        assigned_at = datetime.now(dt_timezone.utc) - timedelta(days=2)
        queue_anchor = assigned_at
        ReviewerAttentionNotificationRecord.objects.create(
            run_date=(timezone.now() - timedelta(days=1)).date(),
            repository=self.repo,
            reviewer=self.user,
            pr_number=101,
            category=ReviewerAttentionNotificationRecord.CATEGORY_NEW_ASSIGNMENT,
            cycle_anchor_at=queue_anchor,
            status=ReviewerAttentionNotificationRecord.STATUS_SENT,
        )
        mock_build_reports.return_value = [
            ReviewerAttentionReport(
                reviewer_login="alice",
                reviewer_user_id=self.user.id,
                repository_id=self.repo.id,
                notifications_enabled=True,
                stale_nudge_days=1,
                auto_unassign_days=21,
                items=(
                    ReviewerAttentionItem(
                        pr_number=101,
                        pr_title="PR 101",
                        is_on_queue=True,
                        last_assigned_at=assigned_at,
                        queue_anchor_at=queue_anchor,
                        days_on_queue_since_assignment=2,
                        total_queue_seconds=2 * 24 * 60 * 60,
                        total_queue_days=2,
                        needs_nudge=True,
                    ),
                ),
                warnings=(),
            )
        ]

        res = reviewer_attention_daily_task.apply().get()

        self.assertEqual(res["delivery"]["stats"]["sent"], 1)
        self.assertEqual(mock_client_cls.return_value.send_direct_message.call_count, 1)
