"""On-demand assignment suggestions — "what should I review?" (design doc 053).

Single authority for "which open PRs could this reviewer take right now, and why not the rest".
Both reviewer-facing surfaces (the ``suggest-prs`` Zulip command and the console suggestions page)
render this module's output; neither re-derives eligibility.

The inversion of the nightly *PR → reviewer* engine is one seam, not an engine change: the
requester's ``ReviewerProfile`` is replaced in the shared candidate catalog with an override copy
(availability and capacity forced open, optionally the label set replaced), and every downstream
engine call then sees the override. Key invariants (numbered as in the design doc):

1. Read-only and stateless: a suggestion is a pure function of (snapshot payload, live
   preference/proposal/opt-out state), computed and discarded. Never builds a snapshot.
2. Reproducible: only the trace's ``available`` membership is read — never the engine's weighted
   random ``picked`` — and the ranking's sort key is a total order, so identical requests against
   one snapshot generation return identical ordered results (and a smaller ``limit`` returns a
   strict prefix of a larger one).
4. Push-throttle preferences (``away_until``, ``auto_assign``, ``maximum_capacity``) are
   overridden by the explicit request; correctness rules (authorship, conflict-of-interest,
   opt-outs, cooldowns, assignment-forbidden labels, active assignees/proposals) never are.
5. One candidate pool: the assignable set comes from ``prepare_assignment_inputs``, shared with
   the nightly builder, so a suggestion can never offer what the scheduled run would refuse.
7. Capacity is reported, never enforced: ``load`` comes from the reviewer's *real*
   ``ReviewerPreference.maximum_capacity`` (via ``reviewer_load_for``), not the override, so the
   rendered load line stays the honest capacity signal.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Sequence

from django.conf import settings
from django.db.models.functions import Lower

from analyzer.models import QueueSnapshot
from analyzer.services.queue_rules import default_rule_set_for_repo
from analyzer.services.reviewer_assignment import _compute_weight, prepare_assignment_inputs
from analyzer.services.reviewer_assignment_engine import (
    ReviewerProfile,
    _normalize_login,
    _reviewer_candidate_state,
    _topic_labels,
    rank_prs_for_assignment,
    suggest_reviewer_for_pr_with_trace,
)
from analyzer.services.reviewer_load import ReviewerLoad, reviewer_load_for
from core.models import Repository
from core.services.topic_labels import TopicLabelMatcher, topic_label_matcher_for_repo
from syncer.models import LabelDef

# Result statuses. Only STATUS_OK carries suggestions; the others explain why there are none,
# so surfaces never have to guess whether an empty list means "nothing" or "could not answer".
STATUS_OK = "ok"
STATUS_NO_SNAPSHOT = "no_snapshot"
STATUS_NOT_A_REVIEWER = "not_a_reviewer"
STATUS_NO_LABELS = "no_labels"
STATUS_NONE_ELIGIBLE = "none_eligible"

# Skip-tally reasons, in the engine's own evaluation order (each pool PR is counted once against
# the first rule that excluded the requester). Deliberately no `at_capacity`: the requester's
# capacity is always overridden, so it can never be the reason *they* were skipped (Invariant 7).
SKIP_ALREADY_ASSIGNED = "already_assigned"
SKIP_NO_TOPIC_LABEL = "no_topic_label"
SKIP_AUTHORED = "authored"
SKIP_CONFLICT_OF_INTEREST = "conflict_of_interest"
SKIP_NO_AREA_MATCH = "no_area_match"
SKIP_OUTRANKED = "outranked"
SKIP_EXCLUDED = "excluded"


# Human phrasing for the skip tally, in the engine's evaluation order (also the render order).
# Shared by both surfaces so "why not more?" reads the same in Zulip and on the console.
_SKIP_PHRASES: dict[str, str] = {
    SKIP_ALREADY_ASSIGNED: "already assigned to you",
    SKIP_NO_TOPIC_LABEL: "with no topic label",
    SKIP_AUTHORED: "authored by you",
    SKIP_CONFLICT_OF_INTEREST: "conflict of interest",
    SKIP_NO_AREA_MATCH: "not matching your labels",
    SKIP_OUTRANKED: "outranked (another reviewer matches more of the PR's labels)",
    SKIP_EXCLUDED: "opted out or on cooldown",
}
_SKIP_ORDER: tuple[str, ...] = tuple(_SKIP_PHRASES)


def format_skip_summary(skipped: dict[str, int]) -> str:
    """One-line human rendering of the skip tally, e.g. ``312 not matching your labels, 7 outranked (…)``.

    Returns ``""`` for an empty tally. This is what makes an empty or short result legible instead
    of looking like a bug — ``outranked`` especially (see the design doc's Measured Baseline).
    """
    parts = [f"{skipped[reason]} {_SKIP_PHRASES[reason]}" for reason in _SKIP_ORDER if skipped.get(reason)]
    parts.extend(f"{count} {reason}" for reason, count in skipped.items() if reason not in _SKIP_PHRASES and count)
    return ", ".join(parts)


@dataclass(frozen=True)
class SuggestedPR:
    pr_number: int
    title: str
    url: str
    author_login: str
    topic_labels: list[str]
    matched_labels: list[str]  # intersection with the effective label set
    queue_age_seconds: float | None
    available_reviewer_count: int  # scarcity, against the REAL catalog (never the override)
    load_weight: float  # what claiming it would add to the reviewer's load


@dataclass(frozen=True)
class SuggestionResult:
    repository_id: int
    reviewer_login: str  # normalized
    effective_labels: list[str]  # the override set, else the reviewer's preferred_labels
    label_override: bool
    unknown_labels: list[str]  # requested labels that are not topic labels in this repo
    dropped_labels: list[str]  # requested labels past MAX_LABELS — used by neither, reported to both
    load: ReviewerLoad | None  # from the reviewer's REAL capacity — never the override
    suggestions: list[SuggestedPR]
    skipped: dict[str, int]  # reason -> count over the assignable pool
    snapshot_generated_at: datetime | None
    status: str  # ok | no_snapshot | not_a_reviewer | no_labels | none_eligible


def _resolve_label_override(
    repository: Repository,
    labels: Sequence[str] | None,
    *,
    matcher: TopicLabelMatcher,
) -> tuple[list[str], list[str], list[str]]:
    """Split the requested override labels into normalized ``(known, unknown, dropped)`` lists.

    Normalizes (strip + lowercase) and dedupes preserving request order. A label is *known* when
    it matches the repo's topic-label pattern AND exists in the repo's synced label catalog
    (``LabelDef``) — so typos and non-topic labels come back in ``unknown`` instead of silently
    yielding nothing. ``(labels or [])`` with no usable tokens means "no override".

    Labels past ``ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS`` come back in ``dropped``. The cap
    is applied *before* the known/unknown split, so without this third list they would land in
    neither and the request would be silently narrowed — the broad label override is the feature's
    biggest unlock, which makes quietly honouring five of eight labels the worst place to be quiet.
    """
    requested: list[str] = []
    seen: set[str] = set()
    for raw in labels or []:
        norm = str(raw or "").strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        requested.append(norm)
    if not requested:
        return [], [], []
    max_labels = int(settings.ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS)
    requested, dropped = requested[:max_labels], requested[max_labels:]
    catalog_lower = set(
        LabelDef.objects.filter(repository=repository)
        .annotate(name_lower=Lower("name"))
        .filter(name_lower__in=requested)
        .values_list("name_lower", flat=True)
    )
    known = [lab for lab in requested if matcher(lab) and lab in catalog_lower]
    known_set = set(known)
    unknown = [lab for lab in requested if lab not in known_set]
    return known, unknown, dropped


def _classify_skip(
    *,
    pr_entry: dict,
    requester: ReviewerProfile,
    requester_norm: str,
    trace: dict,
    matcher: TopicLabelMatcher,
) -> str:
    """Attribute one skipped (requester, PR) pair to the first engine rule that excluded them.

    Reads the trace's ``potential`` membership (max_score survivors) plus a little local
    classification; the checks mirror ``_reviewer_candidate_state`` in its evaluation order so
    each pair lands on exactly one reason.
    """
    topic_lower = {lab.lower() for lab in _topic_labels(pr_entry, matcher)}
    if not topic_lower:
        return SKIP_NO_TOPIC_LABEL
    author_norm = _normalize_login(pr_entry.get("author"))
    if author_norm == requester_norm:
        return SKIP_AUTHORED
    if author_norm in requester.conflict_of_interest_lower:
        return SKIP_CONFLICT_OF_INTEREST
    if not (topic_lower & requester.preferred_labels_lower):
        return SKIP_NO_AREA_MATCH
    potential_norm = {_normalize_login(login) for login in trace.get("potential", [])}
    if requester_norm not in potential_norm:
        # Matched an area but was dropped by the engine's max_score contest: another reviewer
        # matched more of this PR's labels. The non-obvious reason an eligible-looking PR is
        # missing — see the design doc's Measured Baseline.
        return SKIP_OUTRANKED
    # In `potential` but not `available` with availability + capacity overridden: the only
    # remaining filter is the per-PR exclusion set (opt-out / expired-proposal cooldown).
    return SKIP_EXCLUDED


def suggest_prs_for_reviewer(
    repository: Repository,
    reviewer_login: str,
    *,
    labels: Sequence[str] | None = None,
    limit: int | None = None,  # None -> ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT
    now: datetime | None = None,
) -> SuggestionResult:
    """Open PRs the reviewer could take right now, ranked as the nightly builder would rank them.

    Read-only (Invariant 1): reads the cached queue snapshot for the repo's default rule set —
    never builds one — and persists nothing. ``labels`` *replaces* the reviewer's stored
    ``preferred_labels`` for this request; the request also overrides ``away_until``,
    ``auto_assign`` and ``maximum_capacity`` (push throttles, Invariant 4), while authorship,
    conflict-of-interest, opt-outs, cooldowns and the pool filters stay in force.
    """
    current_time = now or datetime.now(timezone.utc)
    effective_limit = int(settings.ANALYZER_ASSIGNMENT_SUGGESTIONS_LIMIT) if limit is None else int(limit)
    requester_norm = _normalize_login(reviewer_login)

    def _result(**overrides) -> SuggestionResult:
        base = dict(
            repository_id=int(repository.id),
            reviewer_login=requester_norm,
            effective_labels=[],
            label_override=False,
            unknown_labels=[],
            dropped_labels=[],
            load=None,
            suggestions=[],
            skipped={},
            snapshot_generated_at=None,
            status=STATUS_OK,
        )
        base.update(overrides)
        return SuggestionResult(**base)

    rule_set = default_rule_set_for_repo(repository)
    cache_key = str(rule_set.id) if rule_set else "default"
    snapshot = QueueSnapshot.objects.filter(repository=repository, cache_key=cache_key).order_by("-generated_at").first()
    if snapshot is None or not snapshot.payload:
        # No snapshot means no answer — never a fabricated empty list.
        return _result(status=STATUS_NO_SNAPSHOT)
    payload = snapshot.payload
    generated_at = snapshot.generated_at
    # A stale snapshot is no better than a missing one and fails far more quietly. When a repo has
    # no *active* rule set, `cache_key` falls back to the literal "default" — where a long-dead row
    # can still be sitting (production carries one generated five months ago). Without a ceiling the
    # guard above is satisfied by that row and the feature serves months-old PRs as though live,
    # which is exactly the fabricated answer Invariant 1 exists to prevent. `<= 0` disables it.
    max_age_seconds = int(settings.ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_SNAPSHOT_AGE_SECONDS)
    if max_age_seconds > 0 and (current_time - generated_at).total_seconds() > max_age_seconds:
        return _result(status=STATUS_NO_SNAPSHOT, snapshot_generated_at=generated_at)

    # One candidate pool, shared with the nightly builder by construction (Invariant 5).
    inputs = prepare_assignment_inputs(repository, payload=payload, now=current_time, rule_set=rule_set)
    profile = next((r for r in inputs.reviewers if _normalize_login(r.github_login) == requester_norm), None)
    if profile is None:
        return _result(status=STATUS_NOT_A_REVIEWER, snapshot_generated_at=generated_at)

    matcher = topic_label_matcher_for_repo(repository)
    known_labels, unknown_labels, dropped_labels = _resolve_label_override(repository, labels, matcher=matcher)
    label_override = bool(known_labels or unknown_labels or dropped_labels)

    # The request profile: an explicit request overrides the push throttles (Invariant 4). The
    # capacity override is unconditional (Invariant 7) — the load line below carries the honest
    # capacity signal instead.
    override_kwargs: dict = {"auto_assign": True, "temporary_break": False, "maximum_capacity": sys.maxsize}
    if label_override:
        override_kwargs["preferred_labels"] = list(known_labels)
        override_kwargs["preferred_labels_lower"] = set(known_labels)
    requester = replace(profile, **override_kwargs)
    effective_labels = list(known_labels) if label_override else list(profile.preferred_labels)

    # The load line comes from the same payload but the REAL preference capacity — never the
    # sys.maxsize override (Invariant 7) — and is computed even for degenerate statuses below so
    # surfaces can always render it.
    load = reviewer_load_for(repository, requester_norm, snapshot_payload=payload, now=current_time)

    common = dict(
        effective_labels=effective_labels,
        label_override=label_override,
        unknown_labels=unknown_labels,
        dropped_labels=dropped_labels,
        load=load,
        snapshot_generated_at=generated_at,
    )
    if not requester.preferred_labels_lower:
        return _result(status=STATUS_NO_LABELS, **common)

    # Every downstream engine call sees the override — the substitution is the whole of the
    # "explicit request" semantics; the engine itself is unmodified.
    catalog = [requester if _normalize_login(r.github_login) == requester_norm else r for r in inputs.reviewers]
    all_prs = payload.get("prs", {})

    ranked_prs, ranking_trace = rank_prs_for_assignment(
        prs_to_assign=inputs.assignable_queue_prs,
        all_prs=all_prs,
        reviewers=catalog,
        assignment_stats=inputs.assignments,
        excluded_by_pr=inputs.excluded_by_pr,
        topic_label_matcher=matcher,
    )

    # A fixed local RNG: the engine's weighted draw ("picked") is computed but never read
    # (Invariant 2); seeding locally also keeps this read path from consuming global randomness.
    rng = random.Random(0)
    suggestions: list[SuggestedPR] = []
    skipped: dict[str, int] = {}
    # Walk the whole ranking (cheap next to the payload read) so the skip tally covers the entire
    # assignable pool; suggestions stop accumulating at `limit`.
    for pr_number in ranked_prs:
        pr_entry = all_prs.get(pr_number) or all_prs.get(str(pr_number))
        if not pr_entry:
            continue
        # The pool's active-assignee filter reads *real* availability, so an away/auto-assign-off
        # requester's own assigned PRs survive into the shared pool. Never offer a reviewer a PR
        # they are already assigned to (mirrors the pool filter, scoped to the requester).
        assignees_norm = {_normalize_login(str(login)) for login in (pr_entry.get("assignees") or []) if login}
        if requester_norm in assignees_norm:
            skipped[SKIP_ALREADY_ASSIGNED] = skipped.get(SKIP_ALREADY_ASSIGNED, 0) + 1
            continue
        _unused_result, trace = suggest_reviewer_for_pr_with_trace(
            pr_entry=pr_entry,
            reviewers=catalog,
            assignment_stats=inputs.assignments,
            rng=rng,
            excluded_logins=inputs.excluded_by_pr.get(pr_number),
            topic_label_matcher=matcher,
        )
        available_norm = {_normalize_login(login) for login in trace.get("available", [])}
        if requester_norm not in available_norm:
            reason = _classify_skip(
                pr_entry=pr_entry,
                requester=requester,
                requester_norm=requester_norm,
                trace=trace,
                matcher=matcher,
            )
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        if len(suggestions) >= effective_limit:
            continue
        details = (ranking_trace.get(str(pr_number)) or {}).get("details") or {}
        topic_labels = _topic_labels(pr_entry, matcher)
        queue_age = details.get("queue_age_seconds")
        # Scarcity is recomputed against the REAL catalog rather than read from the ranking's
        # `details`. The ranking necessarily runs over the override catalog, where the requester
        # is unconditionally available — so `details["available_reviewer_count"]` counts them even
        # when they are really at capacity or away, overstating supply by one to exactly the
        # reviewer reading the number, and most where it matters (a "1 available reviewer" PR is
        # really 0). The ranking itself is deliberately left on the override catalog; only this
        # displayed count is honest. Bounded by `limit`, so at most `limit` extra evaluations.
        _, real_available, _, _ = _reviewer_candidate_state(
            pr_entry=pr_entry,
            reviewers=inputs.reviewers,
            assignment_stats=inputs.assignments,
            excluded_logins=inputs.excluded_by_pr.get(pr_number),
            topic_label_matcher=matcher,
        )
        suggestions.append(
            SuggestedPR(
                pr_number=int(pr_number),
                title=str(pr_entry.get("title") or ""),
                url=f"https://github.com/{repository.owner}/{repository.name}/pull/{int(pr_number)}",
                author_login=str(pr_entry.get("author") or ""),
                topic_labels=topic_labels,
                matched_labels=[lab for lab in topic_labels if lab.lower() in requester.preferred_labels_lower],
                queue_age_seconds=float(queue_age) if queue_age is not None else None,
                available_reviewer_count=len(real_available),
                load_weight=_compute_weight(int(pr_number), pr_entry),
            )
        )

    return _result(
        suggestions=suggestions,
        skipped=skipped,
        status=STATUS_OK if suggestions else STATUS_NONE_ELIGIBLE,
        **common,
    )


__all__ = [
    "SKIP_ALREADY_ASSIGNED",
    "SKIP_AUTHORED",
    "SKIP_CONFLICT_OF_INTEREST",
    "SKIP_EXCLUDED",
    "SKIP_NO_AREA_MATCH",
    "SKIP_NO_TOPIC_LABEL",
    "SKIP_OUTRANKED",
    "STATUS_NO_LABELS",
    "STATUS_NO_SNAPSHOT",
    "STATUS_NONE_ELIGIBLE",
    "STATUS_NOT_A_REVIEWER",
    "STATUS_OK",
    "SuggestedPR",
    "SuggestionResult",
    "format_skip_summary",
    "suggest_prs_for_reviewer",
]
