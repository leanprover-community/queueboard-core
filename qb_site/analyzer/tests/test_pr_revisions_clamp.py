"""Tests for the clamp-to-creation window fix (design decision 048).

Covers the CI-only and force-push builders, the `_normalize_windows` backstop,
the self-healing full rebuild, transitions between builders, and combinatorial
sweeps over event timings relative to `gh_created_at` to catch edge cases.
"""

from __future__ import annotations

import itertools

from django.db.models import F
from django.test import TestCase
from django.utils import timezone

from core.models import Repository
from syncer.models import CommitCheckRun, PullRequest, PRTimelineEvent, PRTimelineEventType
from analyzer.models import PRRevision, PRRevisionBuildState
from analyzer.services.revisions import (
    PR_REVISION_BUILDER_VERSION,
    _build_ci_head_windows,
    _build_force_push_head_windows,
    _normalize_windows,
    rebuild_pr_revisions,
)


class ClampTestBase(TestCase):
    def setUp(self) -> None:
        self.repo = Repository.objects.create(owner="o", name="r", default_branch="master", is_active=True)
        self.t0 = timezone.now() - timezone.timedelta(days=10)

    def _at(self, minutes: int):
        return self.t0 + timezone.timedelta(minutes=minutes)

    def _mk_pr(self, number: int, *, created_min: int = 0, head_sha: str = "") -> PullRequest:
        return PullRequest.objects.create(
            repository=self.repo,
            number=number,
            author=None,
            state="open",
            is_draft=False,
            gh_created_at=self._at(created_min),
            gh_updated_at=self._at(created_min),
            base_ref_name="master",
            head_ref_name="branch",
            head_repo_owner_login="o",
            head_repo_name="fork",
            title="t",
            body="",
            additions=0,
            deletions=0,
            changed_files_count=0,
            timeline_backfill_done=True,
            head_sha=head_sha,
        )

    def _ci(self, head: str, start_min: int, *, dur_min: int = 5, conclusion: str = "SUCCESS", nid: str | None = None) -> None:
        CommitCheckRun.objects.create(
            repository=self.repo,
            github_node_id=nid or f"CR_{head}_{start_min}",
            head_sha=head,
            name="ci",
            status="COMPLETED",
            conclusion=conclusion,
            details_url=None,
            external_id=None,
            gh_started_at=self._at(start_min),
            gh_completed_at=self._at(start_min + dur_min),
        )

    def _fp(self, pr: PullRequest, before: str, after: str, at_min: int) -> None:
        PRTimelineEvent.objects.create(
            pull_request=pr,
            type=PRTimelineEventType.HEAD_FORCE_PUSHED,
            occurred_at=self._at(at_min),
            before_sha=before,
            after_sha=after,
        )

    # --- invariant assertions -------------------------------------------------

    def assert_window_list_valid(self, windows, *, created, head_sha=None):
        """Structural invariant for a raw window list (from a builder)."""
        for from_ts, sha, to_ts in windows:
            if to_ts is not None:
                self.assertLess(from_ts, to_ts, f"malformed/zero-width window {(from_ts, sha, to_ts)}")
        for (f1, _s1, t1), (f2, _s2, _t2) in zip(windows, windows[1:]):
            self.assertLess(f1, f2, "from_ts must strictly increase")
            self.assertEqual(t1, f2, "windows must be contiguous (to_ts[i] == from_ts[i+1])")
        if windows:
            opens = [w for w in windows if w[2] is None]
            self.assertEqual(len(opens), 1, "exactly one open-ended window")
            self.assertIsNone(windows[-1][2], "the open-ended window must be chronologically last")
            self.assertGreaterEqual(windows[0][0], created, "no window may start before gh_created_at")
            if head_sha is not None:
                self.assertEqual(windows[-1][1], head_sha, "open-ended head must equal head_sha")

    def assert_db_windows_valid(self, pr: PullRequest):
        """Structural invariant for the persisted PRRevision rows of a PR."""
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts", "seq", "id"))
        # No malformed rows anywhere.
        self.assertFalse(
            PRRevision.objects.filter(pull_request=pr, to_ts__isnull=False, to_ts__lt=F("from_ts")).exists(),
            "no malformed (to_ts < from_ts) rows may persist",
        )
        windows = [(r.from_ts, r.head_sha, r.to_ts) for r in revs]
        head = (pr.head_sha or "").strip() or None
        self.assert_window_list_valid(windows, created=pr.gh_created_at, head_sha=head)
        if revs:
            # The max-from_ts row must be the open-ended current head (no escaping tail).
            tail = sorted(revs, key=lambda r: (r.from_ts, r.seq, r.id))[-1]
            self.assertIsNone(tail.to_ts, "max(from_ts) row must be the open-ended window")


class TestCiBuilderClamp(ClampTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.pr = self._mk_pr(1, created_min=0)

    def _build(self, first_seen):
        return _normalize_windows(_build_ci_head_windows(self.pr, first_seen))

    def test_empty(self) -> None:
        self.assertEqual(self._build({}), [])

    def test_single_head_after_creation(self) -> None:
        w = self._build({"h0": self._at(60)})
        self.assertEqual(w, [(self._at(0), "h0", None)])
        self.assert_window_list_valid(w, created=self._at(0))

    def test_single_head_before_creation(self) -> None:
        # A lone pre-creation head opens its window at creation, not before.
        w = self._build({"h0": self._at(-60)})
        self.assertEqual(w, [(self._at(0), "h0", None)])
        self.assert_window_list_valid(w, created=self._at(0))

    def test_all_heads_before_creation_collapse_to_last(self) -> None:
        # The #17574/#17448 shape: every head's CI predates creation.
        w = self._build({"h0": self._at(-120), "h1": self._at(-60), "h2": self._at(-1)})
        self.assertEqual(w, [(self._at(0), "h2", None)])  # last pre-creation head, anchored at creation
        self.assert_window_list_valid(w, created=self._at(0))

    def test_mixed_before_and_after_creation(self) -> None:
        # Two heads before creation collapse; the post-creation head keeps its boundary.
        w = self._build({"h0": self._at(-60), "h1": self._at(-30), "h2": self._at(60)})
        self.assertEqual(w, [(self._at(0), "h1", self._at(60)), (self._at(60), "h2", None)])
        self.assert_window_list_valid(w, created=self._at(0))

    def test_all_heads_after_creation_unchanged(self) -> None:
        # Normal PR: clamp is a no-op. Window-0 spans [created, next-head-first-seen)
        # — the first head's own first-seen is not a boundary (pre-existing behavior).
        w = self._build({"h0": self._at(30), "h1": self._at(90)})
        self.assertEqual(w, [(self._at(0), "h0", self._at(90)), (self._at(90), "h1", None)])
        self.assert_window_list_valid(w, created=self._at(0))

    def test_head_exactly_at_creation(self) -> None:
        # A head first-seen exactly at creation must not collide with the collapsed
        # pre-creation window; the at-creation head wins.
        w = self._build({"h0": self._at(-60), "h1": self._at(0)})
        self.assertEqual(w, [(self._at(0), "h1", None)])
        self.assert_window_list_valid(w, created=self._at(0))

    def test_duplicate_first_seen_timestamps(self) -> None:
        # Two heads sharing a pre-creation timestamp: still forward and contiguous.
        w = self._build({"a": self._at(-60), "b": self._at(-60), "c": self._at(30)})
        self.assert_window_list_valid(w, created=self._at(0))
        self.assertEqual(w[-1][2], None)

    def test_combinatorial_offsets(self) -> None:
        created = self._at(0)
        pool = [-300, -120, -1, 0, 1, 120, 300]
        for size in (1, 2, 3, 4):
            for combo in itertools.combinations(pool, size):
                first_seen = {f"h{idx}": self._at(off) for idx, off in enumerate(combo)}
                w = self._normalized_build(first_seen)
                self.assert_window_list_valid(w, created=created)

    def _normalized_build(self, first_seen):
        return _normalize_windows(_build_ci_head_windows(self.pr, first_seen))


class TestForcePushBuilderClamp(ClampTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.pr = self._mk_pr(2, created_min=0)

    def _fps(self, specs):
        """specs: list of (before, after, occurred_min)."""
        out = []
        for before, after, occ in specs:
            out.append(
                PRTimelineEvent(
                    pull_request=self.pr,
                    type=PRTimelineEventType.HEAD_FORCE_PUSHED,
                    occurred_at=self._at(occ),
                    before_sha=before,
                    after_sha=after,
                )
            )
        return out

    def _build(self, specs, ci=None):
        fps = self._fps(specs)
        return _normalize_windows(_build_force_push_head_windows(self.pr, fps, ci or {}))

    def test_normal_all_after_creation(self) -> None:
        w = self._build([("a", "b", 60), ("b", "c", 120)])
        self.assertEqual(
            w,
            [(self._at(0), "a", self._at(60)), (self._at(60), "b", self._at(120)), (self._at(120), "c", None)],
        )
        self.assert_window_list_valid(w, created=self._at(0), head_sha="c")

    def test_first_force_push_before_creation(self) -> None:
        # Seg-0 [created, first_fp) is inverted; it is dropped and the covering
        # segment is clamped to start at creation.
        w = self._build([("a", "b", -60), ("b", "c", 60)])
        self.assertEqual(w, [(self._at(0), "b", self._at(60)), (self._at(60), "c", None)])
        self.assert_window_list_valid(w, created=self._at(0), head_sha="c")

    def test_all_force_pushes_before_creation(self) -> None:
        w = self._build([("a", "b", -120), ("b", "c", -60)])
        self.assertEqual(w, [(self._at(0), "c", None)])
        self.assert_window_list_valid(w, created=self._at(0), head_sha="c")

    def test_in_segment_ci_before_creation_filtered(self) -> None:
        # Pre-creation in-segment CI must not open a window before creation.
        w = self._build([("a", "b", 60)], ci={"x": self._at(-30), "y": self._at(30)})
        self.assert_window_list_valid(w, created=self._at(0), head_sha="b")
        self.assertGreaterEqual(w[0][0], self._at(0))

    def test_combinatorial_offsets(self) -> None:
        created = self._at(0)
        # Chains of 1-3 force pushes with occurred offsets spanning before/after creation.
        offset_patterns = [
            [60],
            [-60],
            [-60, 60],
            [60, 120],
            [-120, -60],
            [-120, 60],
            [-120, -60, 60],
            [-60, 60, 120],
            [60, 120, 180],
            [-200, -100, -50],
        ]
        for pattern in offset_patterns:
            shas = [f"s{i}" for i in range(len(pattern) + 1)]
            specs = [(shas[i], shas[i + 1], off) for i, off in enumerate(pattern)]
            w = self._build(specs)
            self.assert_window_list_valid(w, created=created, head_sha=shas[-1])


class TestNormalizeWindows(ClampTestBase):
    def test_empty(self) -> None:
        self.assertEqual(_normalize_windows([]), [])

    def test_already_valid_is_noop(self) -> None:
        w = [(self._at(0), "a", self._at(10)), (self._at(10), "b", None)]
        self.assertEqual(_normalize_windows(w), w)

    def test_drops_backward_window(self) -> None:
        w = [(self._at(10), "a", self._at(5)), (self._at(0), "b", None)]
        out = _normalize_windows(w)
        self.assertEqual(out, [(self._at(0), "b", None)])

    def test_drops_zero_width_window(self) -> None:
        w = [(self._at(0), "a", self._at(0)), (self._at(0), "b", None)]
        self.assertEqual(_normalize_windows(w), [(self._at(0), "b", None)])

    def test_dedupes_same_from_ts(self) -> None:
        w = [(self._at(0), "a", self._at(10)), (self._at(0), "b", None)]
        out = _normalize_windows(w)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], self._at(0))

    def test_restitches_contiguity(self) -> None:
        # A gap (to_ts < next from_ts) is closed by re-stitching.
        w = [(self._at(0), "a", self._at(5)), (self._at(10), "b", None)]
        out = _normalize_windows(w)
        self.assertEqual(out, [(self._at(0), "a", self._at(10)), (self._at(10), "b", None)])

    def test_unsorted_input(self) -> None:
        w = [(self._at(20), "c", None), (self._at(0), "a", self._at(10)), (self._at(10), "b", self._at(20))]
        out = _normalize_windows(w)
        self.assertEqual(out, [(self._at(0), "a", self._at(10)), (self._at(10), "b", self._at(20)), (self._at(20), "c", None)])


class TestRebuildIntegration(ClampTestBase):
    def _seed_state(self, pr: PullRequest, built_through_min: int) -> PRRevisionBuildState:
        return PRRevisionBuildState.objects.create(
            pull_request=pr,
            built_through_ts=self._at(built_through_min),
            dirty_from_ts=None,
            builder_version=PR_REVISION_BUILDER_VERSION,
        )

    def test_self_heals_trailing_malformed_window(self) -> None:
        # The #17574 shape (trailing 250 subset): malformed row holds max(from_ts).
        pr = self._mk_pr(10, created_min=0, head_sha="h1")
        # Seed the exact malformed state a buggy build would have left.
        PRRevision.objects.create(pull_request=pr, head_sha="h1", from_ts=self._at(-1), to_ts=None, seq=1)
        PRRevision.objects.create(pull_request=pr, head_sha="h0", from_ts=self._at(0), to_ts=self._at(-1), seq=0)
        self._ci("h0", start_min=-120)
        self._ci("h1", start_min=-1)
        self._seed_state(pr, built_through_min=0)

        res = rebuild_pr_revisions(pr)
        self.assertNotEqual(res.strategy, "noop")
        self.assert_db_windows_valid(pr)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual([r.head_sha for r in revs], ["h1"])
        self.assertEqual(revs[0].from_ts, self._at(0))

        # Converges: a second rebuild is a clean noop and leaves no malformed rows.
        res2 = rebuild_pr_revisions(pr)
        self.assertEqual(res2.strategy, "noop")
        self.assert_db_windows_valid(pr)

    def test_self_heals_mid_chain_malformed_window(self) -> None:
        # The 1733 subset: malformed seg-0 buried mid-chain; current head is post-creation.
        pr = self._mk_pr(11, created_min=0, head_sha="hC")
        PRRevision.objects.create(pull_request=pr, head_sha="h0", from_ts=self._at(0), to_ts=self._at(-30), seq=0)
        PRRevision.objects.create(pull_request=pr, head_sha="h1", from_ts=self._at(-30), to_ts=self._at(60), seq=1)
        PRRevision.objects.create(pull_request=pr, head_sha="hC", from_ts=self._at(60), to_ts=None, seq=2)
        self._ci("h0", start_min=-60)
        self._ci("h1", start_min=-30)
        self._ci("hC", start_min=60)
        self._seed_state(pr, built_through_min=65)

        res = rebuild_pr_revisions(pr)
        self.assertNotEqual(res.strategy, "noop")
        self.assert_db_windows_valid(pr)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual([r.head_sha for r in revs], ["h1", "hC"])
        self.assertEqual(revs[0].from_ts, self._at(0))
        self.assertEqual(revs[-1].to_ts, None)

    def test_ci_only_pre_creation_then_force_push(self) -> None:
        # Build CI-only (commits before creation) -> clamped; then a force push lands.
        pr = self._mk_pr(12, created_min=0, head_sha="h1")
        PRRevision.objects.create(pull_request=pr, head_sha="h0", from_ts=self._at(-1), to_ts=None, seq=0)
        self._ci("h0", start_min=-120)
        self._ci("h1", start_min=-1)
        self._seed_state(pr, built_through_min=0)
        rebuild_pr_revisions(pr)
        self.assert_db_windows_valid(pr)

        # Force push h1 -> h2 after creation; head advances.
        self._fp(pr, "h1", "h2", at_min=120)
        pr.head_sha = "h2"
        pr.gh_updated_at = self._at(120)
        pr.save(update_fields=["head_sha", "gh_updated_at"])
        self._ci("h2", start_min=125)

        res = rebuild_pr_revisions(pr)
        self.assertNotEqual(res.strategy, "noop")
        self.assert_db_windows_valid(pr)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(revs[-1].head_sha, "h2")
        # Idempotent afterwards.
        rebuild_pr_revisions(pr)
        self.assert_db_windows_valid(pr)

    def test_force_push_first_event_before_creation(self) -> None:
        pr = self._mk_pr(13, created_min=0, head_sha="c")
        self._fp(pr, "a", "b", at_min=-60)
        self._fp(pr, "b", "c", at_min=60)
        res = rebuild_pr_revisions(pr)
        self.assertNotEqual(res.strategy, "noop")
        self.assert_db_windows_valid(pr)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual([r.head_sha for r in revs], ["b", "c"])
        self.assertEqual(revs[0].from_ts, self._at(0))

    def test_current_head_without_ci_after_pre_creation_commits(self) -> None:
        # Pre-creation commits have CI, but the current head (a fork push) has none.
        # Clamp collapses the pre-creation heads; _ensure_current_head_window then
        # appends a trailing window for the real head. The two must compose cleanly.
        pr = self._mk_pr(14, created_min=0, head_sha="hNew")
        pr.gh_updated_at = self._at(30)
        pr.save(update_fields=["gh_updated_at"])
        PRRevision.objects.create(pull_request=pr, head_sha="h0", from_ts=self._at(-60), to_ts=None, seq=0)
        PRRevision.objects.create(pull_request=pr, head_sha="h1", from_ts=self._at(-1), to_ts=None, seq=1)
        self._ci("h0", start_min=-60)
        self._ci("h1", start_min=-1)
        self._seed_state(pr, built_through_min=0)

        res = rebuild_pr_revisions(pr)
        self.assertNotEqual(res.strategy, "noop")
        self.assert_db_windows_valid(pr)
        revs = list(PRRevision.objects.filter(pull_request=pr).order_by("from_ts"))
        self.assertEqual(revs[-1].head_sha, "hNew")
        self.assertIsNone(revs[-1].to_ts)
        self.assertEqual(revs[0].from_ts, self._at(0))
        rebuild_pr_revisions(pr)
        self.assert_db_windows_valid(pr)

    def test_append_signal_after_clamped_build(self) -> None:
        # First build clamps pre-creation commits to a single window; a later
        # post-creation CI head arrives as a forward signal (append path).
        pr = self._mk_pr(15, created_min=0, head_sha="h1")
        PRRevision.objects.create(pull_request=pr, head_sha="h0", from_ts=self._at(-1), to_ts=None, seq=0)
        self._ci("h0", start_min=-120)
        self._ci("h1", start_min=-1)
        self._seed_state(pr, built_through_min=0)
        rebuild_pr_revisions(pr)
        self.assert_db_windows_valid(pr)

        # Head advances to a post-creation commit with CI.
        pr.head_sha = "h2"
        pr.gh_updated_at = self._at(60)
        pr.save(update_fields=["head_sha", "gh_updated_at"])
        self._ci("h2", start_min=60)
        res = rebuild_pr_revisions(pr, latest_signal_ts=self._at(65))
        self.assertNotEqual(res.strategy, "noop")
        self.assert_db_windows_valid(pr)
        self.assertEqual(PRRevision.objects.filter(pull_request=pr).order_by("-from_ts").first().head_sha, "h2")
        rebuild_pr_revisions(pr, latest_signal_ts=self._at(65))
        self.assert_db_windows_valid(pr)

    def test_combinatorial_force_push_timings(self) -> None:
        # Full rebuild path across before/after-creation force-push timings; each
        # must yield valid windows and converge to a noop on the next rebuild.
        patterns = [
            [60, 120],
            [-60, 60],
            [-120, -60],
            [-60, -30, 60],
            [-100, 50, 150],
            [30, 60, 90, 120],
        ]
        for i, pattern in enumerate(patterns):
            shas = [f"p{i}_{j}" for j in range(len(pattern) + 1)]
            pr = self._mk_pr(2000 + i, created_min=0, head_sha=shas[-1])
            for j, off in enumerate(pattern):
                self._fp(pr, shas[j], shas[j + 1], at_min=off)
            res = rebuild_pr_revisions(pr)
            self.assertNotEqual(res.strategy, "noop", f"pattern {pattern}")
            self.assert_db_windows_valid(pr)
            res2 = rebuild_pr_revisions(pr)
            self.assertEqual(res2.strategy, "noop", f"pattern {pattern} should converge")
            self.assert_db_windows_valid(pr)
