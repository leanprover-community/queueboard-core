"""GET and HEAD requests for a legacy host name are redirected to QUEUEBOARD_BASE_URL's host."""

from __future__ import annotations

import os
import runpy
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import MiddlewareNotUsed
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from core.middleware import LegacyHostRedirectMiddleware
from qb_site.settings import base as base_settings

MIDDLEWARE_PATH = "core.middleware.LegacyHostRedirectMiddleware"
LEGACY = "old.example.org"
CANONICAL = "https://new.example.org"


def _passed_through(_request) -> HttpResponse:
    return HttpResponse("served", status=200)


@override_settings(
    QUEUEBOARD_LEGACY_HOSTS=[LEGACY, "other-old.example.org"],
    QUEUEBOARD_BASE_URL=CANONICAL,
    QUEUEBOARD_LEGACY_HOST_REDIRECT_PERMANENT=False,
)
class LegacyHostRedirectMiddlewareTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    def _middleware(self) -> LegacyHostRedirectMiddleware:
        return LegacyHostRedirectMiddleware(_passed_through)

    def _call(self, method: str, path: str, host: str, **extra):
        request = getattr(self.factory, method.lower())(path, HTTP_HOST=host, **extra)
        return self._middleware()(request)

    def test_get_redirects_to_same_path_and_query(self) -> None:
        resp = self._call("GET", "/console/preferences/?repo=a/b&x=1%202", LEGACY)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{CANONICAL}/console/preferences/?repo=a/b&x=1%202")

    def test_head_redirects(self) -> None:
        resp = self._call("HEAD", "/api/health", LEGACY)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{CANONICAL}/api/health")

    def test_every_listed_host_redirects(self) -> None:
        resp = self._call("GET", "/", "other-old.example.org")

        self.assertEqual(resp["Location"], f"{CANONICAL}/")

    def test_host_matches_without_port_case_or_trailing_dot(self) -> None:
        for host in ("OLD.Example.ORG", f"{LEGACY}:443", f"{LEGACY}."):
            with self.subTest(host=host):
                resp = self._call("GET", "/api/", host)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp["Location"], f"{CANONICAL}/api/")

    def test_other_methods_are_served(self) -> None:
        # Webhook senders don't follow redirects, and analytics beacons are POSTs.
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                resp = self._call(method, "/webhooks/github/", LEGACY)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.content, b"served")

    def test_other_hosts_are_served(self) -> None:
        for host in ("new.example.org", "localhost:8000", "127.0.0.1", "sub.old.example.org", "old.example.org.evil"):
            with self.subTest(host=host):
                resp = self._call("GET", "/api/health", host)
                self.assertEqual(resp.status_code, 200)

    def test_missing_or_invalid_host_is_served(self) -> None:
        request = self.factory.get("/api/")  # SERVER_NAME only, no Host header
        self.assertNotIn("HTTP_HOST", request.META)
        self.assertEqual(self._middleware()(request).status_code, 200)
        self.assertEqual(self._call("GET", "/api/", "bad host").status_code, 200)

    def test_target_never_comes_from_the_request(self) -> None:
        # A path that looks like a network path, and a forged forwarded host, change nothing but the path.
        # (PATH_INFO directly: RequestFactory would read "//evil.example.com" as a host.)
        resp = self._call("GET", "/", LEGACY, PATH_INFO="//evil.example.com/x", HTTP_X_FORWARDED_HOST="evil.example.com")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(f"{CANONICAL}/"), resp["Location"])
        self.assertEqual(resp["Location"], f"{CANONICAL}//evil.example.com/x")

    @override_settings(QUEUEBOARD_LEGACY_HOST_REDIRECT_PERMANENT=True)
    def test_permanent_setting_makes_it_a_301(self) -> None:
        resp = self._call("GET", "/api/", LEGACY)

        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp["Location"], f"{CANONICAL}/api/")

    @override_settings(QUEUEBOARD_BASE_URL="http://new.example.org:8443/ignored/path")
    def test_only_the_base_urls_scheme_and_host_are_used(self) -> None:
        resp = self._call("GET", "/api/?a=1", LEGACY)

        self.assertEqual(resp["Location"], "http://new.example.org:8443/api/?a=1")

    @override_settings(QUEUEBOARD_LEGACY_HOSTS=[])
    def test_unset_legacy_hosts_disables_it(self) -> None:
        with self.assertRaises(MiddlewareNotUsed), self.assertNoLogs("core.middleware"):
            self._middleware()

    @override_settings(QUEUEBOARD_LEGACY_HOSTS=[" ", "bad host"])
    def test_no_valid_legacy_host_disables_it(self) -> None:
        with self.assertRaises(MiddlewareNotUsed):
            self._middleware()

    def test_unusable_base_url_disables_it_with_a_warning(self) -> None:
        for base_url in ("", "new.example.org", "ftp://new.example.org", "https://user@new.example.org"):
            with self.subTest(base_url=base_url), override_settings(QUEUEBOARD_BASE_URL=base_url):
                with self.assertRaises(MiddlewareNotUsed), self.assertLogs("core.middleware", "WARNING") as logs:
                    self._middleware()
                self.assertIn("not redirecting", logs.output[0])

    def test_base_url_naming_a_legacy_host_disables_it(self) -> None:
        # It would redirect to itself.
        for base_url in (f"https://{LEGACY}", "https://Other-Old.example.org:8443"):
            with self.subTest(base_url=base_url), override_settings(QUEUEBOARD_BASE_URL=base_url):
                with self.assertRaises(MiddlewareNotUsed), self.assertLogs("core.middleware", "WARNING"):
                    self._middleware()


@override_settings(QUEUEBOARD_LEGACY_HOSTS=[LEGACY], QUEUEBOARD_BASE_URL=CANONICAL)
class LegacyHostRedirectStackTests(SimpleTestCase):
    """Through the whole middleware stack. SimpleTestCase fails any database query."""

    def test_redirect_sets_no_cookie(self) -> None:
        resp = self.client.get("/api/?x=1", HTTP_HOST=LEGACY)

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"{CANONICAL}/api/?x=1")
        self.assertEqual(dict(resp.cookies), {})

    def test_post_to_legacy_host_is_served(self) -> None:
        resp = self.client.post("/api/", HTTP_HOST=LEGACY)

        self.assertEqual(resp.status_code, 200)

    def test_canonical_and_local_hosts_are_served(self) -> None:
        for host in ("new.example.org", "localhost"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/", HTTP_HOST=host).status_code, 200)


class MiddlewareOrderTests(SimpleTestCase):
    def test_redirect_comes_first(self) -> None:
        self.assertEqual(settings.MIDDLEWARE[0], MIDDLEWARE_PATH)

    def test_production_keeps_it_first_and_whitenoise_after_security(self) -> None:
        # production.py refuses to load without a secret key and allowed hosts; it reads both from base.
        # Its own settings come from the environment, which here may be .env.example's blanks.
        environ = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_SECURE_")}
        with (
            mock.patch.object(base_settings, "SECRET_KEY", "test-secret"),
            mock.patch.object(base_settings, "ALLOWED_HOSTS", ["example.org"]),
            mock.patch.dict(os.environ, environ, clear=True),
        ):
            production = runpy.run_path(
                str(Path(base_settings.__file__).with_name("production.py")),
                run_name="qb_site.settings._production_under_test",
            )

        self.assertEqual(
            production["MIDDLEWARE"][:3],
            [
                MIDDLEWARE_PATH,
                "django.middleware.security.SecurityMiddleware",
                "whitenoise.middleware.WhiteNoiseMiddleware",
            ],
        )
        self.assertEqual(production["MIDDLEWARE"][3:], base_settings.MIDDLEWARE[2:])
