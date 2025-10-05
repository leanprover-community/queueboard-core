from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Repository, ReviewerPreference, User


DEFAULT_REPO = "leanprover-community/mathlib4"


def parse_owner_repo(val: str) -> Tuple[str, str]:
    if "/" not in val:
        raise CommandError("--repo must be in the form 'owner/name'")
    owner, name = val.split("/", 1)
    owner = owner.strip()
    name = name.strip()
    if not owner or not name:
        raise CommandError("--repo must be in the form 'owner/name'")
    return owner, name


def _dedupe_case_insensitive_preserve_first(values: List[str]) -> List[str]:
    seen = set()
    out = []
    for v in values:
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


class Command(BaseCommand):
    help = "Import reviewer preferences from reviewer-topics.json into ReviewerPreference rows"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            "--repo",
            default=DEFAULT_REPO,
            help=f"Target repository in the form owner/name (default: {DEFAULT_REPO})",
        )
        parser.add_argument(
            "--path",
            default=str(Path("reviewer-topics.json")),
            help="Path to reviewer-topics.json (default: reviewer-topics.json)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--replace-labels",
            dest="replace_labels",
            action="store_true",
            help="Replace preferred_labels with the file's list (default)",
        )
        group.add_argument(
            "--merge",
            dest="replace_labels",
            action="store_false",
            help="Merge preferred_labels instead of replacing",
        )
        parser.set_defaults(replace_labels=True)
        parser.add_argument(
            "--create-missing-users",
            dest="create_missing_users",
            action="store_true",
            default=True,
            help="Create User rows for unknown GitHub logins (default: true)",
        )
        parser.add_argument(
            "--no-create-missing-users",
            dest="create_missing_users",
            action="store_false",
            help="Do not create users for unknown GitHub logins",
        )
        parser.add_argument(
            "--create-missing-repo-default-branch",
            default="master",
            help="When creating the repository row, use this as default_branch (default: master)",
        )
        parser.add_argument("--verbose", action="store_true", help="Print per-entry changes")

    def handle(self, *args, **options):  # type: ignore[override]
        repo_arg: str = options["repo"]
        path_arg: str = options["path"]
        dry_run: bool = options["dry_run"]
        replace_labels: bool = options["replace_labels"]
        create_missing_users: bool = options["create_missing_users"]
        create_repo_branch: str = options["create_missing_repo_default_branch"]
        verbose: bool = options["verbose"]

        owner, name = parse_owner_repo(repo_arg)

        file_path = Path(path_arg)
        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")

        try:
            data = json.loads(file_path.read_text())
        except json.JSONDecodeError as e:
            raise CommandError(f"Failed to parse JSON: {e}")

        if not isinstance(data, list):
            raise CommandError("Expected a JSON array at top-level")

        # Ensure repository
        repo = Repository.objects.filter(owner=owner, name=name).first()
        repo_created = False
        if repo is None:
            if dry_run:
                # Simulate creation without hitting the DB; use None to avoid accidental filters
                repo_created = True
                repo = None
            else:
                repo = Repository(owner=owner, name=name, default_branch=create_repo_branch, is_active=True)
                repo.save()
                repo_created = True

        created_users = 0
        updated_users = 0
        created_prefs = 0
        updated_prefs = 0
        skipped_users = 0

        @transaction.atomic
        def apply_entry(entry: Dict[str, Any]) -> None:
            nonlocal created_users, updated_users, created_prefs, updated_prefs, skipped_users

            gh_login = (entry.get("github_handle") or "").strip()
            if not gh_login:
                if verbose:
                    self.stdout.write("- Skipping entry with missing github_handle")
                skipped_users += 1
                return

            # Find user by case-insensitive login
            user = User.objects.filter(github_login__iexact=gh_login).first()
            if user is None:
                if not create_missing_users:
                    skipped_users += 1
                    if verbose:
                        self.stdout.write(f"- No user for {gh_login}; skipping (create_missing_users=false)")
                    return
                if dry_run:
                    # Simulate an unsaved user instance
                    user = User(github_login=gh_login, is_active=True)
                    created_users += 1
                else:
                    user = User(github_login=gh_login, is_active=True)
                    user.save()
                    created_users += 1
            else:
                # Align casing if it changed
                if user.github_login != gh_login:
                    user.github_login = gh_login
                    if not dry_run:
                        user.save(update_fields=["github_login"])
                    updated_users += 1

            # Upsert ReviewerPreference
            # Only query for an existing preference if both repo and user are persisted
            if repo is not None and getattr(repo, "pk", None) and getattr(user, "pk", None):
                pref = ReviewerPreference.objects.filter(repository=repo, user=user).first()
            else:
                pref = None
            was_create = pref is None
            if was_create:
                pref = ReviewerPreference(repository=repo, user=user)  # type: ignore[assignment]

            # Track updates for logs
            changes: Dict[str, Tuple[Any, Any]] = {}

            # maximum_capacity
            if "maximum_capacity" in entry:
                new_cap = int(entry["maximum_capacity"])  # type: ignore[arg-type]
                if pref.maximum_capacity != new_cap:
                    changes["maximum_capacity"] = (pref.maximum_capacity, new_cap)
                    pref.maximum_capacity = new_cap

            # auto_assign and temporary_break
            if bool(entry.get("temporary_break")):
                if pref.auto_assign:
                    changes["auto_assign"] = (pref.auto_assign, False)
                    pref.auto_assign = False
            elif "auto_assign" in entry:
                new_auto = bool(entry["auto_assign"])  # type: ignore[arg-type]
                if pref.auto_assign != new_auto:
                    changes["auto_assign"] = (pref.auto_assign, new_auto)
                    pref.auto_assign = new_auto

            # preferred_labels
            if "top_level" in entry:
                labels = [str(x) for x in entry.get("top_level", [])]
                labels = _dedupe_case_insensitive_preserve_first(labels)
                if replace_labels:
                    if pref.preferred_labels != labels:
                        changes["preferred_labels"] = (pref.preferred_labels, labels)
                        pref.preferred_labels = labels
                else:
                    # Merge, case-insensitive dedupe
                    merged = _dedupe_case_insensitive_preserve_first(list(pref.preferred_labels) + labels)
                    if merged != pref.preferred_labels:
                        changes["preferred_labels"] = (pref.preferred_labels, merged)
                        pref.preferred_labels = merged

            # free_form
            if "free_form" in entry:
                new_ff = str(entry.get("free_form") or "")
                if (pref.free_form or "") != new_ff:
                    changes["free_form"] = (pref.free_form, new_ff)
                    pref.free_form = new_ff

            if was_create:
                if dry_run:
                    created_prefs += 1
                else:
                    pref.save()
                    created_prefs += 1
                if verbose:
                    self.stdout.write(f"+ Created preference for {user.github_login} ({len(changes)} fields)")
            else:
                if changes:
                    if not dry_run:
                        pref.save()
                    updated_prefs += 1
                    if verbose:
                        diffs = ", ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in changes.items())
                        self.stdout.write(f"~ Updated preference for {user.github_login}: {diffs}")
                elif verbose:
                    self.stdout.write(f"= No changes for {user.github_login}")

        # Apply entries
        for entry in data:
            if not isinstance(entry, dict):
                if verbose:
                    self.stdout.write("- Skipping non-object entry")
                continue
            if dry_run:
                # Still run through logic but avoid DB writes by wrapping and not saving
                apply_entry(entry)
            else:
                apply_entry(entry)

        # Summary
        if repo_created:
            self.stdout.write(self.style.SUCCESS(f"Repository {owner}/{name} created"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Users: +{created_users}, ~{updated_users}, -skipped {skipped_users}; Preferences: +{created_prefs}, ~{updated_prefs}"
            )
        )
