from __future__ import annotations

from datetime import datetime, timezone

from django.test import SimpleTestCase

from analyzer.services.ci_evaluation import _evaluate_required_contexts


def _check_run(name: str, conclusion: str) -> dict:
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return {
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "head_sha": "sha",
        "gh_started_at": ts,
        "gh_completed_at": ts,
    }


class EvaluateRequiredContextsTests(SimpleTestCase):
    def test_empty_required_fragment_is_ignored(self):
        passing = [_check_run("lint", "SUCCESS")]
        # An empty/blank required context must not match every check (``"" in name`` is always True);
        # it is skipped rather than collapsing the evaluation to "aggregate of all checks".
        self.assertEqual(_evaluate_required_contexts(["lint"], passing, []), "pass")
        self.assertEqual(_evaluate_required_contexts(["", "   "], passing, []), "pass")

    def test_blank_fragment_does_not_mask_a_real_failure(self):
        runs = [_check_run("lint", "SUCCESS"), _check_run("build", "FAILURE")]
        self.assertEqual(_evaluate_required_contexts(["build", ""], runs, []), "fail")

    def test_fragment_is_matched_case_insensitively(self):
        runs = [_check_run("Lint style", "SUCCESS")]
        self.assertEqual(_evaluate_required_contexts(["Lint style"], runs, []), "pass")
