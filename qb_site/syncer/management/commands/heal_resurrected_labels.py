from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.models import PRLabel
from syncer.services.consistency import resurrected_prlabels_queryset


class Command(BaseCommand):
    help = (
        "Detect and remove PR label attachments that contradict the PR's own "
        "label timeline (latest LABELED/UNLABELED event for the label is an "
        "UNLABELED). These are labels resurrected by the archive importer's "
        "additive-only label sync (design doc 043). Dry-run by default; pass "
        "--apply to delete the stale rows."
    )

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", help="Limit to a single repository in owner/name format")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the stale PRLabel rows (default: dry-run report only)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap the number of rows processed (0 = no cap)",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        repo: Repository | None = None
        repo_opt = opts.get("repo")
        if repo_opt:
            if "/" not in repo_opt:
                raise CommandError("--repo must be in the form owner/name")
            owner, name = repo_opt.split("/", 1)
            repo = Repository.objects.filter(owner=owner, name=name).first()
            if repo is None:
                raise CommandError(f"Repository not found: {repo_opt}")

        qs = (
            resurrected_prlabels_queryset(repo)
            .select_related("pull_request", "pull_request__repository", "label_def")
            .order_by("pull_request__repository_id", "pull_request__number", "label_def__name")
        )

        limit = int(opts.get("limit") or 0)
        rows = list(qs[:limit] if limit > 0 else qs)

        if not rows:
            self.stdout.write(self.style.SUCCESS("No resurrected label attachments found."))
            return

        apply = bool(opts.get("apply"))
        self.stdout.write(
            self.style.WARNING(f"Found {len(rows)} resurrected label attachment(s)" + ("" if apply else " (dry-run; no changes)"))
        )
        for pl in rows:
            pr = pl.pull_request
            self.stdout.write(
                f"  {pr.repository.owner}/{pr.repository.name}#{pr.number} "
                f"[{pr.state}] label={pl.label_def.name!r} attached_at={pl.created_at.isoformat()}"
            )

        if not apply:
            self.stdout.write("Re-run with --apply to delete these attachments.")
            return

        ids = [pl.id for pl in rows]
        PRLabel.objects.filter(id__in=ids).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {len(ids)} stale PRLabel row(s)."))
