from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase


class TestZulipPolicyCommand(SimpleTestCase):
    def _init_policy_template(self, *, temp_dir: str) -> dict:
        path = Path(temp_dir) / "template_policy.json"
        call_command("zulip_policy", "init", str(path))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_init_writes_template_from_registered_commands(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            out = io.StringIO()

            call_command("zulip_policy", "init", str(path), stdout=out)

            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("help", payload)
            self.assertGreaterEqual(len(payload), 1)
            self.assertEqual(payload["help"]["allowed_contexts"], ["dm"])
            self.assertEqual(payload["help"]["allowed_groups"], [1234])

    def test_init_refuses_to_overwrite(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesMessage(CommandError, "refusing to overwrite existing file"):
                call_command("zulip_policy", "init", str(path))

    def test_validate_warns_on_missing_command_entry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            template = self._init_policy_template(temp_dir=temp_dir)
            missing_candidates = sorted(name for name in template if name != "help")
            self.assertTrue(missing_candidates, "Expected at least one non-help command to exist")
            removed_command = missing_candidates[0]
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "help": {
                            "allowed_groups": [1234],
                            "allowed_contexts": ["dm"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("zulip_policy", "validate", str(path), stdout=out)

            output = out.getvalue()
            self.assertIn("Valid policy", output)
            self.assertIn("Missing entries", output)
            self.assertIn(removed_command, output)

    def test_validate_rejects_unknown_command(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "not-a-command": {
                            "allowed_groups": [1234],
                            "allowed_contexts": ["dm"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesMessage(CommandError, "unknown command in policy"):
                call_command("zulip_policy", "validate", str(path))

    def test_validate_accepts_all_markers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "help": {
                            "allowed_groups": ["all"],
                            "allowed_contexts": ["all"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("zulip_policy", "validate", str(path), stdout=out)
            self.assertIn("Valid policy", out.getvalue())

    def test_sync_adds_missing_registered_commands(self) -> None:
        with TemporaryDirectory() as temp_dir:
            template = self._init_policy_template(temp_dir=temp_dir)
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "help": {
                            "allowed_groups": [1234],
                            "allowed_contexts": ["dm"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("zulip_policy", "sync", str(path), stdout=out)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload.keys()), set(template.keys()))
            self.assertEqual(payload["help"]["allowed_groups"], [1234])
            self.assertIn("new command entries", out.getvalue())

    def test_sync_noops_when_policy_is_up_to_date(self) -> None:
        with TemporaryDirectory() as temp_dir:
            template = self._init_policy_template(temp_dir=temp_dir)
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(template),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("zulip_policy", "sync", str(path), stdout=out)

            self.assertIn("already up to date", out.getvalue())

    def test_to_env_export_prints_assignment(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.json"
            path.write_text(
                json.dumps(
                    {
                        "echo": {
                            "allowed_groups": [1234],
                            "allowed_contexts": ["dm"],
                        },
                        "help": {
                            "allowed_groups": [1234],
                            "allowed_contexts": ["dm"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()

            call_command("zulip_policy", "to-env", str(path), "--export", stdout=out)

            output = out.getvalue().strip()
            self.assertTrue(output.startswith("ZULIP_COMMAND_POLICY='{"))
            self.assertTrue(output.endswith("}'"))
            self.assertIn('"echo"', output)
            self.assertIn('"help"', output)
