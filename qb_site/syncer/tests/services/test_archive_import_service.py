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

    def test_status_context_gh_created_at_synthesized_from_committed_date(self) -> None:
        # Legacy fragment lacks createdAt for StatusContext entries. The
        # service synthesizes from commit.committedDate (preferred over
        # archive_timestamp, which is too late for orphan-SHA recovery).
        committed_at = datetime(2024, 2, 14, 9, 30, 0, tzinfo=_tz.utc)
        payload = _archive_payload(
            number=104,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-A",
                        "committedDate": committed_at.isoformat(),
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
        self.assertEqual(row.gh_created_at, committed_at)
        self.assertIsNotNone(row.archive_imported_at)

    def test_check_run_timestamps_synthesized_from_committed_date(self) -> None:
        # Legacy fragment lacks startedAt/completedAt for CheckRun entries.
        # Without synthesis, the analyzer's filter would silently exclude
        # the row from CI evaluation.
        committed_at = datetime(2024, 3, 10, 8, 0, 0, tzinfo=_tz.utc)
        payload = _archive_payload(
            number=110,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-B",
                        "committedDate": committed_at.isoformat(),
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR-B",
                                        "name": "lint",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        # No startedAt / completedAt.
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
        row = CommitCheckRun.objects.get(github_node_id="CR-B")
        self.assertEqual(row.gh_started_at, committed_at)
        self.assertEqual(row.gh_completed_at, committed_at)

    def test_committed_date_preferred_over_archive_timestamp(self) -> None:
        # When both committedDate and archive_timestamp are present,
        # committedDate wins (it's the more accurate source).
        committed_at = datetime(2024, 1, 5, tzinfo=_tz.utc)
        payload = _archive_payload(
            number=111,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-C",
                        "committedDate": committed_at.isoformat(),
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "StatusContext",
                                        "id": "SC-C",
                                        "context": "legacy/ci",
                                        "state": "SUCCESS",
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
        row = CommitStatusContext.objects.get(github_node_id="SC-C")
        self.assertEqual(row.gh_created_at, committed_at)
        self.assertNotEqual(row.gh_created_at, self.archive_ts)

    def test_falls_back_to_archive_timestamp_when_committed_date_missing(self) -> None:
        # Defensive: if committedDate is absent (malformed payload), the
        # importer must still produce a non-null gh_created_at so the
        # StatusContext NOT NULL constraint doesn't trip.
        payload = _archive_payload(
            number=112,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-D",
                        # No committedDate.
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR-D",
                                        "name": "lint",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                    },
                                    {
                                        "__typename": "StatusContext",
                                        "id": "SC-D",
                                        "context": "legacy/ci",
                                        "state": "SUCCESS",
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
        cr = CommitCheckRun.objects.get(github_node_id="CR-D")
        sc = CommitStatusContext.objects.get(github_node_id="SC-D")
        self.assertEqual(cr.gh_completed_at, self.archive_ts)
        self.assertEqual(cr.gh_started_at, self.archive_ts)
        self.assertEqual(sc.gh_created_at, self.archive_ts)

    def test_explicit_timestamps_in_payload_are_preserved(self) -> None:
        # If the payload happens to carry startedAt/completedAt (unusual for
        # the legacy fragment but possible for newer captures), the importer
        # must NOT overwrite them with the synthesized value.
        committed_at = datetime(2024, 4, 1, tzinfo=_tz.utc)
        real_started = datetime(2024, 4, 1, 0, 0, 5, tzinfo=_tz.utc)
        real_completed = datetime(2024, 4, 1, 0, 1, 30, tzinfo=_tz.utc)
        payload = _archive_payload(
            number=113,
            commits=[
                {
                    "commit": {
                        "oid": "commit-sha-E",
                        "committedDate": committed_at.isoformat(),
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "id": "CR-E",
                                        "name": "build",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "startedAt": real_started.isoformat(),
                                        "completedAt": real_completed.isoformat(),
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
        row = CommitCheckRun.objects.get(github_node_id="CR-E")
        self.assertEqual(row.gh_started_at, real_started)
        self.assertEqual(row.gh_completed_at, real_completed)

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
