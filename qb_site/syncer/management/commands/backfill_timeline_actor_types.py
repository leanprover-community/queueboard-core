"""Backfill ``PRTimelineEvent.actor_type`` / ``actor_node_id`` (design doc 051).

Resolves each stored timeline item by its own ``github_node_id`` through
GitHub's ``nodes(ids:)`` root field, which returns the same actor union the
timeline queries do. This is exact rather than heuristic — renamed accounts
resolve correctly and login reuse cannot mis-type anything — and it is roughly
1/20th the rate cost of a full schema-version rewalk wave.

The same response carries ``login``, so archive-imported rows (whose legacy
fragment omitted the actor entirely) get their missing ``actor_login`` filled
in the same pass. That fill is guarded fill-only: a stored non-empty login is
the login *as of ingest time*, and clobbering it with today's login would
destroy the rename history ``actor_node_id`` exists to expose.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz
from typing import Any, Dict, List, Optional, Sequence

import requests
from django.core.management.base import BaseCommand, CommandError

from core.models import Repository
from syncer.models import PRTimelineEvent
from syncer.services.github_client import GitHubClient
from syncer.services.rate_budget import get_rate_snapshot
from syncer.services.sub.timeline_sync import actor_node_id_or_none, actor_type_or_none

logger = logging.getLogger(__name__)

# Tunables live here and on the argument parser rather than in settings: this
# is a one-shot operator command, and a `getattr(settings, ...)` with no
# matching os.getenv line in base.py would be a phantom setting (root AGENTS.md).
DEFAULT_BATCH_SIZE = 100
# Headroom left for the live syncer, which shares this budget and only stops
# itself at SYNCER_RATE_REMAINING_MIN (200). 2500 matches the floor the doc-043
# forced-resync drain settled on (ARCHIVE_RESYNC_MIN_RATE_REMAINING).
DEFAULT_MIN_RATE_REMAINING = 2500
# Slack added to `resetAt` when sleeping, so we wake up after the window rolls.
RATE_RESET_SLACK_SECONDS = 15
# Bounded retry for transient transport failures (5xx, connection resets,
# timeouts). A full drain is ~6 k calls, so meeting one is near-certain, and
# letting it propagate would abort the run before it printed its counters.
CALL_RETRY_ATTEMPTS = 3
CALL_RETRY_BACKOFF_SECONDS = 2

# Marks an id whose call failed for a reason that says nothing about the id
# itself. Those rows stay untyped and a later run retries them; keeping them out
# of `unresolved` is what stops that count from reading as "these nodes are gone
# from GitHub" when it actually means "we never got an answer".
_CALL_FAILED = object()


def _is_rate_limit_error(message: str) -> bool:
    """True for the GraphQL rejection GitHub sends once the budget is spent."""
    low = message.lower()
    return "rate limit" in low or "rate_limited" in low


def _is_missing_node_error(message: str) -> bool:
    """True when GitHub is telling us one specific id does not resolve."""
    low = message.lower()
    return "could not resolve to a node" in low or "not a valid global id" in low


# GitHub names the offending id in the message:
#   Could not resolve to a node with the global id of 'IC_kwDOFcwZ1c7vKdns'.
# Reading it back is what lets us drop the dead id and retry the rest, instead
# of bisecting the batch to rediscover what the error already told us.
_MISSING_ID_RE = re.compile(r"global id of '([^']+)'")


def named_missing_ids(message: str, candidates: Sequence[str]) -> List[str]:
    """Return the ids in ``candidates`` that ``message`` names as unresolvable."""
    named = set(_MISSING_ID_RE.findall(message))
    return [nid for nid in candidates if nid in named]


@dataclass
class BackfillStats:
    """Per-run counters. Every one of these is reported; none is decorative."""

    scanned: int = 0
    typed: int = 0
    node_ids_filled: int = 0
    logins_filled: int = 0
    rows_written: int = 0
    # GitHub returned `actor: null` — permanent, no backfill route can type these.
    null_actor: int = 0
    # The node id no longer resolves (hard-deleted comment/review, or a bad id).
    unresolved: int = 0
    # The call carrying the row failed for a reason unrelated to the row itself,
    # so we never learned anything about it. A later run retries these.
    call_failed: int = 0
    # Retried calls. `api_calls` counts every attempt; this counts the re-tries
    # among them, so a run fighting a flaky API is visible in the report.
    retries: int = 0
    # Actor reported a typename outside PRActorType (e.g. Organization). The
    # node id is still stored; actor_type stays NULL.
    unmodelled_type: int = 0
    api_calls: int = 0
    # Sum of `rateLimit.cost` across responses — what the run actually spent,
    # rather than api_calls x an assumed cost of 1.
    points_spent: int = 0
    distribution: Counter = field(default_factory=Counter)

    def merge(self, other: "BackfillStats") -> None:
        self.scanned += other.scanned
        self.typed += other.typed
        self.node_ids_filled += other.node_ids_filled
        self.logins_filled += other.logins_filled
        self.rows_written += other.rows_written
        self.null_actor += other.null_actor
        self.unresolved += other.unresolved
        self.call_failed += other.call_failed
        self.retries += other.retries
        self.unmodelled_type += other.unmodelled_type
        self.api_calls += other.api_calls
        self.points_spent += other.points_spent
        self.distribution.update(other.distribution)


def _node_actor(node: Any) -> Optional[Dict[str, Any]]:
    """Return the acting account from one resolved node.

    Timeline event types carry ``actor``; ``IssueComment`` and
    ``PullRequestReview`` carry ``author``. Either may be null.
    """
    if not isinstance(node, dict):
        return None
    actor = node.get("actor")
    if actor is None:
        actor = node.get("author")
    return actor if isinstance(actor, dict) else None


def _rate_snapshot_remaining(client: GitHubClient) -> tuple[Optional[int], Optional[str]]:
    snap = get_rate_snapshot(getattr(client, "token_id", None)) or {}
    remaining = snap.get("remaining")
    reset_at = snap.get("resetAt")
    return (remaining if isinstance(remaining, int) else None, reset_at if isinstance(reset_at, str) else None)


def _seconds_until(reset_at_iso: Optional[str]) -> int:
    if not reset_at_iso:
        return 60
    try:
        reset_dt = datetime.fromisoformat(reset_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return 60
    if reset_dt.tzinfo is None:
        reset_dt = reset_dt.replace(tzinfo=_tz.utc)
    delta = (reset_dt - datetime.now(_tz.utc)).total_seconds()
    return max(1, int(delta) + RATE_RESET_SLACK_SECONDS)


class RateBudgetExhausted(Exception):
    """Raised to unwind the drain when the cached rate snapshot is too low."""


class Command(BaseCommand):
    help = (
        "Backfill PRTimelineEvent.actor_type / actor_node_id by re-resolving each row's "
        "stored github_node_id through GitHub's nodes(ids:) root field (design doc 051). "
        "Also fills archive-imported rows' missing actor_login, guarded fill-only so "
        "ingest-time logins and the rename history survive. "
        "Resumable and idempotent: the target set is 'actor_type IS NULL', so an "
        "interrupted run simply continues where it stopped. Note that rows whose actor "
        "GitHub reports as null can never be typed, so repeat runs plateau at that "
        "population rather than reaching zero — the reported null_actor count is that "
        "floor. Rows whose call failed outright (transport error, or a GraphQL error we "
        "cannot pin on an id) are reported as call_failed and left for a later run, so "
        "one flaky call never ends the drain. Drip-feed with --limit, or use "
        "--wait-for-rate for an unattended drain."
    )

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument("--repo", help="Limit to a single repository in owner/name format")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Node ids per nodes(ids:) call (default {DEFAULT_BATCH_SIZE}; GitHub caps this at {GitHubClient.NODES_IDS_MAX})",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap the number of rows resolved across all repositories (0 = no cap)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Resolve and report the actor-type distribution without writing (still spends GraphQL points)",
        )
        parser.add_argument(
            "--min-rate-remaining",
            type=int,
            default=DEFAULT_MIN_RATE_REMAINING,
            help=(
                f"Pause when the cached GraphQL rate snapshot drops below this (default {DEFAULT_MIN_RATE_REMAINING}). "
                "The drain shares its budget with the live syncer."
            ),
        )
        parser.add_argument(
            "--wait-for-rate",
            action="store_true",
            help="Sleep until resetAt instead of stopping when the rate floor is hit",
        )

    def handle(self, *args, **opts):  # type: ignore[override]
        # Last `rateLimit` block seen, so the run can report measured cost and
        # the budget it leaves behind rather than an assumed per-call price.
        self._last_rate: Optional[Dict[str, Any]] = None
        repo_filter = self._resolve_repo(opts.get("repo"))
        batch_size = max(1, min(int(opts.get("batch_size") or DEFAULT_BATCH_SIZE), GitHubClient.NODES_IDS_MAX))
        limit = max(0, int(opts.get("limit") or 0))
        dry_run = bool(opts.get("dry_run"))
        min_rate = max(0, int(opts.get("min_rate_remaining") or 0))
        wait_for_rate = bool(opts.get("wait_for_rate"))

        # Rows we can never reach this way. Counted and reported rather than
        # assumed to be zero; _extract_event_fields drops node-id-less events,
        # so a non-zero count means something else wrote them.
        no_node_id = self._base_queryset(repo_filter).filter(github_node_id__isnull=True).count()
        if no_node_id:
            self.stdout.write(
                self.style.WARNING(f"{no_node_id} untyped row(s) have no github_node_id and cannot be backfilled this way.")
            )

        repos = self._target_repositories(repo_filter)
        if not repos:
            self.stdout.write(self.style.SUCCESS("Nothing to backfill."))
            return

        totals = BackfillStats()
        stopped_on_rate = False
        for repo in repos:
            if limit and totals.scanned >= limit:
                break
            remaining_budget = (limit - totals.scanned) if limit else 0
            self.stdout.write(f"→ {repo.owner}/{repo.name}")
            stats = BackfillStats()
            try:
                self._drain_repo(
                    repo,
                    stats,
                    batch_size=batch_size,
                    limit=remaining_budget,
                    dry_run=dry_run,
                    min_rate=min_rate,
                    wait_for_rate=wait_for_rate,
                )
            except RateBudgetExhausted:
                stopped_on_rate = True
            finally:
                # Merge unconditionally: a run cut short by the rate floor has
                # already written its earlier batches, so its counters are real.
                totals.merge(stats)
                self._report(stats, prefix="  ", dry_run=dry_run)
            if stopped_on_rate:
                break

        self.stdout.write("")
        self._report(totals, prefix="TOTAL ", dry_run=dry_run)
        if self._last_rate:
            self.stdout.write(
                f"rate: remaining={self._last_rate.get('remaining')} used={self._last_rate.get('used')} "
                f"resetAt={self._last_rate.get('resetAt')}"
            )
        remaining = self._base_queryset(repo_filter).count()
        self.stdout.write(f"{remaining} row(s) still have actor_type IS NULL.")
        if stopped_on_rate:
            hint = "" if wait_for_rate else " Pass --wait-for-rate for an unattended drain."
            self.stdout.write(
                self.style.WARNING(
                    "Stopped early: GraphQL rate budget exhausted (floor reached, or GitHub rejected the call). "
                    f"Re-run to continue.{hint}"
                )
            )
        elif dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing was written."))

    # ---- selection -------------------------------------------------------

    def _resolve_repo(self, repo_opt: Optional[str]) -> Optional[Repository]:
        if not repo_opt:
            return None
        if "/" not in repo_opt:
            raise CommandError("--repo must be in the form owner/name")
        owner, name = repo_opt.split("/", 1)
        repo = Repository.objects.filter(owner=owner, name=name).first()
        if repo is None:
            raise CommandError(f"Repository not found: {repo_opt}")
        return repo

    def _base_queryset(self, repo: Optional[Repository]):
        qs = PRTimelineEvent.objects.filter(actor_type__isnull=True)
        if repo is not None:
            qs = qs.filter(pull_request__repository=repo)
        return qs

    def _target_repositories(self, repo: Optional[Repository]) -> List[Repository]:
        """Repositories with at least one backfillable row.

        Filtering on actual work matters even for an explicit ``--repo``:
        constructing a client requires a token, so a no-op run would otherwise
        fail with "GitHub token not found" instead of reporting nothing to do.
        """
        if repo is not None:
            has_work = self._base_queryset(repo).filter(github_node_id__isnull=False).exists()
            return [repo] if has_work else []
        repo_ids = (
            self._base_queryset(None)
            .filter(github_node_id__isnull=False)
            .values_list("pull_request__repository_id", flat=True)
            .distinct()
        )
        return list(Repository.objects.filter(id__in=list(repo_ids)).order_by("owner", "name"))

    # ---- drain -----------------------------------------------------------

    def _drain_repo(
        self,
        repo: Repository,
        stats: BackfillStats,
        *,
        batch_size: int,
        limit: int,
        dry_run: bool,
        min_rate: int,
        wait_for_rate: bool,
    ) -> None:
        # Per-repo client so GitHub App operation tokens resolve correctly.
        client = GitHubClient(operation="syncer_pr_read", owner=repo.owner, repo=repo.name)
        qs = self._base_queryset(repo).filter(github_node_id__isnull=False).order_by("id")

        last_id = 0
        while True:
            if limit and stats.scanned >= limit:
                break
            take = batch_size
            if limit:
                take = min(take, limit - stats.scanned)
            rows = list(qs.filter(id__gt=last_id)[:take])
            if not rows:
                break
            last_id = rows[-1].pk

            self._gate_on_rate(client, min_rate=min_rate, wait_for_rate=wait_for_rate, stats=stats)

            resolved = self._resolve_ids(client, [r.github_node_id for r in rows if r.github_node_id], stats)
            self._apply(rows, resolved, stats, dry_run=dry_run)

    def _gate_on_rate(self, client: GitHubClient, *, min_rate: int, wait_for_rate: bool, stats: BackfillStats) -> None:
        if min_rate <= 0:
            return
        remaining, reset_at = _rate_snapshot_remaining(client)
        if remaining is None or remaining >= min_rate:
            return
        if not wait_for_rate:
            raise RateBudgetExhausted()
        delay = _seconds_until(reset_at)
        self.stdout.write(self.style.WARNING(f"  rate remaining={remaining} < {min_rate}; sleeping {delay}s until reset"))
        time.sleep(delay)

    def _resolve_ids(self, client: GitHubClient, ids: Sequence[str], stats: BackfillStats) -> Dict[str, Any]:
        """Return ``{node_id: node}`` for ``ids``, or ``_CALL_FAILED`` per unasked id.

        Three failure shapes, handled differently because they mean different
        things:

        - **Rate-limit rejection.** The cached snapshot the gate reads lied —
          Redis unavailable, or the live syncer spent the window between our
          check and this call. Retrying or splitting would only spend more, so
          unwind to the resumable stop.
        - **Transport failure** (5xx, reset, timeout). Retried with backoff;
          if it persists, the batch is recorded as unasked and the drain moves
          on to the next one. Deliberately *not* split: an outage would
          otherwise fan one batch out into hundreds of sleeping calls.
        - **A dead node id.** One unresolvable id makes GitHub reject the whole
          call, poisoning a 100-id batch. GitHub names the id, so drop the
          named ones and retry the remainder: 2 calls, against the 13 that
          bisecting a 100-id batch spends to rediscover the same fact. Dropped
          ids are simply absent from the result, which is what makes ``_apply``
          count them as unresolved.
        - **Any other GraphQL error.** Nothing to parse, so fall back to
          halving and recursing — every good id survives in at most log2(n)
          extra calls and the bad one is isolated.
        """
        if not ids:
            return {}
        node_ids = list(ids)
        graphql_error: Optional[str] = None
        transport_error: Optional[str] = None

        for attempt in range(1, CALL_RETRY_ATTEMPTS + 1):
            try:
                stats.api_calls += 1
                payload = client.get_timeline_actors_by_node_ids(ids=node_ids)
            except RuntimeError as exc:
                message = str(exc)
                if _is_rate_limit_error(message):
                    raise RateBudgetExhausted() from exc
                # A query-level error answers the same way next time; go split.
                graphql_error = message
                break
            except requests.RequestException as exc:
                transport_error = str(exc)
                if attempt < CALL_RETRY_ATTEMPTS:
                    stats.retries += 1
                    logger.warning("backfill_actor_types.transport_retry attempt=%s ids=%s error=%s", attempt, len(node_ids), exc)
                    time.sleep(CALL_RETRY_BACKOFF_SECONDS * attempt)
                continue
            else:
                data = payload.get("data") or {}
                rate = data.get("rateLimit")
                if isinstance(rate, dict):
                    cost = rate.get("cost")
                    if isinstance(cost, int):
                        stats.points_spent += cost
                    self._last_rate = rate
                nodes = data.get("nodes") or []
                return {n["id"]: n for n in nodes if isinstance(n, dict) and n.get("id")}

        if graphql_error is None:
            logger.warning("backfill_actor_types.call_failed ids=%s error=%s", len(node_ids), transport_error)
            return {nid: _CALL_FAILED for nid in node_ids}

        dead = named_missing_ids(graphql_error, node_ids)
        if dead:
            # One log line per batch, not per id: a dense run of deleted
            # comments would otherwise bury everything else in the dyno log.
            logger.warning(
                "backfill_actor_types.unresolvable_node_ids count=%s of=%s ids=%s",
                len(dead),
                len(node_ids),
                ",".join(dead[:5]) + ("…" if len(dead) > 5 else ""),
            )
            survivors = [nid for nid in node_ids if nid not in set(dead)]
            # The dead ids stay out of the returned mapping, so they land in the
            # `unresolved` count exactly as if we had asked about them alone.
            return self._resolve_ids(client, survivors, stats)

        if len(node_ids) > 1:
            mid = len(node_ids) // 2
            left = self._resolve_ids(client, node_ids[:mid], stats)
            left.update(self._resolve_ids(client, node_ids[mid:], stats))
            return left

        logger.warning("backfill_actor_types.node_id_rejected id=%s error=%s", node_ids[0], graphql_error)
        # "Could not resolve" is a fact about the row: that node is gone. Any
        # other query-level error is a fact about the call, so keep the two
        # apart instead of reporting both as unresolved nodes.
        if _is_missing_node_error(graphql_error):
            return {}
        return {node_ids[0]: _CALL_FAILED}

    def _apply(self, rows: List[PRTimelineEvent], resolved: Dict[str, Any], stats: BackfillStats, *, dry_run: bool) -> None:
        to_update: List[PRTimelineEvent] = []
        for row in rows:
            stats.scanned += 1
            node = resolved.get(row.github_node_id or "")
            if node is _CALL_FAILED:
                stats.call_failed += 1
                stats.distribution["(call failed)"] += 1
                continue
            if node is None:
                stats.unresolved += 1
                stats.distribution["(unresolved node)"] += 1
                continue

            actor = _node_actor(node)
            if actor is None:
                stats.null_actor += 1
                stats.distribution["(null actor)"] += 1
                continue

            a_type = actor_type_or_none(actor)
            a_node_id = actor_node_id_or_none(actor)
            login = actor.get("login")
            stats.distribution[a_type or f"(unmodelled: {actor.get('__typename')})"] += 1
            if a_type is None:
                stats.unmodelled_type += 1

            changed = False
            if a_type and row.actor_type is None:
                row.actor_type = a_type
                stats.typed += 1
                changed = True
            if a_node_id and not row.actor_node_id:
                row.actor_node_id = a_node_id
                stats.node_ids_filled += 1
                changed = True
            # Fill-only, and the predicate must cover both empties: the two
            # extraction idioms in timeline_sync disagree on NULL vs "".
            if login and not (row.actor_login or ""):
                row.actor_login = str(login)
                stats.logins_filled += 1
                changed = True
            if changed:
                to_update.append(row)

        stats.rows_written += len(to_update)
        if to_update and not dry_run:
            PRTimelineEvent.objects.bulk_update(to_update, ["actor_type", "actor_node_id", "actor_login"])

    # ---- reporting -------------------------------------------------------

    def _report(self, stats: BackfillStats, *, prefix: str, dry_run: bool = False) -> None:
        wrote = "would_write" if dry_run else "written"
        self.stdout.write(
            f"{prefix}scanned={stats.scanned} {wrote}={stats.rows_written} typed={stats.typed} "
            f"node_ids={stats.node_ids_filled} logins={stats.logins_filled} "
            f"null_actor={stats.null_actor} unresolved={stats.unresolved} "
            f"call_failed={stats.call_failed} unmodelled={stats.unmodelled_type} "
            f"api_calls={stats.api_calls} retries={stats.retries} points={stats.points_spent}"
        )
        if stats.distribution:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(stats.distribution.items(), key=lambda kv: (-kv[1], kv[0])))
            self.stdout.write(f"{prefix}distribution: {parts}")
