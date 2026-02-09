from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from syncer.services.ci_backoff import filter_ci_shas_for_enqueue, record_ci_sha_fetch, reset_ci_sha_fetch_state
from syncer.tests.factories import make_pr, make_repo
from syncer.services.ci_backoff import should_enqueue_ci_sha
from syncer.models import CIShaFetchState


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

    @override_settings(SYNCER_CI_SHA_HARD_CAP_DAYS=400, SYNCER_CI_SHA_BACKOFF_ERROR_SECONDS=300)
    def test_error_ignores_hard_cap(self) -> None:
        repo = make_repo()
        pr = make_pr(repo, 2)
        pr.gh_updated_at = timezone.now() - timedelta(days=401)
        pr.save(update_fields=["gh_updated_at", "updated_at"])
        state = CIShaFetchState.objects.create(
            repository=repo,
            sha="deadbeef",
            last_attempted_at=timezone.now() - timedelta(minutes=10),
            last_success_at=None,
            last_result="error",
            attempts=1,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(days=500))

        self.assertTrue(should_enqueue_ci_sha(pr=pr, sha="deadbeef"))

    @override_settings(SYNCER_CI_SHA_MIN_ATTEMPTS_TERMINAL=2, SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=1)
    def test_terminal_requires_min_attempts(self) -> None:
        repo = make_repo()
        pr = make_pr(repo, 3)
        state = CIShaFetchState.objects.create(
            repository=repo,
            sha="sha-terminal",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=1,
        )
        # Even if the settle window has passed, attempt count should allow a retry.
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=10))
        self.assertTrue(should_enqueue_ci_sha(pr=pr, sha="sha-terminal"))

        # Second attempt should allow terminal behavior.
        state.refresh_from_db()
        state.attempts = 2
        state.last_attempted_at = timezone.now() - timedelta(seconds=10)
        state.save(update_fields=["attempts", "last_attempted_at", "updated_at"])
        self.assertFalse(should_enqueue_ci_sha(pr=pr, sha="sha-terminal"))

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60)
    def test_filter_ci_shas_for_enqueue_respects_backoff(self) -> None:
        repo = make_repo()
        pr = make_pr(repo, 4)
        state = CIShaFetchState.objects.create(
            repository=repo,
            sha="sha-blocked",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=120))

        allowed, blocked = filter_ci_shas_for_enqueue(pr=pr, shas=["sha-blocked", "sha-allowed"])
        self.assertEqual(allowed, ["sha-allowed"])
        self.assertEqual(blocked, ["sha-blocked"])

    @override_settings(SYNCER_CI_SHA_SETTLE_WINDOW_SECONDS=60)
    def test_filter_ci_shas_for_enqueue_override_backoff(self) -> None:
        repo = make_repo()
        pr = make_pr(repo, 5)
        state = CIShaFetchState.objects.create(
            repository=repo,
            sha="sha-blocked",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="filtered",
            attempts=2,
        )
        CIShaFetchState.objects.filter(pk=state.pk).update(created_at=timezone.now() - timedelta(seconds=120))

        allowed, blocked = filter_ci_shas_for_enqueue(pr=pr, shas=["sha-blocked"], override_backoff=True)
        self.assertEqual(allowed, ["sha-blocked"])
        self.assertEqual(blocked, [])

    def test_reset_ci_sha_fetch_state_deletes_only_repo_rows(self) -> None:
        repo = make_repo()
        other_repo = make_repo(owner="other", name="repo")
        pr = make_pr(repo, 6)
        make_pr(other_repo, 7)
        CIShaFetchState.objects.create(
            repository=repo,
            sha="sha-reset",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="error",
            attempts=1,
        )
        CIShaFetchState.objects.create(
            repository=other_repo,
            sha="sha-reset",
            last_attempted_at=timezone.now(),
            last_success_at=None,
            last_result="error",
            attempts=1,
        )

        deleted = reset_ci_sha_fetch_state(pr=pr, shas=["sha-reset"])
        self.assertEqual(deleted, 1)
        self.assertFalse(CIShaFetchState.objects.filter(repository=repo, sha="sha-reset").exists())
        self.assertTrue(CIShaFetchState.objects.filter(repository=other_repo, sha="sha-reset").exists())
