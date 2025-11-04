from __future__ import annotations

from typing import List

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from dateutil import parser as dtparser

from core.models import Repository
from syncer.services.github_client import GitHubClient
from syncer.services.pr_sync_service import PRSyncService
from syncer.models import PullRequest


class Command(BaseCommand):
    help = "Sync one or more pull requests for a repository via GraphQL"

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", required=True, help="Repository in owner/name format")
        parser.add_argument("--number", action="append", type=int, help="PR number to sync (repeatable)")
        parser.add_argument("--since", help="ISO8601 cutoff; discover changed PRs since this time")
        parser.add_argument(
            "--states", action="append", help="Repeatable PR states for --since (OPEN, MERGED, CLOSED). Default: OPEN"
        )
        parser.add_argument("--limit", type=int, default=50, help="Max PRs to discover with --since")
        parser.add_argument("--timelineK", type=int, default=150, help="Max timeline items per PR bundle")
        parser.add_argument("--commitsM", type=int, default=15, help="Number of head commits to inspect")
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
        parser.add_argument(
            "--create-missing-repo-default-branch",
            default="master",
            help="Default branch to use when creating a missing repository row",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        owner_name: str = opts["repo"]
        numbers: List[int] = opts.get("number") or []
        since: str | None = opts.get("since")
        states_opt: List[str] | None = opts.get("states")
        limit: int = int(opts.get("limit", 50))
        timelineK: int = opts["timelineK"]
        commitsM: int = opts["commitsM"]
        dry_run: bool = bool(opts["dry_run"])  # not persisted if True
        default_branch: str = opts["create_missing_repo_default_branch"]

        if "/" not in owner_name:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = owner_name.split("/", 1)

        try:
            client = GitHubClient()
        except RuntimeError as e:
            raise CommandError(str(e))

        # Rate limit logging prints will piggy-back on discovery/header/bundle calls; no extra call here.

        # Track last printed resetAt for rate limit logging
        last_reset_at_printed: str | None = None

        # Discover changed PRs since cutoff if requested
        if since:
            states = ["OPEN"] if not states_opt else [s.upper() for s in states_opt]
            allowed = {"OPEN", "MERGED", "CLOSED"}
            invalid = [s for s in states if s not in allowed]
            if invalid:
                raise CommandError(f"Invalid --states: {', '.join(invalid)}; allowed: OPEN, MERGED, CLOSED")
            discovered = client.get_changed_pr_numbers(owner=owner, name=name, since_iso=since, states=states, limit=limit)
            numbers = list(sorted({*numbers, *discovered}))

            # Log initial rate limit resetAt (and only re-print when it changes)
            rl = client.get_last_rate_limit()
            if isinstance(rl, dict):
                rem0 = rl.get("remaining")
                if rem0 is not None:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.remaining: {rem0}"))
                reset_at = rl.get("resetAt")
                if reset_at and reset_at != last_reset_at_printed:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.resetAt: {reset_at}"))
                    last_reset_at_printed = reset_at

        if not numbers:
            raise CommandError("Provide at least one --number or use --since to discover changed PRs")

        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            repo = Repository(owner=owner, name=name, default_branch=default_branch, is_active=True)
            repo.save()

        svc = PRSyncService()

        for num in numbers:
            # Preflight: skip if DB says we've synced at or after GitHub's updatedAt
            header = client.get_pr_header(owner=owner, name=name, number=int(num))
            pr_node = ((header.get("data") or {}).get("repository") or {}).get("pullRequest")
            # Print rate limit resetAt if changed
            rl = client.get_last_rate_limit()
            if isinstance(rl, dict):
                # Per-query cost for header and current remaining
                cost = rl.get("cost")
                rem = rl.get("remaining")
                if cost is not None and rem is not None:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.query pr_header: cost={cost} remaining={rem}"))
                reset_at = rl.get("resetAt")
                if reset_at and reset_at != last_reset_at_printed:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.resetAt: {reset_at}"))
                    last_reset_at_printed = reset_at
            if pr_node:
                try:
                    gh_updated = dtparser.isoparse(pr_node.get("updatedAt"))
                    if timezone.is_naive(gh_updated):
                        gh_updated = timezone.make_aware(gh_updated)
                except Exception:
                    gh_updated = None
                if gh_updated is not None:
                    pr_db = PullRequest.objects.filter(repository=repo, number=int(num)).first()
                    if pr_db and pr_db.last_synced_at and gh_updated <= pr_db.last_synced_at:
                        self.stdout.write(self.style.NOTICE(f"PR #{num} up-to-date; skipping"))
                        continue

            # Define per-query rate log callback used inside the service
            def rate_log(label: str, rl_snap: dict) -> None:
                cost = rl_snap.get("cost")
                rem = rl_snap.get("remaining")
                if cost is not None and rem is not None:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.query {label}: cost={cost} remaining={rem}"))
                rset = rl_snap.get("resetAt")
                nonlocal last_reset_at_printed
                if rset and rset != last_reset_at_printed:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.resetAt: {rset}"))
                    last_reset_at_printed = rset

            res = svc.sync_pull_request(
                repo,
                number=int(num),
                client=client,
                timelineK=timelineK,
                commitsM=commitsM,
                dry_run=dry_run,
                rate_log=rate_log,
            )
            # After bundle/page calls, print resetAt if it changed during sync
            rl2 = client.get_last_rate_limit()
            if isinstance(rl2, dict):
                rem2 = rl2.get("remaining")
                if rem2 is not None:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.remaining: {rem2}"))
                reset_at2 = rl2.get("resetAt")
                if reset_at2 and reset_at2 != last_reset_at_printed:
                    self.stdout.write(self.style.NOTICE(f"rateLimit.resetAt: {reset_at2}"))
                    last_reset_at_printed = reset_at2
            self.stdout.write(
                self.style.SUCCESS(
                    f"Synced PR #{num}: labels +{res['labels_created']}/~{res['labels_updated']} "
                    f"attachments +{res['prlabels_created']}/-{res['prlabels_deleted']}; "
                    f"events +{res['events_created']}; checkruns +{res['checkruns_upserted']}; "
                    f"statusctx +{res['statusctx_upserted']}"
                )
            )

        # Final rate limit snapshot already logged from the last query via callbacks; no extra calls here.
