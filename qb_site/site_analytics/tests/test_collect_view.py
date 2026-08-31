"""Endpoint tests for POST /api/v1/analytics/collect."""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from site_analytics.models import AnalyticsPageView
from site_analytics.services.hashing import _reset_salt_cache

_ALLOWED = override_settings(
    SITE_ANALYTICS_ALLOWED_SITES=["test-site"],
    SITE_ANALYTICS_HASH_SALT="test-salt",
)

URL = "/api/v1/analytics/collect"


@_ALLOWED
class AnalyticsCollectViewTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        _reset_salt_cache()

    def _post(self, data: dict, **kwargs) -> object:
        return self.client.post(URL, data, format="json", **kwargs)

    # --- success path ---

    def test_valid_payload_returns_204(self):
        resp = self._post({"site": "test-site", "path": "/about"})
        self.assertEqual(resp.status_code, 204)

    def test_valid_payload_creates_pageview_row(self):
        self._post({"site": "test-site", "path": "/about", "referrer": "https://example.com"})
        pv = AnalyticsPageView.objects.get()
        self.assertEqual(pv.site, "test-site")
        self.assertEqual(pv.path, "/about")
        self.assertEqual(pv.referrer, "https://example.com")
        self.assertEqual(len(pv.visitor_month_hash), 64)

    def test_referrer_optional(self):
        resp = self._post({"site": "test-site", "path": "/home"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(AnalyticsPageView.objects.get().referrer, "")

    def test_user_agent_captured_from_header(self):
        self._post(
            {"site": "test-site", "path": "/"},
            HTTP_USER_AGENT="Mozilla/5.0 (Test)",
        )
        self.assertEqual(AnalyticsPageView.objects.get().user_agent, "Mozilla/5.0 (Test)")

    # --- validation errors ---

    def test_missing_site_returns_400(self):
        resp = self._post({"path": "/about"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("site", resp.json()["detail"])

    def test_missing_path_returns_400(self):
        resp = self._post({"site": "test-site"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("path", resp.json()["detail"])

    def test_unknown_site_returns_400(self):
        resp = self._post({"site": "unknown-site", "path": "/about"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("site", resp.json()["detail"])

    def test_empty_site_returns_400(self):
        resp = self._post({"site": "", "path": "/about"})
        self.assertEqual(resp.status_code, 400)

    # --- bot filtering ---

    def test_bot_ua_returns_204_but_no_row(self):
        resp = self._post(
            {"site": "test-site", "path": "/about"},
            HTTP_USER_AGENT="Googlebot/2.1",
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(AnalyticsPageView.objects.count(), 0)

    # --- privacy: no raw IP stored ---

    def test_ip_not_stored_in_row(self):
        self._post(
            {"site": "test-site", "path": "/"},
            REMOTE_ADDR="1.2.3.4",
        )
        pv = AnalyticsPageView.objects.get()
        row_values = [str(v) for v in [pv.site, pv.path, pv.referrer, pv.user_agent, pv.visitor_month_hash]]
        self.assertFalse(any("1.2.3.4" in v for v in row_values))

    # --- XFF extraction ---

    def test_xff_used_for_hash_differs_from_remote_addr(self):
        """Two visitors behind the proxy (distinct appended XFF) → different hashes."""
        self._post(
            {"site": "test-site", "path": "/"},
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="1.1.1.1",
        )
        self._post(
            {"site": "test-site", "path": "/"},
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="2.2.2.2",
        )
        hashes = list(AnalyticsPageView.objects.values_list("visitor_month_hash", flat=True))
        self.assertEqual(len(hashes), 2)
        self.assertNotEqual(hashes[0], hashes[1])

    def test_client_cannot_inflate_unique_visitors_by_spoofing_xff(self):
        """A client prepending its own XFF entries must still hash to one visitor.

        The proxy appends the real address, so only the rightmost entry is trusted;
        otherwise a single visitor could mint a fresh hash on every request.
        """
        for spoofed in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            self._post(
                {"site": "test-site", "path": "/"},
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR=f"{spoofed}, 203.0.113.9",
                HTTP_USER_AGENT="Mozilla/5.0",
            )
        hashes = set(AnalyticsPageView.objects.values_list("visitor_month_hash", flat=True))
        self.assertEqual(AnalyticsPageView.objects.count(), 3)
        self.assertEqual(len(hashes), 1, "spoofed X-Forwarded-For entries changed the visitor hash")

    # --- field truncation ---

    def test_oversized_path_is_truncated(self):
        long_path = "/" + "a" * 3000
        resp = self._post({"site": "test-site", "path": long_path})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(len(AnalyticsPageView.objects.get().path), 2000)

    # --- empty allowed list ---

    @override_settings(SITE_ANALYTICS_ALLOWED_SITES=[])
    def test_empty_allowed_sites_rejects_all(self):
        resp = self._post({"site": "test-site", "path": "/about"})
        self.assertEqual(resp.status_code, 400)

    # --- empty UA hardening flag ---

    def test_empty_ua_allowed_by_default(self):
        resp = self._post({"site": "test-site", "path": "/"})  # no HTTP_USER_AGENT
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(AnalyticsPageView.objects.count(), 1)

    @override_settings(SITE_ANALYTICS_REJECT_EMPTY_UA=True)
    def test_empty_ua_dropped_when_flag_enabled(self):
        resp = self._post({"site": "test-site", "path": "/"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(AnalyticsPageView.objects.count(), 0)

    @override_settings(SITE_ANALYTICS_REJECT_EMPTY_UA=True)
    def test_non_empty_ua_accepted_when_flag_enabled(self):
        resp = self._post({"site": "test-site", "path": "/"}, HTTP_USER_AGENT="Mozilla/5.0")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(AnalyticsPageView.objects.count(), 1)

    # --- CORS ---

    def test_post_response_includes_cors_header(self):
        resp = self._post({"site": "test-site", "path": "/"}, HTTP_USER_AGENT="Mozilla/5.0")
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    def test_options_preflight_returns_204_with_cors_headers(self):
        resp = self.client.options(URL)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")
        self.assertIn("POST", resp["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", resp["Access-Control-Allow-Headers"])


@override_settings(SITE_ANALYTICS_ALLOWED_SITES=["test-site"], SITE_ANALYTICS_HASH_SALT="")
class AnalyticsCollectMissingSaltTests(TestCase):
    """With no salt configured the endpoint must drop events, not store weak hashes."""

    def setUp(self) -> None:
        self.client = APIClient()
        _reset_salt_cache()

    def tearDown(self) -> None:
        _reset_salt_cache()

    def test_event_is_dropped_when_no_salt_configured(self):
        with self.assertLogs("api.views.analytics_collect", level="ERROR"):
            resp = self.client.post(
                URL,
                {"site": "test-site", "path": "/"},
                format="json",
                HTTP_USER_AGENT="Mozilla/5.0",
            )
        # 204 keeps the browser beacon quiet; the row must not exist.
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(AnalyticsPageView.objects.count(), 0)

    def test_no_unsalted_hash_is_ever_persisted(self):
        import hashlib

        with self.assertLogs("api.views.analytics_collect", level="ERROR"):
            self.client.post(
                URL,
                {"site": "test-site", "path": "/"},
                format="json",
                HTTP_USER_AGENT="Mozilla/5.0",
                REMOTE_ADDR="203.0.113.7",
            )
        unsalted = hashlib.sha256(b"203.0.113.7|mozilla/5.0|").hexdigest()
        self.assertFalse(AnalyticsPageView.objects.filter(visitor_month_hash=unsalted).exists())
