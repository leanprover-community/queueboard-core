from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from analyzer.models import AssignmentProposal
from analyzer.tasks.assignment_proposal_delivery import deliver_assignment_proposals_task
from core.models import Repository, User


@override_settings(QUEUEBOARD_BASE_URL="https://queue.example.org")
class DeliverAssignmentProposalsTaskTests(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")
        self.now = timezone.now()

    def _seed(self) -> None:
        User.objects.create(github_login="bob", zulip_user_id=4242)
        AssignmentProposal.objects.create(
            repository=self.repo,
            pr_number=101,
            reviewer_login="bob",
            state=AssignmentProposal.STATE_PROPOSED,
            expires_at=self.now + timedelta(days=7),
        )

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=False)
    def test_skips_when_disabled(self) -> None:
        res = deliver_assignment_proposals_task.apply().get()
        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "feature_disabled")

    @override_settings(
        ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True,
        ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED=False,
        ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=False,
    )
    def test_master_on_but_delivery_off_is_disabled(self) -> None:
        # Delivery requires BOTH the master and the delivery flag.
        self._seed()
        res = deliver_assignment_proposals_task.apply().get()
        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "feature_disabled")
        self.assertTrue(AssignmentProposal.objects.filter(pr_number=101, notified_at__isnull=True).exists())

    @override_settings(
        ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=True,
        ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED=True,
        ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=False,
    )
    def test_enabled_sends_and_stamps(self) -> None:
        self._seed()
        client = MagicMock()
        with patch("analyzer.tasks.assignment_proposal_delivery.ZulipClient", return_value=client):
            res = deliver_assignment_proposals_task.apply().get()

        self.assertFalse(res["skipped"])
        self.assertTrue(res["enabled"])
        self.assertEqual(res["totals"]["sent"], 1)
        client.send_direct_message.assert_called_once()
        self.assertTrue(AssignmentProposal.objects.filter(pr_number=101, notified_at__isnull=False).exists())

    @override_settings(
        ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False,
        ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED=False,
        ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=True,
    )
    def test_dry_run_computes_without_client(self) -> None:
        self._seed()
        # No ZulipClient is constructed in dry-run; a mock guards against accidental construction.
        with patch("analyzer.tasks.assignment_proposal_delivery.ZulipClient") as client_cls:
            res = deliver_assignment_proposals_task.apply().get()

        client_cls.assert_not_called()
        self.assertFalse(res["skipped"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["totals"]["would_send"], 1)
        self.assertTrue(AssignmentProposal.objects.filter(pr_number=101, notified_at__isnull=True).exists())

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=True)
    def test_repo_filter_miss_returns_not_found(self) -> None:
        res = deliver_assignment_proposals_task.apply(kwargs={"repository_id": 999999}).get()
        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "repo_not_found_or_inactive")

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False, ANALYZER_ASSIGNMENT_PROPOSALS_DRY_RUN=True)
    def test_command_honors_dry_run_when_run_bare(self) -> None:
        self._seed()
        with patch("analyzer.tasks.assignment_proposal_delivery.ZulipClient") as client_cls:
            call_command("deliver_assignment_proposals", stdout=StringIO())
        client_cls.assert_not_called()
        self.assertTrue(AssignmentProposal.objects.filter(pr_number=101, notified_at__isnull=True).exists())

    @override_settings(ANALYZER_ASSIGNMENT_PROPOSALS_ENABLED=False, ANALYZER_ASSIGNMENT_PROPOSALS_DELIVERY_ENABLED=False)
    def test_command_enable_forces_send(self) -> None:
        self._seed()
        client = MagicMock()
        with patch("analyzer.tasks.assignment_proposal_delivery.ZulipClient", return_value=client):
            call_command("deliver_assignment_proposals", "--enable", stdout=StringIO())
        client.send_direct_message.assert_called_once()
        self.assertTrue(AssignmentProposal.objects.filter(pr_number=101, notified_at__isnull=False).exists())
