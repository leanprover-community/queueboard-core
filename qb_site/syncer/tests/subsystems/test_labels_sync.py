from __future__ import annotations

from django.test import TestCase

from syncer.models import LabelDef, PRLabel
from syncer.services.sub.labels_sync import (
    fetch_repo_label_catalog,
    sync_full_label_catalog,
    sync_label_catalog,
    sync_pr_labels,
)
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


class TestSyncFullLabelCatalog(TestCase):
    def setUp(self) -> None:
        self.repo = make_repo()
        self.pr = make_pr(self.repo, 1)

    def test_creates_updates_and_deletes_against_authoritative_list(self) -> None:
        sync_label_catalog(self.repo, [{"name": "stale", "color": "111111"}, {"name": "rename", "color": "222222"}])
        sync_pr_labels(self.pr, ["stale", "rename"])
        self.assertEqual(PRLabel.objects.filter(pull_request=self.pr).count(), 2)

        res = sync_full_label_catalog(
            self.repo,
            [
                {"name": "Rename", "color": "222222"},  # casing change only
                {"name": "fresh", "color": "abcdef"},  # new label
                # "stale" missing -> should be deleted, cascading PRLabel
            ],
        )

        self.assertEqual(res.created, 1)
        self.assertEqual(res.updated, 1)
        self.assertEqual(res.deleted, 1)
        names = sorted(LabelDef.objects.filter(repository=self.repo).values_list("name", flat=True))
        self.assertEqual(names, ["Rename", "fresh"])
        # stale label removal cascaded; only the rename attachment remains
        remaining = list(PRLabel.objects.filter(pull_request=self.pr).values_list("label_def__name", flat=True))
        self.assertEqual(remaining, ["Rename"])

    def test_partial_pagination_raises_before_deleting(self) -> None:
        sync_label_catalog(self.repo, [{"name": "keep", "color": "111111"}])

        responses = iter(
            [
                {
                    "data": {
                        "repository": {
                            "labels": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                                "nodes": [{"name": "keep", "color": "111111"}],
                            }
                        }
                    }
                },
                {"data": {"repository": None}},  # truncated second page
            ]
        )

        with self.assertRaises(RuntimeError):
            fetch_repo_label_catalog(self.repo, lambda after: next(responses))

        # Catalog must be untouched after a failed fetch.
        names = list(LabelDef.objects.filter(repository=self.repo).values_list("name", flat=True))
        self.assertEqual(names, ["keep"])

    def test_fetcher_paginates_until_has_next_page_false(self) -> None:
        responses = iter(
            [
                {
                    "data": {
                        "repository": {
                            "labels": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                "nodes": [{"name": "a", "color": "111111"}],
                            }
                        }
                    }
                },
                {
                    "data": {
                        "repository": {
                            "labels": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"name": "b", "color": "222222"}],
                            }
                        }
                    }
                },
            ]
        )
        seen_cursors: list = []

        def fetcher(after):
            seen_cursors.append(after)
            return next(responses)

        nodes = fetch_repo_label_catalog(self.repo, fetcher)
        self.assertEqual([n["name"] for n in nodes], ["a", "b"])
        self.assertEqual(seen_cursors, [None, "c1"])
