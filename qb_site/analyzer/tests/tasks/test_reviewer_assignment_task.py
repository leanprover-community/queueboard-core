from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from analyzer.tasks.reviewer_assignment import build_reviewer_assignment
from core.models import Repository


class ReviewerAssignmentTaskTests(TestCase):
    def test_build_reviewer_assignment_returns_trace_payload(self):
        repo = Repository.objects.create(owner="leanprover-community", name="mathlib4", default_branch="master")

        class DummySnapshot:
            def __init__(self) -> None:
                self.id = 123
                self.assignment_count = 5
                self.queue_snapshot = object()

        dummy_snapshot = DummySnapshot()

        with (
            patch(
                "analyzer.tasks.reviewer_assignment.ReviewerAssignmentBuilder.build_and_store",
                return_value=dummy_snapshot,
            ) as mock_build,
            patch(
                "analyzer.tasks.reviewer_assignment.build_reviewer_assignment_trace",
                return_value={"meta": {"schema_version": "v1-trace-draft"}},
            ) as mock_trace,
        ):
            result = build_reviewer_assignment(repository_id=repo.id, cache_key="default")

        self.assertEqual(result["snapshot_id"], 123)
        self.assertEqual(result["assignment_count"], 5)
        self.assertEqual(result["trace"]["meta"]["schema_version"], "v1-trace-draft")
        mock_build.assert_called_once()
        mock_trace.assert_called_once()
