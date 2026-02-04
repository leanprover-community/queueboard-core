#!/usr/bin/env python3

"""
Unit test for process.py helpers.

Run via: `python -m queueboard.test_process`
"""

from __future__ import annotations

from queueboard.process import _compute_commenter_data


def _test_compute_commenter_data_handles_missing_authors() -> None:
    pr_data = {
        "data": {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            {"author": {"login": "alice"}},
                            {"author": None},
                            {"author": {"login": None}},
                        ]
                    },
                    "reviews": {
                        "nodes": [
                            {"author": {"login": "bob"}},
                            {"author": None},
                        ]
                    },
                }
            }
        }
    }
    is_incomplete, commenters = _compute_commenter_data(pr_data)
    assert is_incomplete is False
    assert commenters == ["alice", "bob"]


def main() -> None:
    _test_compute_commenter_data_handles_missing_authors()
    print("test_process: OK — commenter data ignores missing authors")


if __name__ == "__main__":
    main()
