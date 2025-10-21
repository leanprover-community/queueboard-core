from __future__ import annotations

import json
from typing import List

from django.core.management.base import BaseCommand, CommandError

from syncer.services.github_client import GitHubClient


class Command(BaseCommand):
    help = "List PR numbers updated since a cutoff (for testing/manual runs)"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument("--since", required=True, help="ISO8601 cutoff (e.g., 2025-10-20T00:00:00Z or 2025-10-20)")
        parser.add_argument(
            "--states",
            action="append",
            help="Repeatable; one of OPEN, MERGED, CLOSED. Default: OPEN",
        )
        parser.add_argument("--limit", type=int, default=50, help="Max PR numbers to return")
        parser.add_argument("--per-page", type=int, default=100, help="GraphQL page size (max 100)")
        parser.add_argument("--max-pages", type=int, default=0, help="Max pages to fetch (0 = no cap)")
        parser.add_argument("--json", action="store_true", help="Emit a JSON array instead of plain text")

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        since_iso: str = opts["since"]
        states_opt: List[str] | None = opts.get("states")
        limit: int = int(opts["limit"])
        per_page: int = int(opts["per_page"])  # argparse stores as snake_case dest
        max_pages_val: int = int(opts["max_pages"])  # argparse stores as snake_case dest
        json_out: bool = bool(opts["json"])  # emit JSON array if true

        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        # Normalize states; default to OPEN
        states: List[str] | None
        if states_opt:
            allowed = {"OPEN", "MERGED", "CLOSED"}
            states = [s.upper() for s in states_opt]
            invalid = [s for s in states if s not in allowed]
            if invalid:
                raise CommandError(f"Invalid --states: {', '.join(invalid)}; allowed: OPEN, MERGED, CLOSED")
        else:
            states = ["OPEN"]

        # GitHub client
        try:
            gh = GitHubClient()
        except RuntimeError as e:
            raise CommandError(str(e))

        nums = gh.get_changed_pr_numbers(
            owner=owner,
            name=name,
            since_iso=since_iso,
            states=states,
            limit=limit,
            per_page=per_page,
            max_pages=None if max_pages_val <= 0 else max_pages_val,
        )

        if json_out:
            self.stdout.write(json.dumps({
                "repo": owner_name,
                "since": since_iso,
                "states": states,
                "numbers": nums,
            }))
            return

        # Default: print one number per line for easy piping
        if not nums:
            self.stdout.write("")
            return
        self.stdout.write("\n".join(str(n) for n in nums))
