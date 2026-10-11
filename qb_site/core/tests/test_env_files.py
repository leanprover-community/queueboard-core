from __future__ import annotations

import json
import os
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.test import SimpleTestCase

from qb_site.settings import base as base_settings
from qb_site.settings.env_files import FILE_BACKED_ENV_VARS, resolve_env_files

# A Django-generator-style key: characters some env-file formats interpolate (`$`), cut (`#`) or strip (quotes).
SECRET = "k$HOME#x'y\"z!@%^&*(-_=+)"
MARKER = "VALUE-MARKER-0f3c"


class TempFilesTestCase(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, content: str | bytes) -> str:
        path = self.dir / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return str(path)


class ResolveEnvFilesTests(TempFilesTestCase):
    def test_value_read_from_file_replaces_the_file_variable(self) -> None:
        env = {"DJANGO_SECRET_KEY_FILE": self.write("key", SECRET + "\n")}

        resolved = resolve_env_files(env)

        self.assertEqual(resolved, ["DJANGO_SECRET_KEY"])
        self.assertEqual(env, {"DJANGO_SECRET_KEY": SECRET})

    def test_strips_one_trailing_newline_and_nothing_else(self) -> None:
        cases = {
            "secret\n": "secret",
            "secret\r\n": "secret",
            "secret": "secret",
            "secret\n\n": "secret\n",
            "  secret \n": "  secret ",
            "-----BEGIN-----\nabc\n-----END-----\n": "-----BEGIN-----\nabc\n-----END-----",
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                env = {"ZULIP_BOT_API_KEY_FILE": self.write("api_key", content)}
                resolve_env_files(env)
                self.assertEqual(env["ZULIP_BOT_API_KEY"], expected)

    def test_both_set_is_an_error_naming_the_variable_but_no_value(self) -> None:
        env = {
            "GITHUB_WEBHOOK_SECRET": f"env-{MARKER}",
            "GITHUB_WEBHOOK_SECRET_FILE": self.write("webhook", f"file-{MARKER}\n"),
        }

        with self.assertRaises(RuntimeError) as cm:
            resolve_env_files(env)

        message = str(cm.exception)
        self.assertIn("GITHUB_WEBHOOK_SECRET and GITHUB_WEBHOOK_SECRET_FILE are both set", message)
        self.assertNotIn(MARKER, message)

    def test_empty_value_counts_as_unset(self) -> None:
        # A template's blank `NAME=` does not conflict with NAME_FILE...
        env = {"DJANGO_DB_PASSWORD": "", "DJANGO_DB_PASSWORD_FILE": self.write("password", "pw\n")}
        resolve_env_files(env)
        self.assertEqual(env, {"DJANGO_DB_PASSWORD": "pw"})

        # ...and a blank NAME_FILE is ignored.
        env = {"DJANGO_DB_PASSWORD": "pw", "DJANGO_DB_PASSWORD_FILE": ""}
        self.assertEqual(resolve_env_files(env), [])
        self.assertEqual(env, {"DJANGO_DB_PASSWORD": "pw", "DJANGO_DB_PASSWORD_FILE": ""})

    def test_missing_file_is_an_error_naming_variable_and_path(self) -> None:
        missing = str(self.dir / "missing")
        env = {"ZULIP_USER_API_KEY_FILE": missing}

        with self.assertRaises(RuntimeError) as cm:
            resolve_env_files(env)

        message = str(cm.exception)
        self.assertIn("ZULIP_USER_API_KEY_FILE", message)
        self.assertIn(missing, message)
        self.assertNotIn("ZULIP_USER_API_KEY", env)

    def test_directory_is_an_error(self) -> None:
        with self.assertRaisesMessage(RuntimeError, f"SITE_ANALYTICS_HASH_SALT_FILE: cannot read {self.dir}"):
            resolve_env_files({"SITE_ANALYTICS_HASH_SALT_FILE": str(self.dir)})

    def test_bad_contents_are_errors_that_do_not_quote_the_value(self) -> None:
        cases = {
            b"\xff" + MARKER.encode(): "is not valid UTF-8",
            MARKER.encode() + b"\x00": "contains a NUL byte",
        }
        for content, reason in cases.items():
            with self.subTest(reason=reason):
                path = self.write("bad", content)
                with self.assertRaises(RuntimeError) as cm:
                    resolve_env_files({"ZULIP_LINK_TOKEN_SECRET_FILE": path})

                message = str(cm.exception)
                self.assertIn(f"ZULIP_LINK_TOKEN_SECRET_FILE: {path} {reason}", message)
                self.assertNotIn(MARKER, message)
                # A chained decode error would hold the file's bytes, even when not displayed.
                self.assertIsNone(cm.exception.__cause__)
                self.assertIsNone(cm.exception.__context__)

    def test_empty_file_is_an_error(self) -> None:
        for content in ("", "\n", "\r\n"):
            with self.subTest(content=content):
                path = self.write("empty", content)
                env = {"DJANGO_SECRET_KEY_FILE": path}

                with self.assertRaisesMessage(RuntimeError, f"DJANGO_SECRET_KEY_FILE: {path} is empty"):
                    resolve_env_files(env)
                self.assertNotIn("DJANGO_SECRET_KEY", env)

    def test_names_outside_the_allowlist_are_ignored(self) -> None:
        env = {"DJANGO_DB_HOST_FILE": self.write("host", "db\n"), "SSL_CERT_FILE": "/etc/ssl/cert.pem"}
        before = dict(env)

        self.assertEqual(resolve_env_files(env), [])
        self.assertEqual(env, before)

    def test_plain_environment_is_left_alone(self) -> None:
        env = {name: f"value-of-{name}" for name in FILE_BACKED_ENV_VARS}
        before = dict(env)

        self.assertEqual(resolve_env_files(env), [])
        self.assertEqual(env, before)

    def test_a_second_run_sees_only_the_value(self) -> None:
        # What a child process inheriting the environment (runserver's autoreloader) sees.
        env = {"GH_TOKEN_FILE": self.write("token", "tok\n")}
        resolve_env_files(env)

        self.assertEqual(resolve_env_files(env), [])
        self.assertEqual(env, {"GH_TOKEN": "tok"})

    def test_defaults_to_os_environ(self) -> None:
        with mock.patch.dict(os.environ, {"ZULIP_WEBHOOK_TOKEN_FILE": self.write("token", "tok\n")}):
            os.environ.pop("ZULIP_WEBHOOK_TOKEN", None)
            resolve_env_files()

            self.assertEqual(os.environ["ZULIP_WEBHOOK_TOKEN"], "tok")
            self.assertNotIn("ZULIP_WEBHOOK_TOKEN_FILE", os.environ)


class SettingsFromFilesTests(TempFilesTestCase):
    """Run base.py afresh under a given environment, as a process start would."""

    def load_base(self, **env: str) -> tuple[dict[str, object], dict[str, str]]:
        with mock.patch.dict(os.environ):
            for name in FILE_BACKED_ENV_VARS:
                os.environ.pop(name, None)
                os.environ.pop(f"{name}_FILE", None)
            os.environ.update(env)
            settings = runpy.run_path(base_settings.__file__, run_name="qb_site.settings._base_under_test")
            return settings, dict(os.environ)

    def test_json_settings_from_files(self) -> None:
        app_config = {
            "operation_app_map": {"assign_pr": "queueboard-assignment"},
            "apps": [
                {
                    "name": "queueboard-assignment",
                    "app_id": 123456,
                    "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
                    "operations": ["assign_pr"],
                }
            ],
        }
        policy = {"assign": {"allow_user_ids": [1, 2]}}
        repo_log = {"leanprover-community/mathlib4": {"stream": "mathlib4", "topic": "bot actions"}}

        settings, _ = self.load_base(
            GITHUB_APP_TOKEN_CONFIG_FILE=self.write("app.json", json.dumps(app_config, indent=2) + "\n"),
            ZULIP_COMMAND_POLICY_FILE=self.write("policy.json", json.dumps(policy, indent=2) + "\n"),
            ZULIP_REPO_LOG_FILE=self.write("repo_log.json", json.dumps(repo_log) + "\n"),
        )

        self.assertEqual(settings["GITHUB_APP_TOKEN_CONFIG"], app_config)
        self.assertEqual(settings["ZULIP_COMMAND_POLICY"], policy)
        self.assertEqual(settings["ZULIP_REPO_LOG"], repo_log)

    def test_scalar_settings_from_files(self) -> None:
        settings, environ = self.load_base(
            DJANGO_SECRET_KEY_FILE=self.write("secret_key", SECRET + "\n"),
            DJANGO_DB_PASSWORD_FILE=self.write("db_password", "p@ss#word\n"),
            CELERY_BROKER_URL_FILE=self.write("broker", "redis://:p%40ss@redis:6379/0\n"),
        )

        self.assertEqual(settings["SECRET_KEY"], SECRET)
        self.assertEqual(settings["DATABASES"]["default"]["PASSWORD"], "p@ss#word")
        self.assertEqual(settings["CELERY_BROKER_URL"], "redis://:p%40ss@redis:6379/0")
        # The result backend still falls back to the broker URL.
        self.assertEqual(settings["CELERY_RESULT_BACKEND"], "redis://:p%40ss@redis:6379/0")
        # Celery reads CELERY_BROKER_URL from os.environ itself, ahead of the settings.
        self.assertEqual(environ["CELERY_BROKER_URL"], "redis://:p%40ss@redis:6379/0")
        self.assertNotIn("CELERY_BROKER_URL_FILE", environ)

    def test_plain_environment_variables_unchanged(self) -> None:
        app_config = {"apps": [{"name": "a", "app_id": 1, "private_key_path": "/k.pem", "operations": []}]}

        settings, _ = self.load_base(
            DJANGO_SECRET_KEY="plain-key",
            GITHUB_APP_TOKEN_CONFIG=json.dumps(app_config),
            ZULIP_BOT_API_KEY="plain-api-key",
        )

        self.assertEqual(settings["SECRET_KEY"], "plain-key")
        self.assertEqual(settings["GITHUB_APP_TOKEN_CONFIG"], app_config)
        self.assertEqual(settings["ZULIP_BOT_API_KEY"], "plain-api-key")
        self.assertEqual(settings["ZULIP_COMMAND_POLICY"], {})

    def test_both_set_fails_at_import(self) -> None:
        with self.assertRaisesMessage(RuntimeError, "GITHUB_APP_TOKEN_CONFIG and GITHUB_APP_TOKEN_CONFIG_FILE are both set"):
            self.load_base(
                GITHUB_APP_TOKEN_CONFIG="{}",
                GITHUB_APP_TOKEN_CONFIG_FILE=self.write("app.json", "{}\n"),
            )
