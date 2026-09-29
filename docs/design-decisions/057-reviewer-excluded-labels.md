# Per-Reviewer Excluded Labels

## Context
- Reviewers asked for a preference: "never auto-assign me a PR carrying any of these labels".
- The repo-wide analogue already exists: `QueueRuleSet.assignment_forbidden_label_names` withholds a
  PR from auto-assignment for *everyone*. This is the per-reviewer version.
- The nearest per-reviewer rule is `ReviewerPreference.conflict_of_interest` ("never assign me PRs
  by these authors"). It filters a reviewer out before the engine's label matching, and it is
  private.
- The labels reviewers most want to avoid (e.g. `LLM-generated`) are usually not topic labels, so
  matching only the topic labels the engine scores on would miss the point.
- Assignment decisions are made in three places that must agree: the nightly snapshot builder
  (`analyzer/services/reviewer_assignment_engine.py`), the apply/propose executors that act on a
  snapshot up to ~a day later, and on-demand suggestions (design doc 053).

## Decision
- **Storage.** `ReviewerPreference.excluded_labels`: a JSON list of label names, the same shape as
  `preferred_labels` (design doc 003), with no FK to `syncer.LabelDef`. Migration `core/0009`.
- **Matching.** Checked against *every* label on the PR, not only topic labels. Matching ignores
  case, like the other label lists.
- **Where the engine checks it.** A reviewer who excludes a label the PR carries is dropped
  **before label matching**, next to the conflict-of-interest check. This is done in both copies of
  the candidate logic (`_reviewer_candidate_state` and `suggest_reviewer_for_pr_with_trace`). It is
  deliberately *not* folded into `excluded_by_pr`, the per-PR opt-out/cooldown set: that set is
  applied *after* the `max_score` contest, so an excluded reviewer who matched the most labels would
  win the contest and then be dropped, leaving the PR with nobody. Example: PR
  `{t-algebra, t-number-theory, LLM-generated}`, where A prefers both `t-*` labels but excludes
  `LLM-generated`, and B prefers only `t-algebra`. With this decision B gets the PR; with
  `excluded_by_pr` nobody would.
- **Profile.** `ReviewerProfile.excluded_labels_lower: frozenset[str] = frozenset()`, filled in by
  `build_reviewer_catalog`. The default keeps every construction site that predates this unchanged,
  as design doc 054 did for its fields.
- **Execution-time re-check.** Apply and propose re-read live `syncer.PRLabel` rows
  (`reviewer_assignment._excluded_label_logins_for_prs`), as they already do for opt-outs, because a
  label can be added after the snapshot was computed. Apply records
  `ReviewerAssignmentApplication.STATUS_SKIPPED_EXCLUDED_LABEL` (a choices-only change, migration
  `analyzer/0032`); both paths report a `skipped_excluded_label` stat. When no reviewer excludes
  anything, the re-check runs no query.
- **On-demand suggestions honour it.** In design doc 053's split between push throttles, which a
  request may override, and correctness rules, which it may not, this is a correctness rule: it
  records a standing decision about specific PRs, not about how much work the reviewer can take. The
  skip reason is `excluded_label` ("with a label you excluded"), checked after
  `conflict_of_interest`. A `--labels` override replaces preferred labels only and cannot lift an
  exclusion.
- **Form** (`core/forms.py`, console `/console/preferences/`):
  - A comma/newline textarea, validated against the repo's whole `LabelDef` catalog. A checkbox grid
    was ruled out because the full catalog is too long to tick through.
  - Labels are saved in the catalog's spelling.
  - Unknown labels are **rejected**, because a typo would silently void the preference.
  - A label that is also a preferred label is rejected, rather than letting either silently win.
  - A label already saved that has since left the catalog (renamed or deleted on GitHub) is kept, so
    an unrelated edit still saves, and the page flags it.
  - The help text says the field is private, since its Interests-section neighbours are public.
- **Privacy.** Private, like `conflict_of_interest`:
  - Never served by the public `/api/v1/reviewer-interests` endpoint (pinned by a test), and never
    used in public area stats.
  - Engine traces name the reviewers each rule filtered, so they are private too. They live only in
    Celery task results (admin-visible).
  - `core_reviewerpreference`, `django_celery_results_taskresult` and
    `analyzer_reviewerassignmentapplication` are all wiped from the sanitized public backups
    (`scripts/backup_policy.py`), so the backup policy needs no change.
  - The `suggest-prs` skip tally, which now includes this count, stays out of streams. The
    production `ZULIP_COMMAND_POLICY` restricts `suggest-prs` to DMs, and the companion DM-only
    change (design doc 053, 2026-09-29) makes the command itself reply by DM, so this holds even
    where a deployment's policy allows streams.
- **Import/export.** The admin-only reviewer-topics.json export includes `excluded_labels`. An
  import without the key leaves the stored value alone, the same rule design doc 054 uses. Import is
  an operator path and does not validate against the catalog.

## Consequences
- The guarantee is "never *auto*-assigned, proposed or suggested". Explicit human actions stay
  unrestricted: Zulip `assign`, accepting a proposal, and console "assign anyway".
- Existing assignments and pending proposals are left alone when an excluded label appears later.
  The reviewer can unassign or decline.
- Excluded reviewers no longer count as supply for the PR, both in the ranking's
  `available_reviewer_count` and in the scarcity shown on suggestions, which is the honest figure.
- The rule is enforced in four places: the two engine copies, apply/propose, and the suggestions
  skip classifier (plus `scripts/probe_053_suggestions.py`, which mirrors it). Tests cover each, and
  a mutation check confirmed that disabling the engine check fails them.
- As with `preferred_labels`, there is no referential integrity. A rename on GitHub turns an entry
  into a no-op; the form flags it but does not rewrite it.

## Operational Notes
- Migrations: `core/0009_reviewerpreference_excluded_labels` (adds a column) and
  `analyzer/0032_alter_reviewerassignmentapplication_status` (choices only, no DDL).
- No new settings and no feature flag. The field defaults to `[]`, which does nothing.
- Order: independent of the `suggest-prs` DM-only change in production, where the command policy
  already limits `suggest-prs` to DMs. A deployment whose policy allows `suggest-prs` in streams
  should take the DM-only change first, or it would post the excluded-label skip count there.
- Debugging: a reviewer missing from an assignment shows up in the trace's
  `filtered["excluded_label"]`; an apply-time skip shows as status `skipped_excluded_label` on the
  `ReviewerAssignmentApplication` row.
- Deferred: retiring a pending proposal when an excluded label appears on its PR. This would be a
  new `proposal_validity` reason.

## Alternatives
- **Reuse `excluded_by_pr`** (the opt-out/cooldown channel). Less code, but the exclusion would be
  applied after the `max_score` contest and could leave PRs unassigned (see Decision).
- **Topic labels only.** Misses the main use case, which is non-topic labels.
- **Warn on unknown labels instead of rejecting them.** A warning is easy to miss, and the failure
  it leaves behind is silent.
- **Respect the exclusion only in the nightly push, not in suggestions.** The reviewer would then
  be offered, on request, the very PRs they said they never want.
