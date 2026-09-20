#!/usr/bin/env python3

"""
Unit tests for helpers in util.py.

Run via: `python -m queueboard.test_util`
"""

from __future__ import annotations

from queueboard.util import DependencyCounts, transitive_dependency_counts


def _test_chain() -> None:
    # 3 depends on 2 depends on 1: merging 1 unblocks both others.
    counts = transitive_dependency_counts({1: [], 2: [1], 3: [2]})
    assert counts[1] == DependencyCounts(upstream=0, downstream=2), counts[1]
    assert counts[2] == DependencyCounts(upstream=1, downstream=1), counts[2]
    assert counts[3] == DependencyCounts(upstream=2, downstream=0), counts[3]


def _test_diamond() -> None:
    # 4 depends on both 2 and 3, which both depend on 1: PRs are counted once, not per path.
    counts = transitive_dependency_counts({1: [], 2: [1], 3: [1], 4: [2, 3]})
    assert counts[1] == DependencyCounts(upstream=0, downstream=3), counts[1]
    assert counts[4] == DependencyCounts(upstream=3, downstream=0), counts[4]


def _test_cycle_terminates_and_excludes_self() -> None:
    # PR descriptions are free text, so mutually-dependent PRs happen: this must not hang,
    # and a PR must not count itself even though it can reach itself.
    counts = transitive_dependency_counts({1: [2], 2: [3], 3: [1]})
    for pr in (1, 2, 3):
        assert counts[pr] == DependencyCounts(upstream=2, downstream=2), (pr, counts[pr])


def _test_dependencies_outside_the_map_are_ignored() -> None:
    # Dependencies on closed or unknown PRs are filtered out by the caller's key set.
    counts = transitive_dependency_counts({1: [99], 2: [1]})
    assert counts[1] == DependencyCounts(upstream=0, downstream=1), counts[1]
    assert counts[2] == DependencyCounts(upstream=1, downstream=0), counts[2]


def _test_empty() -> None:
    assert transitive_dependency_counts({}) == {}


def main() -> None:
    _test_chain()
    _test_diamond()
    _test_cycle_terminates_and_excludes_self()
    _test_dependencies_outside_the_map_are_ignored()
    _test_empty()
    print("test_util: OK — transitive dependency counts handle chains, diamonds and cycles")


if __name__ == "__main__":
    main()
