from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Build and validate GitHub App token config JSON (GITHUB_APP_TOKEN_CONFIG)."

    def add_arguments(self, parser) -> None:  # type: ignore[override]
        subparsers = parser.add_subparsers(dest="action", required=True)

        init_parser = subparsers.add_parser("init", help="Create a template config JSON file.")
        init_parser.add_argument("file", help="Output path (for example: .github-app-config.local.json)")

        validate_parser = subparsers.add_parser("validate", help="Validate a config JSON file.")
        validate_parser.add_argument("file", help="Config JSON file path")
        validate_parser.add_argument(
            "--check-key-paths",
            action="store_true",
            help="Also verify that every private_key_path exists and is readable.",
        )

        inline_parser = subparsers.add_parser(
            "inline-keys",
            help="Read private_key_path PEM files and write inline private_key fields.",
        )
        inline_parser.add_argument("file", help="Config JSON file path")
        inline_parser.add_argument(
            "--in-place",
            action="store_true",
            help="Overwrite the input file.",
        )
        inline_parser.add_argument(
            "--output",
            help="Write updated config to this output path instead of stdout.",
        )
        inline_parser.add_argument(
            "--keep-path",
            action="store_true",
            help="Keep private_key_path fields after inlining private_key.",
        )
        inline_parser.add_argument(
            "--replace-existing",
            action="store_true",
            help="Replace existing private_key values when private_key_path is also present.",
        )

        env_parser = subparsers.add_parser("to-env", help="Print compact JSON for GITHUB_APP_TOKEN_CONFIG.")
        env_parser.add_argument("file", help="Config JSON file path")
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
            self._cmd_validate(path, check_key_paths=bool(options.get("check_key_paths")))
            return None
        if action == "inline-keys":
            self._cmd_inline_keys(
                path,
                in_place=bool(options.get("in_place")),
                output=options.get("output"),
                keep_path=bool(options.get("keep_path")),
                replace_existing=bool(options.get("replace_existing")),
            )
            return None
        if action == "to-env":
            self._cmd_to_env(path, export=bool(options.get("export")))
            return None

        raise CommandError(f"unknown action: {action}")

    def _cmd_init(self, path: Path) -> None:
        if path.exists():
            raise CommandError(f"refusing to overwrite existing file: {path}")
        template = self._template_config()
        path.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Created template config at {path}"))

    def _cmd_validate(self, path: Path, *, check_key_paths: bool) -> None:
        config = self._read_config(path)
        self._validate_config(config, check_key_paths=check_key_paths, base_dir=path.parent)
        app_count = len(config.get("apps", [])) if isinstance(config.get("apps"), list) else 0
        self.stdout.write(self.style.SUCCESS(f"Valid config: {path} ({app_count} app entries)"))

    def _cmd_inline_keys(
        self,
        path: Path,
        *,
        in_place: bool,
        output: str | None,
        keep_path: bool,
        replace_existing: bool,
    ) -> None:
        if in_place and output:
            raise CommandError("choose either --in-place or --output, not both")

        config = self._read_config(path)
        normalized = self._validate_config(config, check_key_paths=False, base_dir=path.parent)
        converted = 0
        skipped_existing = 0

        apps = normalized.get("apps")
        assert isinstance(apps, list)  # validated above
        for app in apps:
            assert isinstance(app, dict)  # validated above
            private_key_path = app.get("private_key_path")
            if not isinstance(private_key_path, str) or not private_key_path.strip():
                continue

            has_private_key = isinstance(app.get("private_key"), str) and bool(str(app["private_key"]).strip())
            if has_private_key and not replace_existing:
                skipped_existing += 1
                if not keep_path:
                    app.pop("private_key_path", None)
                continue

            key_text = self._read_private_key_text(path=private_key_path, base_dir=path.parent)
            app["private_key"] = key_text
            converted += 1
            if not keep_path:
                app.pop("private_key_path", None)

        rendered = json.dumps(normalized, indent=2) + "\n"
        if in_place:
            path.write_text(rendered, encoding="utf-8")
            destination = str(path)
        elif output:
            output_path = Path(output)
            output_path.write_text(rendered, encoding="utf-8")
            destination = str(output_path)
        else:
            self.stdout.write(rendered.rstrip("\n"))
            destination = "stdout"

        self.stdout.write(
            self.style.SUCCESS(
                f"Inlined private keys for {converted} app(s), skipped {skipped_existing} app(s) with existing private_key. Wrote to {destination}."
            )
        )

    def _cmd_to_env(self, path: Path, *, export: bool) -> None:
        config = self._read_config(path)
        normalized = self._validate_config(config, check_key_paths=False, base_dir=path.parent)
        compact = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
        if export:
            self.stdout.write(f"GITHUB_APP_TOKEN_CONFIG='{compact}'")
            return
        self.stdout.write(compact)

    def _read_config(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CommandError(f"file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise CommandError("config must be a JSON object")
        return raw

    def _template_config(self) -> dict[str, Any]:
        return {
            "api_base_url": "https://api.github.com",
            "cache_skew_seconds": 60,
            "operation_app_map": {
                "assign_pr": "queueboard-assignment",
                "unassign_pr": "queueboard-assignment",
                "syncer_repo_discovery": "queueboard-syncer-read",
                "syncer_pr_read": "queueboard-syncer-read",
                "syncer_ci_read": "queueboard-syncer-read",
            },
            "apps": [
                {
                    "name": "queueboard-assignment",
                    "app_id": 123456,
                    "private_key_path": "/path/to/queueboard-assignment.pem",
                    "installation_lookup": "repo",
                    "operations": ["assign_pr", "unassign_pr"],
                },
                {
                    "name": "queueboard-syncer-read",
                    "app_id": 234567,
                    "private_key_path": "/path/to/queueboard-syncer-read.pem",
                    "installation_lookup": "owner",
                    "installation_owner_type": "org",
                    "installation_owner": "leanprover-community",
                    "operations": ["syncer_repo_discovery", "syncer_pr_read", "syncer_ci_read"],
                },
            ],
        }

    def _validate_config(self, raw: dict[str, Any], *, check_key_paths: bool, base_dir: Path) -> dict[str, Any]:
        allowed_top_level = {"api_base_url", "cache_skew_seconds", "operation_app_map", "apps"}
        unknown_top_level = sorted(set(raw.keys()) - allowed_top_level)
        if unknown_top_level:
            raise CommandError(f"config has unknown top-level keys: {', '.join(unknown_top_level)}")

        api_base_url = raw.get("api_base_url")
        if api_base_url is not None and (not isinstance(api_base_url, str) or not api_base_url.strip()):
            raise CommandError("api_base_url must be a non-empty string when provided")

        cache_skew_seconds = raw.get("cache_skew_seconds")
        if cache_skew_seconds is not None:
            if not isinstance(cache_skew_seconds, int) or cache_skew_seconds < 0:
                raise CommandError("cache_skew_seconds must be a non-negative integer when provided")

        operation_app_map = raw.get("operation_app_map", {})
        if not isinstance(operation_app_map, dict):
            raise CommandError("operation_app_map must be an object")
        for operation, app_name in operation_app_map.items():
            if not isinstance(operation, str) or not operation.strip():
                raise CommandError("operation_app_map keys must be non-empty strings")
            if not isinstance(app_name, str) or not app_name.strip():
                raise CommandError("operation_app_map values must be non-empty strings")

        apps = raw.get("apps")
        if not isinstance(apps, list) or not apps:
            raise CommandError("apps must be a non-empty list")

        app_names: set[str] = set()
        for index, app in enumerate(apps):
            if not isinstance(app, dict):
                raise CommandError(f"apps[{index}] must be an object")
            unknown_app_keys = sorted(
                set(app.keys())
                - {
                    "name",
                    "app_id",
                    "private_key",
                    "private_key_path",
                    "operations",
                    "installation_lookup",
                    "installation_owner_type",
                    "installation_owner",
                }
            )
            if unknown_app_keys:
                raise CommandError(f"apps[{index}] has unknown keys: {', '.join(unknown_app_keys)}")

            name = app.get("name")
            if not isinstance(name, str) or not name.strip():
                raise CommandError(f"apps[{index}].name must be a non-empty string")
            if name in app_names:
                raise CommandError(f"duplicate app name: {name}")
            app_names.add(name)

            app_id = app.get("app_id")
            if not isinstance(app_id, int) or app_id <= 0:
                raise CommandError(f"apps[{index}].app_id must be a positive integer")

            operations = app.get("operations", [])
            if not isinstance(operations, list):
                raise CommandError(f"apps[{index}].operations must be a list")
            if not all(isinstance(item, str) and item.strip() for item in operations):
                raise CommandError(f"apps[{index}].operations must contain non-empty strings")

            installation_lookup = app.get("installation_lookup", "repo")
            if not isinstance(installation_lookup, str) or installation_lookup not in {"repo", "owner"}:
                raise CommandError(f"apps[{index}].installation_lookup must be 'repo' or 'owner' when provided")
            installation_owner_type = app.get("installation_owner_type", "org")
            if not isinstance(installation_owner_type, str) or installation_owner_type not in {"org", "user"}:
                raise CommandError(f"apps[{index}].installation_owner_type must be 'org' or 'user' when provided")
            installation_owner = app.get("installation_owner")
            if installation_owner is not None and (not isinstance(installation_owner, str) or not installation_owner.strip()):
                raise CommandError(f"apps[{index}].installation_owner must be a non-empty string when provided")

            private_key = app.get("private_key")
            private_key_path = app.get("private_key_path")
            has_private_key = isinstance(private_key, str) and bool(private_key.strip())
            has_private_key_path = isinstance(private_key_path, str) and bool(private_key_path.strip())

            if not has_private_key and not has_private_key_path:
                raise CommandError(f"apps[{index}] must define either private_key or private_key_path")

            if private_key is not None and not isinstance(private_key, str):
                raise CommandError(f"apps[{index}].private_key must be a string when provided")
            if private_key_path is not None and not isinstance(private_key_path, str):
                raise CommandError(f"apps[{index}].private_key_path must be a string when provided")

            if check_key_paths and has_private_key_path:
                self._read_private_key_text(path=str(private_key_path), base_dir=base_dir)

        mapped_app_names = {str(app_name) for app_name in operation_app_map.values()}
        unknown_mapped = sorted(mapped_app_names - app_names)
        if unknown_mapped:
            raise CommandError(f"operation_app_map references unknown app(s): {', '.join(unknown_mapped)}")

        return raw

    def _read_private_key_text(self, *, path: str, base_dir: Path) -> str:
        key_path = Path(path)
        if not key_path.is_absolute():
            key_path = (base_dir / key_path).resolve()
        try:
            key_text = key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"failed reading private key file: {key_path}") from exc

        normalized = key_text.strip()
        if not normalized:
            raise CommandError(f"private key file is empty: {key_path}")
        return normalized
