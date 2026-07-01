"""Tests for resurrected-label detection (archive additive-sync follow-up)."""

from __future__ import annotations

from datetime import datetime, timezone as _tz

from django.test import TestCase

from syncer.models import LabelDef, PRLabel, PRTimelineEvent, PRTimelineEventType
from syncer.services.consistency import resurrected_prlabels_queryset
from syncer.tests.factories import make_pr, make_repo


class TestResurrectedPRLabelsQueryset(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1, state="closed")
        self.label = LabelDef.objects.create(repository=self.repo, name="maintainer-merge", color="ededed")

    def _event(self, etype, when, name="maintainer-merge"):
        return PRTimelineEvent.objects.create(
            pull_request=self.pr,
            type=etype,
            occurred_at=when,
            label_name=name,
        )

    def test_flags_attachment_whose_latest_label_event_is_unlabeled(self) -> None:
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label)
        self._event(PRTimelineEventType.LABELED, datetime(2026, 2, 1, tzinfo=_tz.utc))
        self._event(PRTimelineEventType.UNLABELED, datetime(2026, 3, 11, tzinfo=_tz.utc))

        rows = list(resurrected_prlabels_queryset(self.repo))
        self.assertEqual([r.label_def_id for r in rows], [self.label.id])

    def test_ignores_attachment_whose_latest_label_event_is_labeled(self) -> None:
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label)
        self._event(PRTimelineEventType.UNLABELED, datetime(2026, 3, 11, tzinfo=_tz.utc))
        self._event(PRTimelineEventType.LABELED, datetime(2026, 4, 1, tzinfo=_tz.utc))

        self.assertEqual(resurrected_prlabels_queryset(self.repo).count(), 0)

    def test_ignores_attachment_with_no_label_timeline(self) -> None:
        # Purely-historical PR: label attached, no LABELED/UNLABELED events at all.
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label)
        self.assertEqual(resurrected_prlabels_queryset(self.repo).count(), 0)

    def test_matches_case_insensitively_on_label_name(self) -> None:
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label)
        # Timeline stored a different display casing than the LabelDef.
        self._event(PRTimelineEventType.UNLABELED, datetime(2026, 3, 11, tzinfo=_tz.utc), name="Maintainer-Merge")
        self.assertEqual(resurrected_prlabels_queryset(self.repo).count(), 1)

    def test_repository_scope_is_respected(self) -> None:
        PRLabel.objects.create(pull_request=self.pr, label_def=self.label)
        self._event(PRTimelineEventType.UNLABELED, datetime(2026, 3, 11, tzinfo=_tz.utc))

        other = make_repo(owner="o2", name="r2")
        self.assertEqual(resurrected_prlabels_queryset(other).count(), 0)
        self.assertEqual(resurrected_prlabels_queryset(None).count(), 1)
