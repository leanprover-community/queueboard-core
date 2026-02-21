from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase


class TestGitHubAppConfigCommand(SimpleTestCase):
    def test_init_writes_template(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "github_app_config.json"
            out = io.StringIO()

            call_command("github_app_config", "init", str(path), stdout=out)

            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("apps", payload)
            self.assertGreaterEqual(len(payload["apps"]), 1)
            self.assertIn("operation_app_map", payload)

    def test_inline_keys_reads_private_key_paths_and_writes_inline_private_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            key_path = base / "assignment.pem"
            key_path.write_text(
                "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----\n",
                encoding="utf-8",
            )
            config_path = base / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "apps": [
                            {
                                "name": "queueboard-assignment",
                                "app_id": 123456,
                                "private_key_path": "assignment.pem",
                                "operations": ["assign_pr", "unassign_pr"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            call_command("github_app_config", "inline-keys", str(config_path), "--in-place")

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            app = payload["apps"][0]
            self.assertIn("private_key", app)
            self.assertNotIn("private_key_path", app)
            self.assertIn("BEGIN PRIVATE KEY", app["private_key"])

    def test_to_env_export_prints_shell_assignment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "apps": [
                            {
                                "name": "queueboard-assignment",
                                "app_id": 123456,
                                "private_key": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
                                "operations": ["assign_pr", "unassign_pr"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("github_app_config", "to-env", str(path), "--export", stdout=out)

            output = out.getvalue().strip()
            self.assertTrue(output.startswith("GITHUB_APP_TOKEN_CONFIG='{"))
            self.assertTrue(output.endswith("}'"))
            self.assertIn('"apps"', output)

    def test_validate_rejects_unknown_mapped_app(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "operation_app_map": {"assign_pr": "missing-app"},
                        "apps": [
                            {
                                "name": "queueboard-assignment",
                                "app_id": 123456,
                                "private_key": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
                                "operations": ["assign_pr", "unassign_pr"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesMessage(CommandError, "operation_app_map references unknown app"):
                call_command("github_app_config", "validate", str(path))

    def test_validate_accepts_owner_installation_lookup_fields(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "apps": [
                            {
                                "name": "queueboard-syncer-read",
                                "app_id": 234567,
                                "private_key": "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
                                "installation_lookup": "owner",
                                "installation_owner_type": "org",
                                "installation_owner": "leanprover-community",
                                "operations": ["syncer_pr_read"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("github_app_config", "validate", str(path), stdout=out)

            self.assertIn("Valid config", out.getvalue())
