from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.test import SimpleTestCase

from qb_site.settings.env_files import resolve_env_files
from syncer.services.archive_bootstrap import (
    ArchivePREntry,
    default_tree_fetcher,
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


class TestDefaultTreeFetcher(SimpleTestCase):
    def test_sends_token_supplied_as_a_file(self) -> None:
        # GH_TOKEN_FILE is resolved into os.environ at settings import (design doc 058).
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_text("file-token,second-token\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"GH_TOKEN_FILE": str(token_path), "GH_TOKEN": "", "GITHUB_TOKEN": ""}):
                resolve_env_files()
                with mock.patch("syncer.services.archive_bootstrap.requests.get") as mget:
                    mget.return_value.json.return_value = {"tree": []}
                    default_tree_fetcher("o", "r", "master")

        self.assertEqual(mget.call_args.kwargs["headers"]["Authorization"], "Bearer file-token")
