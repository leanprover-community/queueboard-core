from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from syncer.services.ci_backoff import record_ci_sha_fetch
from syncer.tests.factories import make_pr, make_repo


class TestCIShaBackoff(TestCase):
    def test_record_ci_sha_fetch_keeps_ok_sticky(self) -> None:
        repo = make_repo()
        pr = make_pr(repo, 1)
        t0 = timezone.now()
        record_ci_sha_fetch(pr=pr, sha="abc123", result="ok", now=t0)

        t1 = t0 + timedelta(minutes=5)
        record_ci_sha_fetch(pr=pr, sha="abc123", result="filtered", now=t1)

        state = repo.cishafetchstate_set.get(sha="abc123")
        self.assertEqual(state.last_result, "ok")
        self.assertEqual(state.last_success_at, t0)
        self.assertEqual(state.last_attempted_at, t1)
