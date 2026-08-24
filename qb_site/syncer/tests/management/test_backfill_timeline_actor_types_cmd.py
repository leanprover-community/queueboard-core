"""Tests for the ``backfill_timeline_actor_types`` command (design doc 051)."""

from __future__ import annotations

from io import StringIO
from typing import Any, Dict, List, Sequence
from unittest import mock

import requests
from django.core.management import call_command
from django.test import TestCase

from syncer.models import PRActorType, PRTimelineEvent, PRTimelineEventType
from syncer.tests.factories import make_pr, make_repo

CMD = "syncer.management.commands.backfill_timeline_actor_types"


def _actor(typename: str, node_id: str, login: str) -> Dict[str, Any]:
    return {"__typename": typename, "id": node_id, "login": login}


class FakeClient:
    """Stands in for GitHubClient; records every batch it is handed."""

    NODES_IDS_MAX = 100

    # Set by each test: node_id -> node dict (or None to make it unresolvable).
    responses: Dict[str, Any] = {}
    # node ids that make the whole call fail, forcing the batch-splitting path.
    poison: set = set()
    # GraphQL message the poison raises. The default is the one GitHub sends
    # for an id it cannot resolve, which is the only splittable shape.
    poison_message: str = "GraphQL error(s): Could not resolve to a node with the global id"
    # Number of leading calls that die at the transport layer (5xx / reset).
    transport_failures: int = 0
    # Make every call come back as GitHub's rate-limit rejection.
    rate_limited: bool = False

    def __init__(self, **kwargs: Any) -> None:
        self.token_id = "fake-token"
        FakeClient.calls.append(kwargs)

    calls: List[Dict[str, Any]] = []
    batches: List[List[str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.responses = {}
        cls.poison = set()
        cls.poison_message = "GraphQL error(s): Could not resolve to a node with the global id"
        cls.transport_failures = 0
        cls.rate_limited = False
        cls.calls = []
        cls.batches = []

    def get_timeline_actors_by_node_ids(self, *, ids: Sequence[str]) -> Dict[str, Any]:
        ids = list(ids)
        FakeClient.batches.append(ids)
        if len(ids) > self.NODES_IDS_MAX:
            raise ValueError("batch exceeded the nodes(ids:) cap")
        if FakeClient.rate_limited:
            raise RuntimeError("GraphQL error(s): API rate limit exceeded for user ID 1")
        if FakeClient.transport_failures > 0:
            FakeClient.transport_failures -= 1
            raise requests.ConnectionError("502 Server Error: Bad Gateway")
        if FakeClient.poison & set(ids):
            raise RuntimeError(FakeClient.poison_message)
        nodes = [FakeClient.responses.get(i) for i in ids]
        return {"data": {"rateLimit": {"cost": 1, "remaining": 4999}, "nodes": nodes}}


class BackfillCommandTestBase(TestCase):
    def setUp(self) -> None:
        FakeClient.reset()
        self.repo = make_repo(owner="leanprover-community", name="mathlib4")
        self.pr = make_pr(self.repo, 1)

    def _row(self, node_id: str, **kwargs: Any) -> PRTimelineEvent:
        defaults: Dict[str, Any] = {
            "pull_request": self.pr,
            "github_node_id": node_id,
            "type": PRTimelineEventType.LABELED,
            "occurred_at": "2025-01-01T00:00:00Z",
        }
        defaults.update(kwargs)
        return PRTimelineEvent.objects.create(**defaults)

    def _run(self, **opts: Any) -> str:
        out = StringIO()
        with mock.patch(f"{CMD}.GitHubClient", FakeClient):
            call_command("backfill_timeline_actor_types", stdout=out, **opts)
        return out.getvalue()


class TestBackfillResolution(BackfillCommandTestBase):
    def test_types_rows_from_resolved_actors(self) -> None:
        self._row("TL_BOT", actor_login="mathlib-merge-conflicts")
        self._row("TL_USER", actor_login="alice")
        FakeClient.responses = {
            "TL_BOT": {
                "__typename": "LabeledEvent",
                "id": "TL_BOT",
                "actor": _actor("Bot", "BOT_kgDOD2_IkQ", "mathlib-merge-conflicts"),
            },
            "TL_USER": {
                "__typename": "LabeledEvent",
                "id": "TL_USER",
                "actor": _actor("User", "U_kgDOAlice", "alice"),
            },
        }
        self._run()

        bot = PRTimelineEvent.objects.get(github_node_id="TL_BOT")
        self.assertEqual(bot.actor_type, PRActorType.BOT)
        self.assertEqual(bot.actor_node_id, "BOT_kgDOD2_IkQ")
        user = PRTimelineEvent.objects.get(github_node_id="TL_USER")
        self.assertEqual(user.actor_type, PRActorType.USER)
        self.assertEqual(user.actor_node_id, "U_kgDOAlice")

    def test_author_bearing_nodes_are_read_too(self) -> None:
        # IssueComment / PullRequestReview carry `author`, not `actor`. The
        # synthesized dismissed-review parents land here as well, since their
        # stored node id is the review's.
        self._row("REV_1", type=PRTimelineEventType.REVIEW_APPROVED, actor_login="alice")
        FakeClient.responses = {
            "REV_1": {
                "__typename": "PullRequestReview",
                "id": "REV_1",
                "author": _actor("User", "U_kgDOAlice", "alice"),
            }
        }
        self._run()
        self.assertEqual(PRTimelineEvent.objects.get(github_node_id="REV_1").actor_type, PRActorType.USER)

    def test_null_actor_stays_untyped(self) -> None:
        # Workflow-driven label events genuinely have no actor. This is the
        # permanent floor the drain plateaus at, not a failure.
        self._row("TL_NULL")
        FakeClient.responses = {"TL_NULL": {"__typename": "LabeledEvent", "id": "TL_NULL", "actor": None}}
        out = self._run()
        row = PRTimelineEvent.objects.get(github_node_id="TL_NULL")
        self.assertIsNone(row.actor_type)
        self.assertIsNone(row.actor_node_id)
        self.assertIn("null_actor=1", out)

    def test_unresolvable_node_is_counted_not_crashed(self) -> None:
        self._row("TL_GONE")
        FakeClient.responses = {"TL_GONE": None}
        out = self._run()
        self.assertIsNone(PRTimelineEvent.objects.get(github_node_id="TL_GONE").actor_type)
        self.assertIn("unresolved=1", out)

    def test_unmodelled_actor_type_stores_node_id_only(self) -> None:
        self._row("TL_ORG")
        FakeClient.responses = {
            "TL_ORG": {
                "__typename": "ClosedEvent",
                "id": "TL_ORG",
                "actor": _actor("Organization", "O_1", "leanprover-community"),
            }
        }
        out = self._run()
        row = PRTimelineEvent.objects.get(github_node_id="TL_ORG")
        self.assertIsNone(row.actor_type)
        self.assertEqual(row.actor_node_id, "O_1")
        self.assertIn("unmodelled=1", out)

    def test_rows_without_node_id_are_reported(self) -> None:
        self._row("TL_OK")
        PRTimelineEvent.objects.create(
            pull_request=self.pr,
            github_node_id=None,
            type=PRTimelineEventType.LABELED,
            occurred_at="2025-01-01T00:00:00Z",
        )
        FakeClient.responses = {"TL_OK": {"__typename": "LabeledEvent", "id": "TL_OK", "actor": _actor("User", "U_1", "alice")}}
        out = self._run()
        self.assertIn("1 untyped row(s) have no github_node_id", out)


class TestBackfillWriteGuards(BackfillCommandTestBase):
    def test_already_typed_rows_are_not_re_resolved(self) -> None:
        self._row("TL_DONE", actor_login="alice", actor_type=PRActorType.USER, actor_node_id="U_1")
        out = self._run()
        # Not in the target set at all: no client and no batch were ever made.
        self.assertEqual(FakeClient.batches, [])
        self.assertEqual(FakeClient.calls, [])
        self.assertIn("Nothing to backfill", out)

    def test_is_idempotent(self) -> None:
        self._row("TL_1", actor_login="alice")
        FakeClient.responses = {"TL_1": {"__typename": "LabeledEvent", "id": "TL_1", "actor": _actor("User", "U_1", "alice")}}
        self._run()
        first = PRTimelineEvent.objects.get(github_node_id="TL_1")
        batches_after_first = len(FakeClient.batches)

        self._run()
        second = PRTimelineEvent.objects.get(github_node_id="TL_1")
        self.assertEqual(
            (first.actor_type, first.actor_node_id, first.actor_login),
            (second.actor_type, second.actor_node_id, second.actor_login),
        )
        # The second run had nothing to do.
        self.assertEqual(len(FakeClient.batches), batches_after_first)

    def test_fills_missing_actor_login_for_null_and_empty(self) -> None:
        # Archive-imported rows have NULL actor_login; the _login_or_empty
        # idiom writes "". Both are "missing" and both must be filled.
        self._row("TL_NULL_LOGIN", actor_login=None)
        self._row("TL_EMPTY_LOGIN", actor_login="")
        FakeClient.responses = {
            "TL_NULL_LOGIN": {
                "__typename": "LabeledEvent",
                "id": "TL_NULL_LOGIN",
                "actor": _actor("Bot", "BOT_1", "mathlib-bors"),
            },
            "TL_EMPTY_LOGIN": {
                "__typename": "LabeledEvent",
                "id": "TL_EMPTY_LOGIN",
                "actor": _actor("Bot", "BOT_1", "mathlib-bors"),
            },
        }
        out = self._run()
        self.assertEqual(PRTimelineEvent.objects.get(github_node_id="TL_NULL_LOGIN").actor_login, "mathlib-bors")
        self.assertEqual(PRTimelineEvent.objects.get(github_node_id="TL_EMPTY_LOGIN").actor_login, "mathlib-bors")
        self.assertIn("logins=2", out)

    def test_never_overwrites_an_existing_login_even_after_a_rename(self) -> None:
        # The stored login is the login as of ingest time. Clobbering it with
        # today's login would destroy exactly the history actor_node_id exists
        # to expose.
        self._row("TL_RENAMED", actor_login="mathlib4-merge-conflict-bot")
        FakeClient.responses = {
            "TL_RENAMED": {
                "__typename": "LabeledEvent",
                "id": "TL_RENAMED",
                "actor": _actor("User", "U_kgDODVl3LA", "some-new-login"),
            }
        }
        self._run()
        row = PRTimelineEvent.objects.get(github_node_id="TL_RENAMED")
        self.assertEqual(row.actor_login, "mathlib4-merge-conflict-bot")
        self.assertEqual(row.actor_node_id, "U_kgDODVl3LA")
        self.assertEqual(row.actor_type, PRActorType.USER)

    def test_dry_run_resolves_but_writes_nothing(self) -> None:
        self._row("TL_DRY")
        FakeClient.responses = {
            "TL_DRY": {"__typename": "LabeledEvent", "id": "TL_DRY", "actor": _actor("Bot", "BOT_1", "mathlib-bors")}
        }
        out = self._run(dry_run=True)
        row = PRTimelineEvent.objects.get(github_node_id="TL_DRY")
        self.assertIsNone(row.actor_type)
        self.assertIsNone(row.actor_node_id)
        self.assertIn("Bot=1", out)
        self.assertIn("Dry run", out)


class TestBackfillBatching(BackfillCommandTestBase):
    def test_batches_at_the_hundred_id_cap(self) -> None:
        for i in range(250):
            node_id = f"TL_{i:03d}"
            self._row(node_id)
            FakeClient.responses[node_id] = {
                "__typename": "LabeledEvent",
                "id": node_id,
                "actor": _actor("User", f"U_{i}", "alice"),
            }
        self._run()
        self.assertEqual([len(b) for b in FakeClient.batches], [100, 100, 50])
        self.assertEqual(PRTimelineEvent.objects.filter(actor_type__isnull=True).count(), 0)

    def test_limit_caps_rows_resolved(self) -> None:
        for i in range(20):
            node_id = f"TL_{i:03d}"
            self._row(node_id)
            FakeClient.responses[node_id] = {
                "__typename": "LabeledEvent",
                "id": node_id,
                "actor": _actor("User", f"U_{i}", "alice"),
            }
        self._run(limit=5, batch_size=3)
        self.assertEqual([len(b) for b in FakeClient.batches], [3, 2])
        self.assertEqual(PRTimelineEvent.objects.filter(actor_type__isnull=False).count(), 5)

    def test_graphql_error_splits_the_batch_and_isolates_the_bad_id(self) -> None:
        for i in range(4):
            node_id = f"TL_{i}"
            self._row(node_id)
            FakeClient.responses[node_id] = {
                "__typename": "LabeledEvent",
                "id": node_id,
                "actor": _actor("User", f"U_{i}", "alice"),
            }
        FakeClient.poison = {"TL_2"}

        self._run(batch_size=4)
        # The three good ids still resolved; only the poisoned one was dropped.
        self.assertEqual(PRTimelineEvent.objects.filter(actor_type__isnull=False).count(), 3)
        self.assertIsNone(PRTimelineEvent.objects.get(github_node_id="TL_2").actor_type)
        # 1 failed call of 4, then halves (2 + 2), then the failing half's singles.
        self.assertEqual([len(b) for b in FakeClient.batches], [4, 2, 2, 1, 1])


class TestBackfillRateGating(BackfillCommandTestBase):
    def test_stops_when_rate_snapshot_is_below_the_floor(self) -> None:
        self._row("TL_RATE")
        FakeClient.responses = {
            "TL_RATE": {"__typename": "LabeledEvent", "id": "TL_RATE", "actor": _actor("User", "U_1", "alice")}
        }
        with mock.patch(f"{CMD}.get_rate_snapshot", return_value={"remaining": 10, "resetAt": None}):
            out = self._run(min_rate_remaining=500)
        self.assertEqual(FakeClient.batches, [])
        self.assertIsNone(PRTimelineEvent.objects.get(github_node_id="TL_RATE").actor_type)
        self.assertIn("rate budget exhausted", out)

    def test_runs_when_snapshot_is_healthy(self) -> None:
        self._row("TL_RATE_OK")
        FakeClient.responses = {
            "TL_RATE_OK": {"__typename": "LabeledEvent", "id": "TL_RATE_OK", "actor": _actor("User", "U_1", "alice")}
        }
        with mock.patch(f"{CMD}.get_rate_snapshot", return_value={"remaining": 4000, "resetAt": None}):
            self._run(min_rate_remaining=500)
        self.assertEqual(PRTimelineEvent.objects.get(github_node_id="TL_RATE_OK").actor_type, PRActorType.USER)


class TestBackfillCallFailures(BackfillCommandTestBase):
    """A ~6 k-call drain meets a flaky API; none of it may end the run."""

    def _row_with_response(self, node_id: str) -> None:
        self._row(node_id)
        FakeClient.responses[node_id] = {
            "__typename": "LabeledEvent",
            "id": node_id,
            "actor": _actor("User", f"U_{node_id}", "alice"),
        }

    def test_transport_failure_is_retried_and_then_succeeds(self) -> None:
        self._row_with_response("TL_FLAKY")
        FakeClient.transport_failures = 1
        with mock.patch(f"{CMD}.time.sleep") as sleep:
            out = self._run()
        self.assertEqual(PRTimelineEvent.objects.get(github_node_id="TL_FLAKY").actor_type, PRActorType.USER)
        self.assertEqual([len(b) for b in FakeClient.batches], [1, 1])
        self.assertIn("retries=1", out)
        sleep.assert_called_once()

    def test_persistent_transport_failure_is_counted_and_never_split(self) -> None:
        # Splitting an outage would fan one batch out into hundreds of
        # sleeping calls, so the batch is recorded unasked and the drain
        # moves on. The rows stay untyped for a later run.
        for i in range(4):
            self._row_with_response(f"TL_DOWN_{i}")
        FakeClient.transport_failures = 99
        with mock.patch(f"{CMD}.time.sleep"):
            out = self._run(batch_size=4)
        self.assertEqual(PRTimelineEvent.objects.filter(actor_type__isnull=False).count(), 0)
        # Three attempts at the same batch of 4 — no halving.
        self.assertEqual([len(b) for b in FakeClient.batches], [4, 4, 4])
        self.assertIn("call_failed=4", out)
        # And not misreported as nodes GitHub no longer has.
        self.assertIn("unresolved=0", out)

    def test_api_rate_limit_rejection_stops_the_drain(self) -> None:
        # The gate reads a cached snapshot, so it can be stale. A live
        # rejection must unwind rather than retry or split — both would only
        # spend more of a budget that is already gone.
        self._row_with_response("TL_LIMITED")
        FakeClient.rate_limited = True
        out = self._run()
        self.assertEqual([len(b) for b in FakeClient.batches], [1])
        self.assertIsNone(PRTimelineEvent.objects.get(github_node_id="TL_LIMITED").actor_type)
        self.assertIn("rate budget exhausted", out)

    def test_unattributable_graphql_error_is_not_counted_as_unresolved(self) -> None:
        # GitHub's "something went wrong" is a fact about the call, not about
        # the row; only "could not resolve" means the node is really gone.
        self._row_with_response("TL_SHRUG")
        FakeClient.poison = {"TL_SHRUG"}
        FakeClient.poison_message = "GraphQL error(s): Something went wrong while executing your query"
        out = self._run()
        self.assertIsNone(PRTimelineEvent.objects.get(github_node_id="TL_SHRUG").actor_type)
        self.assertIn("call_failed=1", out)
        self.assertIn("unresolved=0", out)


class TestBackfillRepoScoping(BackfillCommandTestBase):
    def test_repo_filter_limits_the_target_set(self) -> None:
        other_repo = make_repo(owner="leanprover-community", name="batteries")
        other_pr = make_pr(other_repo, 7)
        self._row("TL_MATHLIB")
        PRTimelineEvent.objects.create(
            pull_request=other_pr,
            github_node_id="TL_OTHER",
            type=PRTimelineEventType.LABELED,
            occurred_at="2025-01-01T00:00:00Z",
        )
        FakeClient.responses = {
            "TL_MATHLIB": {
                "__typename": "LabeledEvent",
                "id": "TL_MATHLIB",
                "actor": _actor("User", "U_1", "alice"),
            },
            "TL_OTHER": {"__typename": "LabeledEvent", "id": "TL_OTHER", "actor": _actor("User", "U_2", "bob")},
        }
        self._run(repo="leanprover-community/mathlib4")
        self.assertEqual(PRTimelineEvent.objects.get(github_node_id="TL_MATHLIB").actor_type, PRActorType.USER)
        self.assertIsNone(PRTimelineEvent.objects.get(github_node_id="TL_OTHER").actor_type)

    def test_explicit_repo_with_no_work_builds_no_client(self) -> None:
        # Constructing a client needs a token, so a no-op run must not reach
        # for one — it should just say there is nothing to do.
        self._row("TL_DONE", actor_type=PRActorType.USER, actor_node_id="U_1")
        out = self._run(repo="leanprover-community/mathlib4")
        self.assertEqual(FakeClient.calls, [])
        self.assertIn("Nothing to backfill", out)

    def test_client_is_constructed_per_repository(self) -> None:
        self._row("TL_A")
        FakeClient.responses = {"TL_A": {"__typename": "LabeledEvent", "id": "TL_A", "actor": _actor("User", "U_1", "alice")}}
        self._run()
        self.assertEqual(
            FakeClient.calls,
            [{"operation": "syncer_pr_read", "owner": "leanprover-community", "repo": "mathlib4"}],
        )
