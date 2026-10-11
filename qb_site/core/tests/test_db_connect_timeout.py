"""Opening a database connection gives up after DJANGO_DB_CONNECT_TIMEOUT seconds."""

from __future__ import annotations

import os
import runpy
import socket
import threading
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db import OperationalError, connections
from django.test import SimpleTestCase

from qb_site.settings import base as base_settings


def _load_settings(connect_timeout: str | None) -> dict:
    """Evaluate settings/base.py afresh with DJANGO_DB_CONNECT_TIMEOUT set to this, or unset."""
    environ = {k: v for k, v in os.environ.items() if k != "DJANGO_DB_CONNECT_TIMEOUT"}
    if connect_timeout is not None:
        environ["DJANGO_DB_CONNECT_TIMEOUT"] = connect_timeout
    with mock.patch.dict(os.environ, environ, clear=True):
        # A dotted run_name gives the module a package, so base.py's relative imports resolve.
        return runpy.run_path(str(Path(base_settings.__file__)), run_name="qb_site.settings._base_under_test")


def _connect_timeout(value: str | None) -> object:
    return _load_settings(value)["DATABASES"]["default"]["OPTIONS"]["connect_timeout"]


class ConnectTimeoutSettingTests(SimpleTestCase):
    def test_default_is_ten_seconds(self) -> None:
        for value in (None, "", "  "):
            with self.subTest(value=value):
                self.assertEqual(_connect_timeout(value), 10)

    def test_set_value_is_used(self) -> None:
        self.assertEqual(_connect_timeout("2"), 2)
        self.assertEqual(_connect_timeout(" 30 "), 30)

    def test_out_of_range_or_not_an_integer_refuses_to_start(self) -> None:
        # Below 2, psycopg would quietly use 2, or for 0 and less its own 130 s default.
        for value in ("0", "1", "-5", "601", "ten", "2.5"):
            with self.subTest(value=value), self.assertRaisesMessage(RuntimeError, "DJANGO_DB_CONNECT_TIMEOUT"):
                _load_settings(value)

    def test_active_settings_carry_it(self) -> None:
        self.assertEqual(settings.DATABASES["default"]["OPTIONS"]["connect_timeout"], settings.DJANGO_DB_CONNECT_TIMEOUT)


class ConnectTimeoutBehaviourTests(SimpleTestCase):
    """Against a real socket, through Django's own connection code."""

    def test_silent_server_fails_within_the_timeout(self) -> None:
        # The kernel completes the handshake for a listening socket nobody accepts from, so the
        # client connects and then never hears back: a hung Postgres.
        listener = socket.create_server(("127.0.0.1", 0), backlog=8)
        self.addCleanup(listener.close)
        # If the timeout ever stops reaching psycopg, this turns its 130 s wait into a slow failure.
        safety = threading.Timer(15, listener.close)
        safety.start()
        self.addCleanup(safety.cancel)
        port = listener.getsockname()[1]

        default = connections["default"]
        # The configured OPTIONS, with the shortest timeout psycopg allows, so the test stays quick.
        options = {**default.settings_dict["OPTIONS"], "connect_timeout": 2}
        silent = default.__class__(
            {**default.settings_dict, "HOST": "127.0.0.1", "PORT": str(port), "OPTIONS": options}, alias="silent"
        )
        self.addCleanup(silent.close)

        started = time.monotonic()
        with self.assertRaises(OperationalError):
            silent.ensure_connection()
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 1.5)
        self.assertLess(elapsed, 6)
