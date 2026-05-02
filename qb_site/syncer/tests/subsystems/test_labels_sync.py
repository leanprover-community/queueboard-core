from __future__ import annotations

from django.test import TestCase

from syncer.models import LabelDef, PRLabel
from syncer.services.sub.labels_sync import sync_label_catalog, sync_pr_labels
from syncer.tests.factories import make_repo, make_pr


class TestLabelsSync(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_catalog_case_insensitive_and_attachments(self) -> None:
        labels = [{"name": "Easy", "color": "abcdef"}, {"name": "easy", "color": "ABCDEF"}]
        res = sync_label_catalog(self.repo, labels)
        self.assertEqual(LabelDef.objects.filter(repository=self.repo).count(), 1)
        self.assertGreaterEqual(res.created, 1)

        sync_pr_labels(self.pr, ["easy"])  # lower-case
        self.assertEqual(PRLabel.objects.filter(pull_request=self.pr).count(), 1)
        # Remove
        sync_pr_labels(self.pr, [])
        self.assertEqual(PRLabel.objects.filter(pull_request=self.pr).count(), 0)
