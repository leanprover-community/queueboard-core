"""The production settings: .env.example's blank HSTS toggles, and WhiteNoise's static files storage."""

from __future__ import annotations

import os
import re
import runpy
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.templatetags.static import static
from django.test import SimpleTestCase, override_settings

from qb_site.settings import base as base_settings

HSTS_VARS = ("DJANGO_SECURE_HSTS_SECONDS", "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", "DJANGO_SECURE_HSTS_PRELOAD")


def _load_production(**hsts: str) -> dict:
    """Evaluate settings/production.py afresh with only these HSTS variables set."""
    environ = {k: v for k, v in os.environ.items() if k not in HSTS_VARS}
    environ.update(hsts)
    # production.py refuses to load without a secret key and allowed hosts; it reads both from base.
    with (
        mock.patch.object(base_settings, "SECRET_KEY", "test-secret"),
        mock.patch.object(base_settings, "ALLOWED_HOSTS", ["example.org"]),
        mock.patch.dict(os.environ, environ, clear=True),
    ):
        return runpy.run_path(
            str(Path(base_settings.__file__).with_name("production.py")),
            run_name="qb_site.settings._production_under_test",
        )


class HstsSettingTests(SimpleTestCase):
    def test_unset_or_blank_means_no_hsts(self) -> None:
        cases = [{}, dict.fromkeys(HSTS_VARS, ""), dict.fromkeys(HSTS_VARS, "  ")]
        for hsts in cases:
            with self.subTest(hsts=hsts):
                production = _load_production(**hsts)
                self.assertEqual(production["SECURE_HSTS_SECONDS"], 0)
                self.assertFalse(production["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
                self.assertFalse(production["SECURE_HSTS_PRELOAD"])

    def test_set_values_are_used(self) -> None:
        production = _load_production(
            DJANGO_SECURE_HSTS_SECONDS=" 31536000 ",
            DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS="true",
            DJANGO_SECURE_HSTS_PRELOAD="1",
        )
        self.assertEqual(production["SECURE_HSTS_SECONDS"], 31536000)
        self.assertTrue(production["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertTrue(production["SECURE_HSTS_PRELOAD"])

    def test_seconds_not_an_integer_refuses_to_start(self) -> None:
        for value in ("ten", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _load_production(DJANGO_SECURE_HSTS_SECONDS=value)


STATIC_TAG = re.compile(r"""{%\s*static\s+['"]([^'"]+)['"]""")


class StaticFilesStorageTests(SimpleTestCase):
    def test_whitenoise_is_named_in_storages(self) -> None:
        production = _load_production()
        self.assertEqual(
            production["STORAGES"],
            {
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
            },
        )
        # Django 5.1 removed STATICFILES_STORAGE: it would be ignored.
        self.assertNotIn("STATICFILES_STORAGE", production)

    def test_collected_files_are_hashed_and_compressed(self) -> None:
        # The manifest makes {% static %} raise for a name that wasn't collected, so each one in the
        # templates must be.
        names = sorted(
            {
                name
                for path in (base_settings.BASE_DIR / "templates").rglob("*.html")
                for name in STATIC_TAG.findall(path.read_text())
            }
        )
        self.assertIn("shared/shared_pages.css", names)
        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(STORAGES=_load_production()["STORAGES"], STATIC_ROOT=root),
        ):
            call_command("collectstatic", interactive=False, verbosity=0)
            for name in [*names, "admin/css/base.css"]:
                with self.subTest(name=name):
                    stem, ext = os.path.splitext(name)
                    url = static(name)
                    self.assertRegex(url, rf"^/static/{re.escape(stem)}\.[0-9a-f]{{12}}{re.escape(ext)}$")
                    self.assertTrue(Path(root, name).is_file())
            # The plain names stay, as the forms' modules import each other by them.
            self.assertTrue(Path(root, "zulip_bot/expiry.js").is_file())
            self.assertTrue(Path(root, static("admin/css/base.css").removeprefix("/static/") + ".gz").is_file())
