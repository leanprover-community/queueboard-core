from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from zulip_bot.commands import list_commands
from zulip_bot.commands import echo as _echo  # noqa: F401
from zulip_bot.commands import help as _help  # noqa: F401
from zulip_bot.commands import prefs as _prefs  # noqa: F401


class Command(BaseCommand):
    help = "Build and validate Zulip command policy JSON."

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        subparsers = parser.add_subparsers(dest="action", required=True)

        init_parser = subparsers.add_parser("init", help="Create a template policy JSON file.")
        init_parser.add_argument("file", help="Output path (for example: .zulip-policy.local.json)")

        validate_parser = subparsers.add_parser("validate", help="Validate a policy JSON file.")
        validate_parser.add_argument("file", help="Policy JSON file path")

        sync_parser = subparsers.add_parser(
            "sync", help="Append skeleton entries for newly registered commands in an existing policy file."
        )
        sync_parser.add_argument("file", help="Policy JSON file path")

        env_parser = subparsers.add_parser("to-env", help="Print compact JSON for ZULIP_COMMAND_POLICY.")
        env_parser.add_argument("file", help="Policy JSON file path")
        env_parser.add_argument(
            "--export",
            action="store_true",
            help="Print as a shell assignment line.",
        )

    def handle(self, *args, **options) -> str | None:  # type: ignore[override]
        action = options["action"]
        path = Path(options["file"])

        if action == "init":
            self._cmd_init(path)
            return None
        if action == "validate":
            self._cmd_validate(path)
            return None
        if action == "sync":
            self._cmd_sync(path)
            return None
        if action == "to-env":
            self._cmd_to_env(path, export=bool(options.get("export")))
            return None

        raise CommandError(f"unknown action: {action}")

    def _cmd_init(self, path: Path) -> None:
        if path.exists():
            raise CommandError(f"refusing to overwrite existing file: {path}")
        template = self._template_policy()
        path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Created template policy at {path}"))

    def _cmd_validate(self, path: Path) -> None:
        policy = self._read_policy(path)
        known_commands = {command.name for command in list_commands()}
        policy_commands = set(policy.keys())
        missing = sorted(known_commands - policy_commands)

        self.stdout.write(self.style.SUCCESS(f"Valid policy: {path} ({len(policy)} command entries)"))
        if missing:
            self.stdout.write(self.style.WARNING("Missing entries (commands will be denied by default): " + ", ".join(missing)))

    def _cmd_to_env(self, path: Path, *, export: bool) -> None:
        policy = self._read_policy(path)
        compact = json.dumps(policy, separators=(",", ":"), sort_keys=True)
        if export:
            self.stdout.write(f"ZULIP_COMMAND_POLICY='{compact}'")
            return
        self.stdout.write(compact)

    def _cmd_sync(self, path: Path) -> None:
        policy = self._read_policy(path)
        missing = [command.name for command in list_commands() if command.name not in policy]
        if not missing:
            self.stdout.write(self.style.SUCCESS(f"Policy already up to date: {path}"))
            return

        for command_name in missing:
            policy[command_name] = self._skeleton_rule()
        path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Updated policy with {len(missing)} new command entries: {', '.join(missing)}"))

    def _read_policy(self, path: Path) -> dict[str, dict[str, list[Any]]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CommandError(f"file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}") from exc
        return self._validate_policy(raw)

    def _template_policy(self) -> dict[str, dict[str, list[Any]]]:
        template: dict[str, dict[str, list[Any]]] = {}
        for command in list_commands():
            template[command.name] = self._skeleton_rule()
        return template

    def _skeleton_rule(self) -> dict[str, list[Any]]:
        return {
            "allowed_groups": [1234],
            "allowed_contexts": ["dm"],
        }

    def _validate_policy(self, policy: Any) -> dict[str, dict[str, list[Any]]]:
        if not isinstance(policy, dict):
            raise CommandError("policy must be a JSON object keyed by command name")

        known_commands = {command.name for command in list_commands()}
        normalized: dict[str, dict[str, list[Any]]] = {}

        for command, rule in policy.items():
            if not isinstance(command, str) or not command.strip():
                raise CommandError("each command name must be a non-empty string")
            if command not in known_commands:
                raise CommandError(f"unknown command in policy: {command}")
            if not isinstance(rule, dict):
                raise CommandError(f"rule for '{command}' must be an object")

            unknown = sorted(set(rule.keys()) - {"allowed_groups", "allowed_contexts"})
            if unknown:
                raise CommandError(f"rule for '{command}' has unknown keys: {', '.join(unknown)}")

            allowed_groups = rule.get("allowed_groups", [])
            allowed_contexts = rule.get("allowed_contexts", [])

            if not isinstance(allowed_groups, list):
                raise CommandError(f"rule for '{command}'.allowed_groups must be a list")
            if not all(
                (isinstance(item, int) and item > 0) or (isinstance(item, str) and item.lower() in {"*", "all"})
                for item in allowed_groups
            ):
                raise CommandError(f"rule for '{command}'.allowed_groups must contain positive integers or '*'/'all'")

            if not isinstance(allowed_contexts, list):
                raise CommandError(f"rule for '{command}'.allowed_contexts must be a list")
            if not all(isinstance(item, str) and item for item in allowed_contexts):
                raise CommandError(
                    f"rule for '{command}'.allowed_contexts must contain non-empty strings (use '*' or 'all' for unrestricted)"
                )

            normalized[command] = {
                "allowed_groups": allowed_groups,
                "allowed_contexts": allowed_contexts,
            }

        return normalized
