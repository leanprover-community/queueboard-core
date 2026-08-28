from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, IO, Iterable, List, Tuple

from django.db import transaction

from core.models import Repository, ReviewerPreference, User
from core.utils.db import update_if_changed

DEFAULT_REPO = "leanprover-community/mathlib4"


class ReviewerTopicsImportError(Exception):
    """Raised when reviewer-topics.json import fails."""


class ReviewerTopicsExportError(ReviewerTopicsImportError):
    """Raised when reviewer-topics.json export fails."""


@dataclass
class ImportResult:
    owner: str
    name: str
    repo_created: bool
    created_users: int
    updated_users: int
    skipped_users: int
    created_preferences: int
    updated_preferences: int
    dry_run: bool
    log: list[str]

    def summary_text(self) -> str:
        return (
            f"Users: +{self.created_users}, ~{self.updated_users}, -skipped {self.skipped_users}; "
            f"Preferences: +{self.created_preferences}, ~{self.updated_preferences}"
        )


def parse_owner_repo(val: str) -> Tuple[str, str]:
    if "/" not in val:
        raise ReviewerTopicsImportError("Repository must be in the form 'owner/name'")
    owner, name = val.split("/", 1)
    owner = owner.strip()
    name = name.strip()
    if not owner or not name:
        raise ReviewerTopicsImportError("Repository must be in the form 'owner/name'")
    return owner, name


def _read_text(source: IO[str] | IO[bytes] | str | Path) -> str:
    if hasattr(source, "read"):
        content = source.read()  # type: ignore[arg-type]
        if isinstance(content, bytes):
            return content.decode("utf-8")
        return str(content)
    path = Path(source)
    if not path.exists():
        raise ReviewerTopicsImportError(f"File not found: {path}")
    return path.read_text()


def _dedupe_case_insensitive_preserve_first(values: Iterable[str]) -> List[str]:
    seen = set()
    out: list[str] = []
    for v in values:
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def import_reviewer_topics(
    *,
    repo: str,
    file_obj: IO[str] | IO[bytes] | str | Path,
    replace_labels: bool = True,
    dry_run: bool = False,
    create_missing_users: bool = True,
    create_missing_repo_default_branch: str = "master",
    verbose: bool = False,
    logger: Callable[[str], None] | None = None,
) -> ImportResult:
    """Import reviewer preferences from a reviewer-topics.json-like payload.

    Field mapping notes:
    - ``github_handle`` -> User.github_login (case-insensitive lookup, updates casing if different).
    - ``temporary_break``: when truthy, sets ``auto_assign=False`` (no direct DB column).
    - ``auto_assign``: otherwise mirrors the JSON boolean.
    - ``top_level``: maps to ``preferred_labels`` (replace or merge based on ``replace_labels``).
    - ``free_form``: copied to the ``free_form`` text field.
    - ``maximum_capacity``: copied when present; otherwise existing/default is kept.
    - ``max_new_assignments_per_week``: copied when present (design doc 054); ``null`` clears the
      limit. Absent leaves the existing value alone, so a file written before 054 never silently
      un-limits a reviewer.
    - ``conflict_of_interest``: copied to ``conflict_of_interest`` (deduped, case-insensitive).
    - ``zulip_handle`` and any other extra fields are ignored (not stored).
    """

    owner, name = parse_owner_repo(repo)
    text = _read_text(file_obj)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ReviewerTopicsImportError(f"Failed to parse JSON: {exc}") from exc

    if not isinstance(data, list):
        raise ReviewerTopicsImportError("Expected a JSON array at top-level")

    repo_obj = Repository.objects.filter(owner=owner, name=name).first()
    repo_created = False
    if repo_obj is None:
        if dry_run:
            repo_created = True
        else:
            repo_obj = Repository(
                owner=owner,
                name=name,
                default_branch=create_missing_repo_default_branch,
                is_active=True,
            )
            repo_obj.save()
            repo_created = True

    created_users = 0
    updated_users = 0
    created_prefs = 0
    updated_prefs = 0
    skipped_users = 0
    log_lines: list[str] = []

    def log(message: str) -> None:
        log_lines.append(message)
        if logger:
            logger(message)

    @transaction.atomic
    def apply_entry(entry: dict[str, Any]) -> None:
        nonlocal created_users, updated_users, created_prefs, updated_prefs, skipped_users

        gh_login = (entry.get("github_handle") or "").strip()
        if not gh_login:
            skipped_users += 1
            if verbose:
                log("- Skipping entry with missing github_handle")
            return

        user = User.objects.filter(github_login__iexact=gh_login).first()
        if user is None:
            if not create_missing_users:
                skipped_users += 1
                if verbose:
                    log(f"- No user for {gh_login}; skipping (create_missing_users=false)")
                return
            if dry_run:
                user = User(github_login=gh_login, is_active=True)
                created_users += 1
            else:
                user = User(github_login=gh_login, is_active=True)
                user.save()
                created_users += 1
        else:
            if user.github_login != gh_login:
                if dry_run:
                    updated_users += 1
                else:
                    _, fields = update_if_changed(user, {"github_login": gh_login})
                    if fields:
                        updated_users += 1

        if repo_obj is not None and getattr(repo_obj, "pk", None) and getattr(user, "pk", None):
            pref = ReviewerPreference.objects.filter(repository=repo_obj, user=user).first()
        else:
            pref = None
        was_create = pref is None
        if was_create:
            pref = ReviewerPreference(repository=repo_obj, user=user)  # type: ignore[assignment]

        changes: dict[str, tuple[Any, Any]] = {}

        if "maximum_capacity" in entry:
            new_cap = int(entry["maximum_capacity"])  # type: ignore[arg-type]
            if pref.maximum_capacity != new_cap:
                changes["maximum_capacity"] = (pref.maximum_capacity, new_cap)
                pref.maximum_capacity = new_cap

        if "max_new_assignments_per_week" in entry:
            raw_rate = entry["max_new_assignments_per_week"]
            new_rate = None if raw_rate is None else int(raw_rate)  # type: ignore[arg-type]
            if pref.max_new_assignments_per_week != new_rate:
                changes["max_new_assignments_per_week"] = (pref.max_new_assignments_per_week, new_rate)
                pref.max_new_assignments_per_week = new_rate

        if bool(entry.get("temporary_break")):
            if pref.auto_assign:
                changes["auto_assign"] = (pref.auto_assign, False)
                pref.auto_assign = False
        elif "auto_assign" in entry:
            new_auto = bool(entry["auto_assign"])  # type: ignore[arg-type]
            if pref.auto_assign != new_auto:
                changes["auto_assign"] = (pref.auto_assign, new_auto)
                pref.auto_assign = new_auto

        if "top_level" in entry:
            labels = [str(x) for x in entry.get("top_level", [])]
            labels = _dedupe_case_insensitive_preserve_first(labels)
            if replace_labels:
                if pref.preferred_labels != labels:
                    changes["preferred_labels"] = (pref.preferred_labels, labels)
                    pref.preferred_labels = labels
            else:
                merged = _dedupe_case_insensitive_preserve_first(list(pref.preferred_labels) + labels)
                if merged != pref.preferred_labels:
                    changes["preferred_labels"] = (pref.preferred_labels, merged)
                    pref.preferred_labels = merged

        if "free_form" in entry:
            new_ff = str(entry.get("free_form") or "")
            if (pref.free_form or "") != new_ff:
                changes["free_form"] = (pref.free_form, new_ff)
                pref.free_form = new_ff

        if "conflict_of_interest" in entry:
            raw_conflicts = entry.get("conflict_of_interest") or []
            conflicts = _dedupe_case_insensitive_preserve_first(str(x) for x in raw_conflicts)
            if pref.conflict_of_interest != conflicts:
                changes["conflict_of_interest"] = (pref.conflict_of_interest, conflicts)
                pref.conflict_of_interest = conflicts

        if was_create:
            if dry_run:
                created_prefs += 1
            else:
                pref.save()
                created_prefs += 1
            if verbose:
                log(f"+ Created preference for {gh_login} ({len(changes)} fields)")
        else:
            if changes:
                if not dry_run:
                    pref.save()
                updated_prefs += 1
                if verbose:
                    diffs = ", ".join(f"{k}: {a!r} -> {b!r}" for k, (a, b) in changes.items())
                    log(f"~ Updated preference for {gh_login}: {diffs}")
            elif verbose:
                log(f"= No changes for {gh_login}")

    for entry in data:
        if not isinstance(entry, dict):
            if verbose:
                log("- Skipping non-object entry")
            continue
        apply_entry(entry)

    result = ImportResult(
        owner=owner,
        name=name,
        repo_created=repo_created,
        created_users=created_users,
        updated_users=updated_users,
        skipped_users=skipped_users,
        created_preferences=created_prefs,
        updated_preferences=updated_prefs,
        dry_run=dry_run,
        log=log_lines,
    )
    if verbose:
        log(result.summary_text())
    return result


def export_reviewer_topics(
    *,
    repo: str,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Serialize reviewer preferences for a repository to reviewer-topics.json structure.

    Field mapping notes:
    - Emits ``github_handle`` from ``User.github_login``.
    - Emits ``top_level`` from ``preferred_labels``.
    - Emits ``free_form`` and ``auto_assign``.
    - Emits ``maximum_capacity`` only when it differs from the model default (to mirror legacy files).
    - Emits ``max_new_assignments_per_week`` only when a limit is set (``None`` is the default and
      means unlimited, so omitting it round-trips as "no limit").
    - Emits ``conflict_of_interest`` when present.
    - Does not emit ``zulip_handle`` or other non-model fields.
    """

    owner, name = parse_owner_repo(repo)
    repo_obj = Repository.objects.filter(owner=owner, name=name).first()
    if repo_obj is None:
        raise ReviewerTopicsExportError(f"Repository not found: {owner}/{name}")

    prefs = ReviewerPreference.objects.filter(repository=repo_obj).select_related("user").order_by("user__github_login")

    entries: list[dict[str, Any]] = []
    for pref in prefs:
        gh_login = pref.user.github_login or ""
        entry: dict[str, Any] = {
            "github_handle": gh_login,
            "top_level": list(pref.preferred_labels),
            "free_form": pref.free_form or "",
            "auto_assign": bool(pref.auto_assign),
        }
        if pref.maximum_capacity != ReviewerPreference._meta.get_field("maximum_capacity").default:
            entry["maximum_capacity"] = pref.maximum_capacity
        if pref.max_new_assignments_per_week is not None:
            entry["max_new_assignments_per_week"] = pref.max_new_assignments_per_week
        if pref.conflict_of_interest:
            entry["conflict_of_interest"] = list(pref.conflict_of_interest)
        entries.append(entry)

    return owner, name, entries
