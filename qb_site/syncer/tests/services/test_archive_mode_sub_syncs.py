"""Sub-sync edits supporting archive-mode ingest (design doc 043 Commit 3)."""

from __future__ import annotations

from datetime import datetime, timezone as _tz

from django.test import TestCase

from syncer.models import (
    CommitCheckRun,
    CommitStatusContext,
    LabelDef,
    PRLabel,
    PRTimelineEvent,
    PRTimelineEventType,
    PullRequest,
)
from syncer.services.sub.ci_sync import sync_check_runs, sync_status_contexts
from syncer.services.sub.labels_sync import sync_pr_labels
from syncer.services.sub.pull_request_sync import upsert_pull_request
from syncer.services.sub.timeline_sync import sync_timeline_events
from syncer.tests.factories import make_pr, make_repo


def _bundle(**overrides):
    base = {
        "number": 1,
        "state": "OPEN",
        "isDraft": False,
        "title": "T",
        "body": "B",
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-01T01:00:00Z",
        "baseRefName": "master",
        "headRefName": "branch",
        "headRefOid": "live-sha",
        "headRepositoryOwner": {"login": "o"},
        "headRepository": {"name": "fork"},
        "additions": 0,
        "deletions": 0,
        "changedFiles": 0,
        "author": {"login": "alice"},
    }
    base.update(overrides)
    return base


class TestUpsertPullRequestArchiveParams(TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_skip_watermark_leaves_last_synced_at_null_on_create(self) -> None:
        res = upsert_pull_request(_bundle(number=1), self.repo, skip_watermark=True)
        self.assertTrue(res.created)
        pr = PullRequest.objects.get(repository=self.repo, number=1)
        self.assertIsNone(pr.last_synced_at)

    def test_skip_watermark_default_false_advances_on_create(self) -> None:
        res = upsert_pull_request(_bundle(number=2), self.repo)
        self.assertTrue(res.created)
        pr = PullRequest.objects.get(repository=self.repo, number=2)
        self.assertIsNotNone(pr.last_synced_at)

    def test_if_newer_than_gates_state_title_body_head_sha_when_existing_is_newer(self) -> None:
        # Live sync first: pr.gh_updated_at = 2025-06-01.
        upsert_pull_request(
            _bundle(
                number=3,
                title="Live title",
                body="Live body",
                state="OPEN",
                isDraft=False,
                headRefOid="live-sha",
                updatedAt="2025-06-01T00:00:00Z",
                closedAt=None,
                mergedAt=None,
            ),
            self.repo,
        )

        # Archive call with older snapshot: 2025-01-01.
        archive_updated_at = datetime(2025, 1, 1, tzinfo=_tz.utc)
        archive_bundle = _bundle(
            number=3,
            title="Archive title",
            body="Archive body",
            state="CLOSED",
            isDraft=True,
            headRefOid="archive-sha",
            updatedAt="2025-01-01T00:00:00Z",
            closedAt="2025-01-02T00:00:00Z",
            mergedAt=None,
            additions=42,
            deletions=7,
            changedFiles=3,
        )
        res = upsert_pull_request(archive_bundle, self.repo, if_newer_than=archive_updated_at)
        self.assertFalse(res.created)
        pr = PullRequest.objects.get(repository=self.repo, number=3)

        # Gated fields preserved from live.
        self.assertEqual(pr.title, "Live title")
        self.assertEqual(pr.body, "Live body")
        self.assertEqual(pr.state, "open")
        self.assertFalse(pr.is_draft)
        self.assertEqual(pr.head_sha, "live-sha")
        self.assertIsNone(pr.closed_at)

        # Non-gated fields (additions etc.) flow through.
        self.assertEqual(pr.additions, 42)
        self.assertEqual(pr.deletions, 7)
        self.assertEqual(pr.changed_files_count, 3)

    def test_if_newer_than_does_not_gate_when_archive_is_newer(self) -> None:
        upsert_pull_request(_bundle(number=4, title="Old", updatedAt="2025-01-01T00:00:00Z"), self.repo)
        future = datetime(2026, 1, 1, tzinfo=_tz.utc)
        upsert_pull_request(
            _bundle(number=4, title="New", updatedAt="2026-01-01T00:00:00Z"),
            self.repo,
            if_newer_than=future,
        )
        pr = PullRequest.objects.get(repository=self.repo, number=4)
        self.assertEqual(pr.title, "New")


class TestSyncPRLabelsAdditiveOnly(TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 10)
        self.label_a = LabelDef.objects.create(repository=self.repo, name="bug", color="ff0000")
        self.label_b = LabelDef.objects.create(repository=self.repo, name="WIP", color="ffff00")
        self.label_c = LabelDef.objects.create(repository=self.repo, name="enhancement", color="00ff00")

    def test_additive_only_does_not_detach_live_labels(self) -> None:
        # Live row already has bug + WIP attached.
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label_a)
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label_b)

        # Archive snapshot only mentions "enhancement".
        sync_pr_labels(self.pr, ["enhancement"], additive_only=True)

        attached = set(PRLabel.objects.filter(pull_request=self.pr).values_list("label_def__name", flat=True))
        self.assertEqual(attached, {"bug", "WIP", "enhancement"})

    def test_additive_only_drops_unknown_label_names_silently(self) -> None:
        # Archive references a label whose LabelDef does not exist for this repo.
        sync_pr_labels(self.pr, ["does-not-exist", "bug"], additive_only=True)
        # Only the existing LabelDef matched.
        attached = list(PRLabel.objects.filter(pull_request=self.pr).values_list("label_def__name", flat=True))
        self.assertEqual(attached, ["bug"])
        # Did not create a new LabelDef.
        self.assertFalse(LabelDef.objects.filter(repository=self.repo, name="does-not-exist").exists())

    def test_default_mode_still_detaches(self) -> None:
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label_a)
        sync_pr_labels(self.pr, ["enhancement"])  # additive_only defaults to False
        attached = set(PRLabel.objects.filter(pull_request=self.pr).values_list("label_def__name", flat=True))
        self.assertEqual(attached, {"enhancement"})


class TestSyncTimelineEventsArchiveMode(TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 11)

    def test_legacy_review_dismissed_without_previous_state_skips_synthesis(self) -> None:
        events = [
            {
                "__typename": "ReviewDismissedEvent",
                "id": "DISMISS-1",
                "createdAt": "2025-04-01T00:00:00Z",
                # Legacy fragment: no previousReviewState.
                "review": {"author": {"login": "reviewer"}},
            }
        ]
        res = sync_timeline_events(self.pr, events, archive_mode=True)
        self.assertEqual(res.created, 1)  # The dismiss row itself; no synthesized parent.
        types = list(PRTimelineEvent.objects.filter(pull_request=self.pr).values_list("type", flat=True))
        self.assertEqual(types, [PRTimelineEventType.REVIEW_DISMISSED])

    def test_force_push_event_without_shas_is_dropped(self) -> None:
        events = [
            {
                "__typename": "HeadRefForcePushedEvent",
                "id": "FP-1",
                "createdAt": "2025-04-01T00:00:00Z",
                # Legacy fragment lacks beforeCommit/afterCommit.
            }
        ]
        res = sync_timeline_events(self.pr, events, archive_mode=True)
        self.assertEqual(res.created, 0)
        self.assertEqual(PRTimelineEvent.objects.filter(pull_request=self.pr).count(), 0)

    def test_legacy_pull_request_review_node_without_state_is_silently_dropped(self) -> None:
        events = [
            {
                "__typename": "PullRequestReview",
                "id": "REVIEW-1",
                "createdAt": "2025-04-01T00:00:00Z",
                # No state, no submittedAt.
                "author": {"login": "reviewer"},
            }
        ]
        res = sync_timeline_events(self.pr, events, archive_mode=True)
        self.assertEqual(res.created, 0)

    def test_issue_comment_and_assigned_event_still_ingest(self) -> None:
        events = [
            {
                "__typename": "IssueComment",
                "id": "IC-1",
                "createdAt": "2025-04-01T00:00:00Z",
                "author": {"login": "alice"},
            },
            {
                "__typename": "AssignedEvent",
                "id": "AE-1",
                "createdAt": "2025-04-02T00:00:00Z",
                "assignee": {"login": "bob"},
            },
        ]
        res = sync_timeline_events(self.pr, events, archive_mode=True)
        self.assertEqual(res.created, 2)


class TestSyncCheckRunsArchiveMode(TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 12)

    def test_archive_mode_create_inserts_with_null_legacy_fields(self) -> None:
        # Legacy CheckRun: only id, name, status, conclusion, detailsUrl.
        ctx = {
            "id": "CR-1",
            "name": "lint",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://x/y",
            # No externalId, startedAt, completedAt.
        }
        res = sync_check_runs(self.pr, [ctx], "sha-1", archive_mode=True)
        self.assertEqual(res.created, 1)
        row = CommitCheckRun.objects.get(github_node_id="CR-1")
        self.assertIsNone(row.external_id)
        self.assertIsNone(row.gh_started_at)
        self.assertIsNone(row.gh_completed_at)

    def test_archive_mode_update_does_not_downgrade_live_non_null_fields(self) -> None:
        # Live writes a complete row first.
        live_ctx = {
            "id": "CR-2",
            "name": "build",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://x/y",
            "externalId": "ext-123",
            "startedAt": "2025-05-01T00:00:00Z",
            "completedAt": "2025-05-01T00:05:00Z",
        }
        sync_check_runs(self.pr, [live_ctx], "sha-2")  # default mode
        row_before = CommitCheckRun.objects.get(github_node_id="CR-2")
        self.assertEqual(row_before.external_id, "ext-123")
        self.assertIsNotNone(row_before.gh_started_at)
        self.assertIsNotNone(row_before.gh_completed_at)

        # Archive arrives with the same node_id but no external_id / timestamps.
        archive_ctx = {
            "id": "CR-2",
            "name": "build",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": "https://x/y",
        }
        sync_check_runs(self.pr, [archive_ctx], "sha-2", archive_mode=True)
        row_after = CommitCheckRun.objects.get(github_node_id="CR-2")
        # Live's non-null values preserved.
        self.assertEqual(row_after.external_id, "ext-123")
        self.assertEqual(row_after.gh_started_at, row_before.gh_started_at)
        self.assertEqual(row_after.gh_completed_at, row_before.gh_completed_at)


class TestSyncStatusContextsArchiveMode(TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 13)

    def test_archive_mode_create_uses_supplied_gh_created_at(self) -> None:
        # Caller (archive_import service) is expected to pre-fill createdAt
        # with archive_timestamp when the legacy payload lacks it.
        synth_ts = "2024-01-15T12:34:56Z"
        ctx = {
            "id": "SC-1",
            "context": "ci/legacy",
            "state": "SUCCESS",
            "targetUrl": None,
            "description": None,
            "createdAt": synth_ts,
        }
        res = sync_status_contexts(self.pr, [ctx], "sha-3", archive_mode=True)
        self.assertEqual(res.created, 1)
        row = CommitStatusContext.objects.get(github_node_id="SC-1")
        self.assertEqual(row.gh_created_at, datetime(2024, 1, 15, 12, 34, 56, tzinfo=_tz.utc))
