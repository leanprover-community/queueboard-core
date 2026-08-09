"""Tests for the monthly salt rotation task."""

from __future__ import annotations

from django.test import TestCase, override_settings

from site_analytics.models.salt import SiteAnalyticsSalt
from site_analytics.services.hashing import SaltUnavailable, _reset_salt_cache, compute_visitor_hash
from site_analytics.tasks.rotate_salt import rotate_salt_task


class RotateSaltTaskTests(TestCase):
    def setUp(self):
        _reset_salt_cache()

    def test_creates_salt_row_when_none_exists(self):
        self.assertEqual(SiteAnalyticsSalt.objects.count(), 0)
        rotate_salt_task()
        self.assertEqual(SiteAnalyticsSalt.objects.count(), 1)

    def test_created_salt_is_nonempty(self):
        rotate_salt_task()
        self.assertTrue(SiteAnalyticsSalt.objects.get().salt)

    def test_replaces_existing_salt(self):
        SiteAnalyticsSalt.objects.create(salt="old-salt")
        rotate_salt_task()
        # Only one row should remain after rotation.
        self.assertEqual(SiteAnalyticsSalt.objects.count(), 1)
        self.assertNotEqual(SiteAnalyticsSalt.objects.get().salt, "old-salt")

    def test_new_salt_differs_from_previous(self):
        SiteAnalyticsSalt.objects.create(salt="old-salt")
        rotate_salt_task()
        self.assertNotEqual(SiteAnalyticsSalt.objects.get().salt, "old-salt")

    def test_returns_summary_dict(self):
        result = rotate_salt_task()
        self.assertTrue(result["rotated"])
        self.assertIn("old_deleted", result)

    def test_old_deleted_count_is_accurate(self):
        SiteAnalyticsSalt.objects.create(salt="first")
        SiteAnalyticsSalt.objects.create(salt="second")
        result = rotate_salt_task()
        # Both pre-existing rows should have been deleted.
        self.assertEqual(result["old_deleted"], 2)
        self.assertEqual(SiteAnalyticsSalt.objects.count(), 1)


@override_settings(SITE_ANALYTICS_HASH_SALT="")
class MissingSaltFailsClosedTests(TestCase):
    """No salt anywhere must raise rather than produce an unsalted hash."""

    def setUp(self):
        _reset_salt_cache()

    def tearDown(self):
        _reset_salt_cache()

    def test_hashing_raises_when_no_salt_available(self):
        self.assertEqual(SiteAnalyticsSalt.objects.count(), 0)
        with self.assertRaises(SaltUnavailable):
            compute_visitor_hash("1.2.3.4", "Mozilla/5.0")

    def test_hashing_recovers_once_a_salt_row_exists(self):
        with self.assertRaises(SaltUnavailable):
            compute_visitor_hash("1.2.3.4", "Mozilla/5.0")
        SiteAnalyticsSalt.objects.create(salt="fresh-salt")
        _reset_salt_cache()
        self.assertEqual(len(compute_visitor_hash("1.2.3.4", "Mozilla/5.0")), 64)

    def test_empty_salt_result_is_cached_rather_than_requeried(self):
        # A misconfigured deployment must not issue a DB query per request.
        with self.assertRaises(SaltUnavailable):
            compute_visitor_hash("1.2.3.4", "Mozilla/5.0")
        with self.assertNumQueries(0):
            for _ in range(5):
                with self.assertRaises(SaltUnavailable):
                    compute_visitor_hash("1.2.3.4", "Mozilla/5.0")

    @override_settings(SITE_ANALYTICS_HASH_SALT="env-fallback-salt")
    def test_env_salt_used_as_bootstrap_when_no_row_exists(self):
        self.assertEqual(len(compute_visitor_hash("1.2.3.4", "Mozilla/5.0")), 64)
