"""Rolling-window intake counts — the *flow* half of reviewer capacity (design doc 054).

``ReviewerPreference.maximum_capacity`` bounds the **stock** a reviewer holds at once; it does not
bound **flow**. A reviewer who clears PRs quickly frees the slot and the next nightly run refills
them, so the concurrent cap only ever bit reviewers who *didn't* act (measured: reviewers with
``maximum_capacity=10`` taking 22–30 new PRs in 30 days). ``max_new_assignments_per_week`` bounds
the flow instead, and this module is the single place that answers the question it needs:

> how many **distinct PRs** has this reviewer been newly assigned in this repository within the
> trailing ``ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS``?

Two consumers share it so they cannot disagree: ``build_reviewer_catalog`` (which puts the count on
every ``ReviewerProfile``, where the engine gate reads it) and, through the same profiles, the
reviewer-facing load line. A reviewer whose push went quiet sees the same number that silenced it.

Counting notes, each load-bearing:

- **Source is** ``analyzer.ReviewerAssignmentApplication`` **(design doc 046)** — an append-only,
  indefinitely-retained row per *system-mediated* assignment: the nightly auto direct-assign,
  confirm-mode accepts, and console pull-claims all route through ``assign_reviewer_and_record``.
  A raw Zulip ``assign`` self-assign writes no row and so does not count (design doc 053's known
  audit asymmetry, inherited rather than introduced here — see 054 Subtlety 4).
- **Distinct PRs, not rows.** A PR that is auto-unassigned by the attention sweep and later
  re-assigned writes several ``applied`` rows; the limit counts "new PRs", so it counts once. On
  production this currently saves nothing (zero re-assignment churn in 67 days) — it is insurance
  against the sweep producing a repeat, where row-counting would then be wrong.
- **Case-insensitive on both sides.** ``ReviewerAssignmentApplication.reviewer_login`` is stored
  **verbatim**, not normalized: the nightly paths pass engine logins while the console accept and
  the 053 claim pass ``User.github_login``, which keeps GitHub's original casing (``core_user``
  enforces only case-*insensitive* uniqueness). Measured, 11 of 41 production reviewers are stored
  capitalized, and this failure mode is not a partial undercount but a **zero** — a case-sensitive
  filter would silently exempt a quarter of the population from limits they had opted into. The
  ``Lower()`` on the column and the normalization of the requested logins are both required.
- **Only** ``status='applied'`` **counts**, and ``applied_at`` is the clock. Proposed-but-unaccepted
  work is bounded by the concurrent cap instead (054 Subtlety 5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from django.conf import settings
from django.db.models import Count
from django.db.models.functions import Lower

from analyzer.models import ReviewerAssignmentApplication
from core.models import Repository


def normalize_login(login: str | None) -> str:
    """Lowercase/strip a login into the key space this module counts in."""
    return (login or "").strip().lower()


def assignment_rate_window_days() -> int:
    """The configured rolling window, in days (``ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS``).

    Read through one helper so every call site — catalog build, surfacing, UI copy — measures and
    *describes* the same period. The setting defines what "per week" means for every reviewer's
    stored number, so it is deliberately global rather than per-reviewer.
    """
    return int(settings.ANALYZER_ASSIGNMENT_RATE_WINDOW_DAYS)


def recent_assignment_counts(
    repository: Repository,
    logins: Sequence[str],
    *,
    window_days: int,
    now: datetime,
) -> dict[str, int]:
    """Distinct newly-assigned PRs per reviewer in the trailing window, keyed by normalized login.

    Counts ``applied`` ``ReviewerAssignmentApplication`` rows for ``repository`` whose
    ``applied_at`` is at or after ``now - window_days``, deduplicated by ``pr_number`` per reviewer.
    Every requested login appears in the result — a reviewer with no intake maps to ``0`` — so
    callers can index without a default and never confuse "no rows" with "not asked about".

    ``window_days <= 0`` disables the count (all zeros) rather than degenerating into "everything
    since the epoch", which would silently block every limited reviewer.
    """
    normalized = sorted({key for key in (normalize_login(login) for login in logins) if key})
    counts: dict[str, int] = {login: 0 for login in normalized}
    if not normalized or int(window_days) <= 0:
        return counts

    since = now - timedelta(days=int(window_days))
    rows = (
        ReviewerAssignmentApplication.objects.filter(
            repository=repository,
            status=ReviewerAssignmentApplication.STATUS_APPLIED,
            applied_at__gte=since,
        )
        .annotate(login_lower=Lower("reviewer_login"))
        .filter(login_lower__in=normalized)
        # The model has a Meta.ordering; without clearing it Django folds those columns into the
        # GROUP BY and the aggregate below fragments into one row per (login, run_date, pr, id).
        .order_by()
        .values("login_lower")
        .annotate(pr_count=Count("pr_number", distinct=True))
    )
    for row in rows:
        counts[str(row["login_lower"])] = int(row["pr_count"])
    return counts


__all__ = [
    "assignment_rate_window_days",
    "normalize_login",
    "recent_assignment_counts",
]
