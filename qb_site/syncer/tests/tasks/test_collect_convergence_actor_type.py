"""Convergence counters for the actor-typing drain (design doc 051)."""

from __future__ import annotations

from django.test import TestCase

from syncer.models import PRActorType, PRTimelineEvent, PRTimelineEventType, SyncerConvergenceSnapshot
from syncer.tasks.collect_convergence import collect_syncer_convergence_task
from syncer.tests.factories import make_pr, make_repo


class TestCollectSyncerConvergenceActorTypeCounters(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.pr = make_pr(self.repo, 1)
        self.seq = 0

    def _event(self, **kwargs: object) -> PRTimelineEvent:
        self.seq += 1
        defaults: dict = {
            "pull_request": self.pr,
            "github_node_id": f"TL_{self.seq}",
            "type": PRTimelineEventType.LABELED,
            "occurred_at": "2025-01-01T00:00:00Z",
        }
        defaults.update(kwargs)
        return PRTimelineEvent.objects.create(**defaults)

    def _latest(self) -> SyncerConvergenceSnapshot:
        collect_syncer_convergence_task()
        return SyncerConvergenceSnapshot.objects.filter(repository=self.repo).latest("collected_at")

    def test_counts_the_drain_target_set_and_the_typeable_subset(self) -> None:
        # Untyped with a login: typeable work, counted by both.
        self._event(actor_login="alice")
        # Untyped with no login at all (archive-imported shape) and with the
        # empty-string spelling the other extraction idiom writes: still in the
        # drain's target set, but not known to have had an actor.
        self._event(actor_login=None)
        self._event(actor_login="")
        # Already typed: counted by neither.
        self._event(actor_login="bob", actor_type=PRActorType.USER, actor_node_id="U_1")
        # No node id, so the drain cannot reach it; excluded from its target set
        # but still typeable work the metric should not hide.
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            github_node_id=None,
            type=PRTimelineEventType.LABELED,
            occurred_at="2025-01-01T00:00:00Z",
            actor_login="carol",
        )

        snap = self._latest()
        self.assertEqual(snap.timeline_events_missing_actor_type, 3)
        self.assertEqual(snap.timeline_events_untyped_with_login, 2)

    def test_both_counters_are_zero_once_everything_resolvable_is_typed(self) -> None:
        # The floor the drain plateaus at: GitHub reports no actor, so there is
        # no login and nothing to type. `missing_actor_type` stays non-zero
        # forever; only the login-bearing counter converges.
        self._event(actor_login=None)
        self._event(actor_login="alice", actor_type=PRActorType.BOT, actor_node_id="BOT_1")

        snap = self._latest()
        self.assertEqual(snap.timeline_events_missing_actor_type, 1)
        self.assertEqual(snap.timeline_events_untyped_with_login, 0)

    def test_counters_are_scoped_per_repository(self) -> None:
        other_repo = make_repo(owner="leanprover-community", name="batteries")
        other_pr = make_pr(other_repo, 7)
        self._event(actor_login="alice")
        PRTimelineEvent.objects.create(
            pull_request=other_pr,
            github_node_id="TL_OTHER",
            type=PRTimelineEventType.LABELED,
            occurred_at="2025-01-01T00:00:00Z",
            actor_login="bob",
        )

        collect_syncer_convergence_task()
        mine = SyncerConvergenceSnapshot.objects.filter(repository=self.repo).latest("collected_at")
        theirs = SyncerConvergenceSnapshot.objects.filter(repository=other_repo).latest("collected_at")
        self.assertEqual(mine.timeline_events_untyped_with_login, 1)
        self.assertEqual(theirs.timeline_events_untyped_with_login, 1)
        self.assertEqual(mine.timeline_events_missing_actor_type, 1)
        self.assertEqual(theirs.timeline_events_missing_actor_type, 1)
