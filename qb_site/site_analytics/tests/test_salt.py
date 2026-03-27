"""Tests for the monthly salt rotation task."""

from __future__ import annotations

from django.test import TestCase

from site_analytics.models.salt import SiteAnalyticsSalt
from site_analytics.services.hashing import _reset_salt_cache
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
