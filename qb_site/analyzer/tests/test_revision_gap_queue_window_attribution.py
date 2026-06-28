"""End-to-end regression for design decision 049.

A coverage gap between two ``PRRevision`` windows makes the queue-window builder see a
"missing head" inside the gap, flip eligibility, and attribute the flip to CI with no FK
(``CI_PASSED``/``CI_FAILED`` + null CI FKs) — the shape that looped forever in the
staleness sweep. This test reproduces that shape from a gap, then verifies the revision
contiguity self-heal + a window rebuild removes it.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.test import TestCase
from django.utils import timezone

from analyzer.models import PRQueueWindow, PRRevision, PRRevisionBuildState, QueueRuleSet
from analyzer.models.queue_window import QueueWindowEventType
from analyzer.services.queue_windows import rebuild_queue_windows_for_pr
from analyzer.services.revisions import PR_REVISION_BUILDER_VERSION, rebuild_pr_revisions
from core.models import Repository
from syncer.models import CommitCheckRun, PullRequest, PRTimelineEvent, PRTimelineEventType


_CI = [QueueWindowEventType.CI_PASSED, QueueWindowEventType.CI_FAILED]


class TestRevisionGapQueueWindowAttribution(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.t0 = timezone.now() - timedelta(days=2)
        self.rule_set = QueueRuleSet.objects.create(
            repository=self.repo,
            version=1,
            require_open=True,
            require_not_draft=True,
            require_ci_success=True,
            ci_gating_mode=QueueRuleSet.CIGatingMode.ALL_REQUIRED_SUCCESS,
            required_ci_contexts=["ci"],
        )

    def _at(self, minutes: int):
        return self.t0 + timedelta(minutes=minutes)

    def _ci(self, head: str, completed_min: int) -> None:
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=f"CR_{head}_{completed_min}",
            head_sha=head,
            name="ci",
            status="COMPLETED",
            conclusion="SUCCESS",
            gh_started_at=self._at(completed_min - 5),
            gh_completed_at=self._at(completed_min),
        )

    def _null_fk_ci_windows(self, pr: PullRequest) -> list[PRQueueWindow]:
        return list(
            PRQueueWindow.objects.filter(pull_request=pr).filter(
                Q(opened_by_event_type__in=_CI, opened_by_check_run__isnull=True, opened_by_status_context__isnull=True)
                | Q(closed_by_event_type__in=_CI, closed_by_check_run__isnull=True, closed_by_status_context__isnull=True)
            )
        )

    def test_revision_gap_produces_fkless_ci_window_then_heals(self) -> None:
        pr = PullRequest.objects.create(
            repository=self.repo,
            number=100,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self._at(0),
            gh_updated_at=self._at(45),
            base_ref_name="master",
            head_ref_name="b",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
            commits_backfill_done=True,
            head_sha="hLate",
        )
        # Legacy gappy revisions: hEarly ends at +30 but hLate starts at +60 -> gap [30, 60).
        PRRevision.objects.create(pull_request=pr, head_sha="hEarly", from_ts=self._at(0), to_ts=self._at(30), seq=0)
        PRRevision.objects.create(pull_request=pr, head_sha="hLate", from_ts=self._at(60), to_ts=None, seq=1)
        self._ci("hEarly", completed_min=5)
        self._ci("hLate", completed_min=65)
        # An irrelevant label change inside the gap creates a boundary with no CI row, so
        # the eligibility flip there is attributed to CI with no FK (the bug shape).
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.LABELED,
            occurred_at=self._at(45),
            label_name="t-irrelevant",
        )
        # Clean build state so a revision rebuild would otherwise noop — mirrors the
        # production rows that stayed stuck behind the noop short-circuit.
        PRRevisionBuildState.objects.create(
            pull_request=pr,
            revision_version=1,
            builder_version=PR_REVISION_BUILDER_VERSION,
            built_through_ts=self._at(70),
            dirty_from_ts=None,
        )

        # 1. The gap yields exactly one FK-less CI window (the convergence-loop precondition).
        rebuild_queue_windows_for_pr(pr=pr, rule_sets=[self.rule_set])
        self.assertEqual(len(self._null_fk_ci_windows(pr)), 1)

        # 2. The contiguity self-heal forces a full rebuild (not a noop) and re-stitches.
        res = rebuild_pr_revisions(pr)
        self.assertNotEqual(res.strategy, "noop")
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts", "seq", "id"))
        for a, b in zip(revs, revs[1:]):
            self.assertEqual(a.to_ts, b.from_ts, "healed revisions must be contiguous")

        # 3. Rebuilding the windows over healed revisions removes the FK-less CI attribution.
        rebuild_queue_windows_for_pr(pr=pr, rule_sets=[self.rule_set])
        self.assertEqual(self._null_fk_ci_windows(pr), [])
