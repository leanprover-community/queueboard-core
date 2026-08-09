"""Tests for the site_analytics deploy-time configuration checks."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from site_analytics.checks import SALT_MISSING_ID, check_hash_salt_configured


class HashSaltCheckTests(SimpleTestCase):
    """SimpleTestCase forbids database access, so every test here also proves the
    check touches no tables — it must stay runnable during `manage.py migrate`,
    before SiteAnalyticsSalt exists."""

    @override_settings(SITE_ANALYTICS_ALLOWED_SITES=["queueboard"], SITE_ANALYTICS_HASH_SALT="")
    def test_error_when_ingestion_enabled_without_salt(self):
        errors = check_hash_salt_configured(None)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].id, SALT_MISSING_ID)

    @override_settings(SITE_ANALYTICS_ALLOWED_SITES=["queueboard"], SITE_ANALYTICS_HASH_SALT="   ")
    def test_whitespace_only_salt_is_treated_as_missing(self):
        self.assertEqual(len(check_hash_salt_configured(None)), 1)

    @override_settings(SITE_ANALYTICS_ALLOWED_SITES=["queueboard"], SITE_ANALYTICS_HASH_SALT="s3cret")
    def test_no_error_when_salt_present(self):
        self.assertEqual(check_hash_salt_configured(None), [])

    @override_settings(SITE_ANALYTICS_ALLOWED_SITES=[], SITE_ANALYTICS_HASH_SALT="")
    def test_no_error_when_ingestion_disabled(self):
        # Analytics is opt-in: a deployment with no allowed sites accepts no events,
        # so it must not be forced to configure a salt (this is the CI/dev case).
        self.assertEqual(check_hash_salt_configured(None), [])

    @override_settings(SITE_ANALYTICS_ALLOWED_SITES=["queueboard"], SITE_ANALYTICS_HASH_SALT="")
    def test_check_does_not_touch_the_database(self):
        # Under SimpleTestCase any query raises DatabaseOperationForbidden, so reaching
        # the assertion at all proves the check is settings-only. Guards against someone
        # later "improving" it by consulting SiteAnalyticsSalt, which would make the
        # first deploy unbootable: migrate runs checks before creating that table.
        errors = check_hash_salt_configured(None)
        self.assertEqual(errors[0].id, SALT_MISSING_ID)
