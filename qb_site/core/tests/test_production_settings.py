"""The production settings load with .env.example's blank HSTS toggles."""

from __future__ import annotations

import os
import runpy
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

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
