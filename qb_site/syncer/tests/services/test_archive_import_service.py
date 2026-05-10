"""Tests for the archive backfill ingest service (design doc 043 Commit 3)."""

from __future__ import annotations

from datetime import datetime, timezone as _tz

from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRRevisionBuildState
from syncer.models import (
    CommitCheckRun,
    CommitStatusContext,
    LabelDef,
    PRLabel,
    PRTimelineEvent,
    PRTimelineEventType,
    PullRequest,
)
from syncer.services.archive_import import (
    ArchivePayloadError,
    import_pr_info_payload,
    unwrap_pr_info_payload,
)
from syncer.tests.factories import make_pr, make_repo


def _archive_payload(
    *,
    number: int = 100,
    title: str = "Archive title",
    body: str = "Archive body",
    state: str = "OPEN",
    is_draft: bool = False,
    head_sha: str = "archive-head",
    updated_at: str = "2024-01-01T12:00:00Z",
    label_names: list[str] | None = None,
    timeline_events: list | None = None,
    commits: list | None = None,
):
    return {
        "number": number,
        "state": state,
        "isDraft": is_draft,
        "title": title,
        "body": body,
        "createdAt": "2023-12-01T00:00:00Z",
        "updatedAt": updated_at,
        "baseRefName": "master",
        "headRefName": "topic",
        "headRefOid": head_sha,
        "headRepositoryOwner": {"login": "o"},
        "headRepository": {"name": "fork"},
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
        "author": {"login": "alice"},
        "labels": {"nodes": [{"name": n} for n in (label_names or [])]},
        "timelineItems": {"nodes": (timeline_events or [])},
        "commits": {"nodes": (commits or [])},
    }


class TestUnwrapPRInfoPayload(TestCase):
    def test_full_graphql_response_shape(self) -> None:
        wrapped = {"data": {"repository": {"pullRequest": {"number": 1}}}}
        self.assertEqual(unwrap_pr_info_payload(wrapped), {"number": 1})

    def test_data_only_shape(self) -> None:
        self.assertEqual(unwrap_pr_info_payload({"pullRequest": {"number": 2}}), {"number": 2})

    def test_unwrapped_node_shape(self) -> None:
        node = {"number": 3, "title": "x"}
        self.assertEqual(unwrap_pr_info_payload(node), node)

    def test_invalid_shape_raises(self) -> None:
        with self.assertRaises(ArchivePayloadError):
            unwrap_pr_info_payload({"foo": "bar"})


class TestImportPRInfoPayloadCore(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.archive_ts = datetime(2024, 6, 1, 10, 0, 0, tzinfo=_tz.utc)

    def test_creates_pr_without_advancing_last_synced_at(self) -> None:
        payload = _archive_payload(number=100)
        result = import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        self.assertTrue(result.pr_created)
        pr = PullRequest.objects.get(repository=self.repo, number=100)
        self.assertIsNone(pr.last_synced_at)
        self.assertIsNotNone(pr.archive_imported_at)

    def test_does_not_advance_sync_schema_version(self) -> None:
        payload = _archive_payload(number=101)
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        pr = PullRequest.objects.get(repository=self.repo, number=101)
        self.assertEqual(pr.sync_schema_version, 0)

    def test_attaches_existing_label_defs_only(self) -> None:
        LabelDef.objects.create(repository=self.repo, name="bug", color="ff0000")
        # 'unknown' has no LabelDef in the live repo; should be silently dropped.
        payload = _archive_payload(number=102, label_names=["bug", "unknown"])
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        pr = PullRequest.objects.get(repository=self.repo, number=102)
        attached = list(PRLabel.objects.filter(pull_request=pr).values_list("label_def__name", flat=True))
        self.assertEqual(attached, ["bug"])
        self.assertFalse(LabelDef.objects.filter(repository=self.repo, name="unknown").exists())

    def test_newer_wins_guard_preserves_live_pr_core(self) -> None:
        # Create live PR via the live upsert path (newer updatedAt).
        live_pr = make_pr(
            self.repo,
            number=103,
            title="Live title",
            body="Live body",
            state="open",
            is_draft=False,
            head_sha="live-sha",
        )
        live_pr.gh_updated_at = datetime(2025, 6, 1, tzinfo=_tz.utc)
        live_pr.save(update_fields=["gh_updated_at"])

        # Archive payload is older.
        payload = _archive_payload(
            number=103,
            title="Archive title",
            body="Archive body",
            state="CLOSED",
            is_draft=True,
            head_sha="archive-stale-sha",
            updated_at="2024-01-01T00:00:00Z",
        )
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        live_pr.refresh_from_db()
        self.assertEqual(live_pr.title, "Live title")
        self.assertEqual(live_pr.body, "Live body")
        self.assertEqual(live_pr.state, "open")
        self.assertFalse(live_pr.is_draft)
        # head_sha is critical — stamping archive's stale value would corrupt analyzer.
        self.assertEqual(live_pr.head_sha, "live-sha")

    def test_status_context_gh_created_at_synthesized_from_archive_timestamp(self) -> None:
        # Legacy fragment lacks createdAt for StatusContext entries.
        payload = _archive_payload(
            number=104,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-A",
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "StatusContext",
                                        "id": "SC-A",
                                        "context": "legacy/ci",
                                        "state": "SUCCESS",
                                        "targetUrl": None,
                                        "description": None,
                                        # No createdAt.
                                    }
                                ]
                            }
                        },
                    }
                }
            ],
        )
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        row = CommitStatusContext.objects.get(github_node_id="SC-A")
        self.assertEqual(row.gh_created_at, self.archive_ts)
        self.assertIsNotNone(row.archive_imported_at)

    def test_full_shape_creates_expected_rows_with_archive_imported_at(self) -> None:
        LabelDef.objects.create(repository=self.repo, name="bug", color="ff0000")
        payload = _archive_payload(
            number=105,
            label_names=["bug"],
            timeline_events=[
                {
                    "__typename": "IssueComment",
                    "id": "IC-X",
                    "createdAt": "2024-04-01T00:00:00Z",
                    "author": {"login": "bob"},
                },
                {
                    "__typename": "ReviewDismissedEvent",
                    "id": "DISMISS-X",
                    "createdAt": "2024-04-02T00:00:00Z",
                    "review": {"author": {"login": "carol"}},
                    # No previousReviewState — synthesis must be skipped.
                },
            ],
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-1",
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR-Y",
                                        "name": "lint",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "detailsUrl": "https://x/y",
                                    },
                                    {
                                        "__typename": "StatusContext",
                                        "id": "SC-Y",
                                        "context": "ci/legacy",
                                        "state": "SUCCESS",
                                        "targetUrl": None,
                                        "description": None,
                                    },
                                ]
                            }
                        },
                    }
                }
            ],
        )
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        pr = PullRequest.objects.get(repository=self.repo, number=105)
        self.assertIsNotNone(pr.archive_imported_at)

        # Timeline: one IssueComment + one ReviewDismissed (no synthesized parent).
        types = sorted(PRTimelineEvent.objects.filter(pull_request=pr).values_list("type", flat=True))
        self.assertEqual(
            types,
            sorted([PRTimelineEventType.ISSUE_COMMENTED, PRTimelineEventType.REVIEW_DISMISSED]),
        )
        # Both stamped with provenance.
        self.assertTrue(
            all(
                ts is not None
                for ts in PRTimelineEvent.objects.filter(pull_request=pr).values_list("archive_imported_at", flat=True)
            )
        )

        # CheckRun + StatusContext both stamped.
        cr = CommitCheckRun.objects.get(github_node_id="CR-Y")
        sc = CommitStatusContext.objects.get(github_node_id="SC-Y")
        self.assertIsNotNone(cr.archive_imported_at)
        self.assertIsNotNone(sc.archive_imported_at)
        self.assertEqual(sc.gh_created_at, self.archive_ts)

    def test_reimport_is_no_op_and_does_not_flap_archive_imported_at(self) -> None:
        payload = _archive_payload(
            number=106,
            timeline_events=[
                {
                    "__typename": "IssueComment",
                    "id": "IC-1",
                    "createdAt": "2024-04-01T00:00:00Z",
                    "author": {"login": "bob"},
                }
            ],
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-2",
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR-Z",
                                        "name": "lint",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "detailsUrl": None,
                                    }
                                ]
                            }
                        },
                    }
                }
            ],
        )
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        ev = PRTimelineEvent.objects.get(github_node_id="IC-1")
        cr = CommitCheckRun.objects.get(github_node_id="CR-Z")
        first_ev_stamp = ev.archive_imported_at
        first_cr_stamp = cr.archive_imported_at

        # Re-import.
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        # Row counts unchanged.
        self.assertEqual(PRTimelineEvent.objects.filter(github_node_id="IC-1").count(), 1)
        self.assertEqual(CommitCheckRun.objects.filter(github_node_id="CR-Z").count(), 1)
        # Provenance preserved (not flapped).
        ev.refresh_from_db()
        cr.refresh_from_db()
        self.assertEqual(ev.archive_imported_at, first_ev_stamp)
        self.assertEqual(cr.archive_imported_at, first_cr_stamp)

    def test_advances_latest_ci_synced_at_on_creates(self) -> None:
        payload = _archive_payload(
            number=107,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-3",
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR-Q",
                                        "name": "lint",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                    }
                                ]
                            }
                        },
                    }
                }
            ],
        )
        before = timezone.now()
        import_pr_info_payload(
            self.repo,
            payload,
            archive_name="queueboard-archive2",
            archive_timestamp=self.archive_ts,
        )
        pr = PullRequest.objects.get(repository=self.repo, number=107)
        state = PRRevisionBuildState.objects.get(pull_request=pr)
        self.assertIsNotNone(state.latest_ci_synced_at)
        self.assertGreaterEqual(state.latest_ci_synced_at, before)
