"""Timeline actor typing: ``actor_type`` / ``actor_node_id`` (design doc 051).

Covers the extraction helpers, one case per ``__typename`` branch that sets
``actor_login``, the fill-empty update path, and the synthesized
dismissed-review parent.
"""

from __future__ import annotations

from django.test import TestCase

from syncer.models import PRActorType, PRTimelineEvent, PRTimelineEventType
from syncer.services.sub.timeline_sync import (
    _actor_node_id_or_none,
    _actor_type_or_none,
    _extract_event_fields,
    _synthesize_dismissed_review_parent,
    sync_timeline_events,
)
from syncer.tests.factories import make_pr, make_repo

USER = {"__typename": "User", "id": "U_kgDOAlice", "login": "alice"}
BOT = {"__typename": "Bot", "id": "BOT_kgDOBors", "login": "mathlib-bors"}
MANNEQUIN = {"__typename": "Mannequin", "id": "MNQ_kgDOGhost", "login": "ghost"}


def _ev(typename: str, node_id: str, **extra):
    base = {"__typename": typename, "id": node_id, "createdAt": "2025-01-01T00:00:00Z"}
    base.update(extra)
    return base


class TestActorHelpers(TestCase):
    def test_known_typenames_pass_through(self) -> None:
        self.assertEqual(_actor_type_or_none(USER), "User")
        self.assertEqual(_actor_type_or_none(BOT), "Bot")
        self.assertEqual(_actor_type_or_none(MANNEQUIN), "Mannequin")

    def test_unknown_typename_is_dropped_not_stored_raw(self) -> None:
        # A future union member (Organization, EnterpriseUserAccount, …) must
        # not land in the column: NULL means unknown, and that is what it is.
        self.assertIsNone(_actor_type_or_none({"__typename": "Organization", "id": "O_1", "login": "org"}))

    def test_null_and_non_dict_actors(self) -> None:
        for actor in (None, "", [], {}):
            self.assertIsNone(_actor_type_or_none(actor))
            self.assertIsNone(_actor_node_id_or_none(actor))

    def test_node_id_is_stringified_and_empty_is_none(self) -> None:
        self.assertEqual(_actor_node_id_or_none({"__typename": "User", "id": 12345}), "12345")
        self.assertIsNone(_actor_node_id_or_none({"__typename": "User", "id": None}))
        self.assertIsNone(_actor_node_id_or_none({"__typename": "User"}))


class TestExtractEventFieldsActorIdentity(TestCase):
    """One case per branch of ``_extract_event_fields`` that sets actor_login."""

    def _assert_identity(self, ev: dict, *, login: str, actor_type: str, node_id: str) -> None:
        fields = _extract_event_fields(ev)
        assert fields is not None
        self.assertEqual(fields["actor_login"], login)
        self.assertEqual(fields["actor_type"], actor_type)
        self.assertEqual(fields["actor_node_id"], node_id)

    def test_labeled_and_unlabeled(self) -> None:
        for tn in ("LabeledEvent", "UnlabeledEvent"):
            self._assert_identity(
                _ev(tn, "N1", actor=BOT, label={"name": "ready-to-merge"}),
                login="mathlib-bors",
                actor_type="Bot",
                node_id="BOT_kgDOBors",
            )

    def test_assigned_and_unassigned(self) -> None:
        for tn in ("AssignedEvent", "UnassignedEvent"):
            self._assert_identity(
                _ev(tn, "N2", actor=USER, assignee=USER),
                login="alice",
                actor_type="User",
                node_id="U_kgDOAlice",
            )

    def test_draft_and_state_flips(self) -> None:
        for tn in ("ReadyForReviewEvent", "ConvertToDraftEvent", "ReopenedEvent", "ClosedEvent"):
            self._assert_identity(_ev(tn, "N3", actor=MANNEQUIN), login="ghost", actor_type="Mannequin", node_id="MNQ_kgDOGhost")

    def test_force_push(self) -> None:
        self._assert_identity(
            _ev(
                "HeadRefForcePushedEvent",
                "N4",
                actor=USER,
                beforeCommit={"oid": "a" * 40},
                afterCommit={"oid": "b" * 40},
            ),
            login="alice",
            actor_type="User",
            node_id="U_kgDOAlice",
        )

    def test_issue_comment_uses_author(self) -> None:
        self._assert_identity(
            _ev("IssueComment", "N5", author=BOT), login="mathlib-bors", actor_type="Bot", node_id="BOT_kgDOBors"
        )

    def test_pull_request_review_uses_author(self) -> None:
        ev = {
            "__typename": "PullRequestReview",
            "id": "N6",
            "state": "APPROVED",
            "submittedAt": "2025-01-01T00:00:00Z",
            "author": USER,
            "comments": {"totalCount": 0},
        }
        self._assert_identity(ev, login="alice", actor_type="User", node_id="U_kgDOAlice")

    def test_review_dismissed_types_the_dismisser(self) -> None:
        ev = _ev(
            "ReviewDismissedEvent",
            "N7",
            previousReviewState="CHANGES_REQUESTED",
            actor=USER,
            review={"id": "REV_1", "submittedAt": "2024-12-31T00:00:00Z", "author": BOT},
        )
        self._assert_identity(ev, login="alice", actor_type="User", node_id="U_kgDOAlice")
        fields = _extract_event_fields(ev)
        assert fields is not None
        # …and denormalizes the *dismissed review's* author separately.
        self.assertEqual(fields["extra"]["dismissed_review_author"], "mathlib-bors")
        self.assertEqual(fields["extra"]["dismissed_review_author_type"], "Bot")
        self.assertEqual(fields["extra"]["dismissed_review_author_node_id"], "BOT_kgDOBors")

    def test_review_requested_and_removed(self) -> None:
        for tn in ("ReviewRequestedEvent", "ReviewRequestRemovedEvent"):
            self._assert_identity(
                _ev(tn, "N8", actor=USER, requestedReviewer={"__typename": "User", "login": "bob"}),
                login="alice",
                actor_type="User",
                node_id="U_kgDOAlice",
            )

    def test_null_actor_yields_none_not_user(self) -> None:
        # GitHub returns actor: null for workflow-driven label events. This is
        # a permanent population, not a transient gap.
        fields = _extract_event_fields(_ev("LabeledEvent", "N9", actor=None, label={"name": "delegated"}))
        assert fields is not None
        self.assertIsNone(fields["actor_type"])
        self.assertIsNone(fields["actor_node_id"])

    def test_absent_actor_field_yields_none(self) -> None:
        # The legacy archive fragment omits `actor` entirely.
        fields = _extract_event_fields(_ev("LabeledEvent", "N10", label={"name": "WIP"}))
        assert fields is not None
        self.assertIsNone(fields["actor_type"])
        self.assertIsNone(fields["actor_node_id"])

    def test_unknown_actor_typename_is_not_stored(self) -> None:
        actor = {"__typename": "Organization", "id": "O_1", "login": "leanprover-community"}
        fields = _extract_event_fields(_ev("ClosedEvent", "N11", actor=actor))
        assert fields is not None
        self.assertEqual(fields["actor_login"], "leanprover-community")
        self.assertIsNone(fields["actor_type"])
        # The node id is still exact and useful even when the kind is unmodelled.
        self.assertEqual(fields["actor_node_id"], "O_1")


class TestTimelineSyncActorIdentityPersistence(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_create_persists_identity(self) -> None:
        sync_timeline_events(self.pr, [_ev("LabeledEvent", "TL1", actor=BOT, label={"name": "CI"})])
        row = PRTimelineEvent.objects.get(github_node_id="TL1")
        self.assertEqual(row.actor_type, PRActorType.BOT)
        self.assertEqual(row.actor_node_id, "BOT_kgDOBors")

    def test_rewalk_fills_previously_empty_identity(self) -> None:
        # Simulates a row ingested before the columns existed (or an archive row).
        sync_timeline_events(self.pr, [_ev("LabeledEvent", "TL2", label={"name": "CI"})])
        row = PRTimelineEvent.objects.get(github_node_id="TL2")
        self.assertIsNone(row.actor_type)

        res = sync_timeline_events(self.pr, [_ev("LabeledEvent", "TL2", actor=BOT, label={"name": "CI"})])
        self.assertEqual(res.updated, 1)
        row.refresh_from_db()
        self.assertEqual(row.actor_type, PRActorType.BOT)
        self.assertEqual(row.actor_node_id, "BOT_kgDOBors")
        self.assertEqual(row.actor_login, "mathlib-bors")

    def test_rewalk_never_overwrites_an_existing_identity(self) -> None:
        sync_timeline_events(self.pr, [_ev("ClosedEvent", "TL3", actor=USER)])
        # A later walk reporting a different account must not clobber the
        # ingest-time attribution — that history is the point of the node id.
        sync_timeline_events(self.pr, [_ev("ClosedEvent", "TL3", actor=BOT)])
        row = PRTimelineEvent.objects.get(github_node_id="TL3")
        self.assertEqual(row.actor_type, PRActorType.USER)
        self.assertEqual(row.actor_node_id, "U_kgDOAlice")
        self.assertEqual(row.actor_login, "alice")

    def test_archive_mode_rows_ingest_untyped(self) -> None:
        res = sync_timeline_events(
            self.pr,
            [_ev("LabeledEvent", "TL4", label={"name": "WIP"})],
            archive_mode=True,
        )
        self.assertEqual(res.created, 1)
        row = PRTimelineEvent.objects.get(github_node_id="TL4")
        self.assertIsNone(row.actor_type)
        self.assertIsNone(row.actor_node_id)
        self.assertIsNone(row.actor_login)


class TestSynthesizedDismissedReviewParentIdentity(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def _dismiss_event(self, *, review_author: dict | None) -> dict:
        return _ev(
            "ReviewDismissedEvent",
            "TL_DIS",
            previousReviewState="CHANGES_REQUESTED",
            actor=USER,
            review={"id": "REV_1", "submittedAt": "2024-12-31T00:00:00Z", "author": review_author},
        )

    def test_synthesized_parent_carries_review_author_identity(self) -> None:
        sync_timeline_events(self.pr, [self._dismiss_event(review_author=BOT)])
        parent = PRTimelineEvent.objects.get(github_node_id="REV_1")
        self.assertEqual(parent.type, PRTimelineEventType.REVIEW_CHANGES_REQUESTED)
        self.assertEqual(parent.actor_login, "mathlib-bors")
        self.assertEqual(parent.actor_type, PRActorType.BOT)
        self.assertEqual(parent.actor_node_id, "BOT_kgDOBors")

    def test_synthesized_parent_stays_untyped_for_legacy_extra(self) -> None:
        # A REVIEW_DISMISSED row ingested before doc 051 has no
        # dismissed_review_author_type key in `extra`; synthesis must not
        # invent one. The nodes(ids:) backfill heals these.
        legacy_extra = {
            "previous_review_state": "APPROVED",
            "dismissed_review_node_id": "REV_LEGACY",
            "dismissed_review_author": "alice",
            "dismissed_review_submitted_at": "2024-12-31T00:00:00Z",
        }
        parent, created = _synthesize_dismissed_review_parent(self.pr, legacy_extra)
        self.assertTrue(created)
        assert parent is not None
        self.assertEqual(parent.actor_login, "alice")
        self.assertIsNone(parent.actor_type)
        self.assertIsNone(parent.actor_node_id)

    def test_synthesized_parent_drops_unmodelled_author_type(self) -> None:
        parent, _ = _synthesize_dismissed_review_parent(
            self.pr,
            {
                "previous_review_state": "APPROVED",
                "dismissed_review_node_id": "REV_ODD",
                "dismissed_review_author": "leanprover-community",
                "dismissed_review_submitted_at": "2024-12-31T00:00:00Z",
                "dismissed_review_author_type": "Organization",
                "dismissed_review_author_node_id": "O_1",
            },
        )
        assert parent is not None
        self.assertIsNone(parent.actor_type)
        self.assertEqual(parent.actor_node_id, "O_1")
