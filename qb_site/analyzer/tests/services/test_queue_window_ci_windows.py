from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from core.models import Repository
from analyzer.models import QueueRuleSet, PRQueueWindow, PRRevision
from syncer.models import PullRequest, CheckRun
from analyzer.services.queue_windows import rebuild_queue_windows_for_ruleset


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=dt_timezone.utc)


class TestQueueWindowCIWindows(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.rules = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            required_label_names=[],
            forbidden_label_names=[],
            required_ci_contexts=["lint"],
        )

    def _mk_pr(self, number: int) -> PullRequest:
        created = _dt(2024, 9, 1)
        updated = _dt(2024, 9, 2)
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=created,
            gh_updated_at=updated,
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="r",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
        )

    def _add_revision(self, pr: PullRequest, head_sha: str, from_ts: datetime, to_ts: datetime | None, seq: int) -> None:
        PRRevision.objects.create(
            pull_request=pr,
            head_sha=head_sha,
            from_ts=from_ts,
            to_ts=to_ts,
            seq=seq,
        )

    def test_ci_windows_open_and_close_on_success_and_failure(self) -> None:
        pr = self._mk_pr(1)
        # Single head SHA across the whole interval.
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), None, 0)

        # Failing CI first, then passing, then failing again.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_FAIL",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 2),
            gh_completed_at=_dt(2024, 9, 2),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=_dt(2024, 9, 4),
        )
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_FAIL_LATE",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="FAILURE",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 8),
            gh_completed_at=_dt(2024, 9, 8),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 10))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # CI should be ok only from Sep 4 (first SUCCESS) until Sep 8 (later FAILURE).
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 4))
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 8))

    def test_ci_windows_across_force_push_and_revisions(self) -> None:
        pr = self._mk_pr(2)
        # Head sha1 from Sep 1-6, then sha2 from Sep 6 onwards.
        self._add_revision(pr, "sha1", _dt(2024, 9, 1), _dt(2024, 9, 6), 0)
        self._add_revision(pr, "sha2", _dt(2024, 9, 6), None, 1)

        # CI success for sha1 at Sep 3.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_sha1_OK",
            head_sha="sha1",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 3),
            gh_completed_at=_dt(2024, 9, 3),
        )
        # No CI for sha2 until Sep 9.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_sha2_OK",
            head_sha="sha2",
            name="lint",
            status="COMPLETED",
            conclusion="SUCCESS",
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 9),
            gh_completed_at=_dt(2024, 9, 9),
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 12))
        self.assertEqual(res.deleted, 0)

        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules).order_by("from_ts"))
        # Expect:
        # - First window from Sep 3 (sha1 CI success) until Sep 6 (revision boundary).
        # - Second window from Sep 9 (sha2 CI success) until Sep 12 (as_of).
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].from_ts, _dt(2024, 9, 3))
        self.assertEqual(windows[0].to_ts, _dt(2024, 9, 6))
        self.assertEqual(windows[1].from_ts, _dt(2024, 9, 9))
        self.assertEqual(windows[1].to_ts, _dt(2024, 9, 12))

    def test_missing_or_pending_ci_yields_no_windows(self) -> None:
        pr = self._mk_pr(3)
        self._add_revision(pr, "shaP", _dt(2024, 9, 1), None, 0)

        # Only a pending run (status != COMPLETED) exists; no successful snapshot.
        CheckRun.objects.create(
            pull_request=pr,
            github_node_id="CR_PENDING",
            head_sha="shaP",
            name="lint",
            status="IN_PROGRESS",
            conclusion=None,
            details_url=None,
            external_id=None,
            gh_started_at=_dt(2024, 9, 4),
            gh_completed_at=None,
        )

        res = rebuild_queue_windows_for_ruleset(pr=pr, rule_set=self.rules, as_of=_dt(2024, 9, 10))
        self.assertEqual(res.deleted, 0)
        windows = list(PRQueueWindow.objects.filter(pull_request=pr, rule_set=self.rules))
        # With only missing/pending CI for the required context, we should not
        # persist any windows for this PR.
        self.assertEqual(len(windows), 0)
