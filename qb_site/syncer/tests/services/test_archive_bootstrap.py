from __future__ import annotations

from django.test import SimpleTestCase

from syncer.services.archive_bootstrap import (
    ArchivePREntry,
    enumerate_archive_pr_entries,
)


def _make_fetcher(*, root_tree: list[dict], data_tree: list[dict], root_truncated: bool = False, data_truncated: bool = False):
    calls: list[tuple[str, str, str]] = []

    def fetcher(owner: str, repo: str, tree_ref: str) -> dict:
        calls.append((owner, repo, tree_ref))
        if tree_ref == "master":
            return {"tree": root_tree, "truncated": root_truncated}
        if tree_ref == "DATA_TREE_SHA":
            return {"tree": data_tree, "truncated": data_truncated}
        raise AssertionError(f"unexpected tree_ref: {tree_ref}")

    fetcher.calls = calls  # type: ignore[attr-defined]
    return fetcher


class TestEnumerateArchivePREntries(SimpleTestCase):
    def test_happy_path_filters_to_pr_dirs(self) -> None:
        fetcher = _make_fetcher(
            root_tree=[
                {"path": "README.md", "type": "blob", "sha": "x"},
                {"path": "data", "type": "tree", "sha": "DATA_TREE_SHA"},
                {"path": "scripts", "type": "tree", "sha": "y"},
            ],
            data_tree=[
                {"path": "12345", "type": "tree", "sha": "abc"},
                {"path": "67890", "type": "tree", "sha": "def"},
                {"path": "stray-file.txt", "type": "blob", "sha": "z"},
                {"path": "not-a-number", "type": "tree", "sha": "w"},
            ],
        )
        entries = enumerate_archive_pr_entries(
            owner="leanprover-community",
            archive="queueboard-archive2",
            fetcher=fetcher,
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            sorted(entries, key=lambda e: e.pr_number),
            [
                ArchivePREntry(pr_number=12345, archive_path="data/12345/pr_info.json", blob_sha="abc"),
                ArchivePREntry(pr_number=67890, archive_path="data/67890/pr_info.json", blob_sha="def"),
            ],
        )
        # Two REST calls: root tree + data tree.
        self.assertEqual(len(fetcher.calls), 2)  # type: ignore[attr-defined]

    def test_missing_data_dir_raises(self) -> None:
        fetcher = _make_fetcher(
            root_tree=[{"path": "README.md", "type": "blob", "sha": "x"}],
            data_tree=[],
        )
        with self.assertRaisesRegex(RuntimeError, "No data/ directory"):
            enumerate_archive_pr_entries(
                owner="o",
                archive="r",
                fetcher=fetcher,
            )

    def test_root_truncated_raises(self) -> None:
        fetcher = _make_fetcher(
            root_tree=[{"path": "data", "type": "tree", "sha": "DATA_TREE_SHA"}],
            data_tree=[],
            root_truncated=True,
        )
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            enumerate_archive_pr_entries(owner="o", archive="r", fetcher=fetcher)

    def test_data_tree_truncated_raises(self) -> None:
        fetcher = _make_fetcher(
            root_tree=[{"path": "data", "type": "tree", "sha": "DATA_TREE_SHA"}],
            data_tree=[{"path": "1", "type": "tree", "sha": "x"}],
            data_truncated=True,
        )
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            enumerate_archive_pr_entries(owner="o", archive="r", fetcher=fetcher)

    def test_zero_or_negative_pr_numbers_skipped(self) -> None:
        fetcher = _make_fetcher(
            root_tree=[{"path": "data", "type": "tree", "sha": "DATA_TREE_SHA"}],
            data_tree=[
                {"path": "0", "type": "tree", "sha": "x"},
                {"path": "1", "type": "tree", "sha": "y"},
            ],
        )
        entries = enumerate_archive_pr_entries(owner="o", archive="r", fetcher=fetcher)
        self.assertEqual([e.pr_number for e in entries], [1])
