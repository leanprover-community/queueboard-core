# Reviewer Console Guidelines

## Scope
- `qb_site/console/` is the GitHub-OAuth authenticated **reviewer console**: a `confirm`-mode
  reviewer signs in and accepts/declines the assignment proposals made to them (design decision 050),
  and edits their reviewer preferences at `/console/preferences/` (design decision 022).
- It is a plain server-rendered Django app (no DRF, no models of its own). State lives in
  `analyzer.AssignmentProposal` / `analyzer.ReviewerOptOut`; identity is `core.User`.
- Mounted at `/console/` (`qb_site/qb_site/urls.py`). Templates in `qb_site/templates/console/`.

## Styling
- Console pages share the reviewer-facing design system with the `zulip_bot` flows (registration,
  prefs, close/label PR): the palette tokens, `.page`/`.hero`/`.card`/`.cta`/`.status`/`.pr-label`
  components live in the app-neutral `qb_site/static/shared/shared_pages.css`
  (`{% static 'shared/shared_pages.css' %}`). Console-only bits (proposal cards, accept/decline
  buttons, load line, assigned-PR roster) live in `qb_site/console/static/console/console.css` and
  are built on the shared CSS variables — do not hardcode colors. `templates/console/base.html`
  links both stylesheets and wraps content in `<main class="page">`.
- The shared system is intentionally light-only (matching the other reviewer pages); don't add a
  console-specific `prefers-color-scheme` block. When adjusting shared components, remember the
  `zulip_bot` templates consume the same file.
- **`.meta` is hero-only.** It is `color: #d8f3f2`, pale text for the dark teal `.hero` gradient; on a
  white `.card` or the page background it is nearly invisible. For secondary text there use `.help`
  (`var(--muted)`), as `unavailable.html` does. For a context strip above a form, use `.form-context`
  (shares the `.countdown` chrome from `prefs_form.css`, minus the expiry semantics).
- Django strips `{# … #}` comments **only on a single line**. A multi-line one renders as visible page
  text — use `{% comment %}` instead. `console/tests/test_prefs.py` guards the prefs page against
  raw template syntax reaching the reader.

## Home dashboard (`views.home` / `_build_home_context`)
- Beyond pending proposals, the home page is a small per-repo dashboard. A repo earns a section only
  when the reviewer has **a proposal or ≥1 assigned open PR** there; each section shows:
  - a **load line** from `analyzer.services.reviewer_load` (`reviewer_load_with_breakdown` +
    `format_load_line`): the weighted, engine-matching load incl. pending proposals, so the number
    agrees with the `assigned-prs` command and the daily digest. Absent a queue snapshot, no load
    line is rendered.
  - **assigned open PRs with status**, sourced from `analyzer.services.reviewer_attention`
    (`build_reviewer_attention_reports`) — the same authority the `assigned-prs` command uses. When
    `ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED` is on, the roster is a checkbox form
    posting to `console:unassign` (see below).
  - the **proposals** to accept/decline.
  - a **per-PR load contribution** (`+1` / `+0.1` / `+0`) next to each assigned PR and proposal, from
    `reviewer_load.pr_load_breakdown` folded over the *same* snapshot as the aggregate, so the parts
    sum to the load line. Rendered only when a load line exists. The load line itself is a
    `<details>` whose disclosure reveals the load legend (`templates/console/_load_legend.html`,
    a shared partial) — explanation lives where the number is, not as a standing block up top.
- Do not re-derive load or assigned-PR facts here; reuse `reviewer_load` / `reviewer_attention`. The
  load + per-PR breakdown come from one `reviewer_load_with_breakdown` call (single snapshot read).

## Auth model
- **Not** Django admin auth. A reviewer needs no Django account — only a GitHub identity.
- Login flow (`views.login` → `views.oauth_callback`): token-less, bookmarkable URL → GitHub OAuth
  → resolve the `core.User` → store its id in the Django session (`console.session`).
  - OAuth client: `core.services.github_oauth.GitHubOAuthClient` (shared with registration).
  - CSRF: a random nonce is stored in the session and echoed in the Fernet-signed `state`
    (`core.services.oauth_state.issue_console_oauth_state`); the callback requires them to match.
  - Identity → user: `core.services.github_identity.resolve_user_from_identity`
    (Zulip-agnostic; never touches `zulip_user_id`). **Resolve-only by construction** — an unknown
    GitHub account (or a recycled login whose node id no longer matches) gets a 403 instead of
    minting a `core.User`; only people already known (registered via the Zulip flow, or ingested
    by the syncer) can open a session.
- Console access is keyed on the authenticated `github_login`, matched **case-insensitively**
  against `AssignmentProposal.reviewer_login`. A reviewer can only act on their own proposals.
- **A second place opens this session.** `zulip_bot.views.register_github_callback` calls
  `console.session.set_reviewer` after a successful registration (when the new reviewer has preference
  rows), so they land on the prefs page already signed in. It is the same GitHub-OAuth verification the console does, plus a Zulip-identity proof. Any
  future caller of `set_reviewer` outside this app must clear that same bar.
- **Admission gate (`_is_reviewer` / `_reviewer_from_session`).** Resolve-only is not reviewer-only:
  `syncer` upserts a `core.User` for every PR author, so a "known GitHub account" is usually just a
  contributor. A session is granted only to a user with ≥1 `ReviewerPreference` row **or** an active
  `AssignmentProposal` for their login (the OR clause keeps a reviewer whose row was removed
  mid-flight able to answer the proposal already made to them). Enforced in `oauth_callback` (refuse
  the session, 403 `NOT_A_REVIEWER_MESSAGE`) *and* on every view via `_reviewer_from_session`, which
  returns `(reviewer, None)` / `(None, None)` for "not signed in" / `(None, 403 response)`. Route new
  views through it rather than calling `console_session.get_reviewer` directly.

## Preferences page (`views.prefs`)
- `/console/preferences/` is **the** place reviewers edit their preferences (design doc 022; the
  expiring Zulip token page it replaced is gone, and the `CONSOLE_PREFS_ENABLED` flag that staged the
  rollout was removed with it). The `prefs` Zulip command DMs this URL.
- Do not re-implement the form here. `core.forms.ReviewerPreferenceForm` owns the editable fields and
  validation, `core.services.reviewer_prefs.build_preferences_formset` owns formset assembly, and
  `templates/shared/_reviewer_prefs_fields.html` owns the field markup. They live in `core`/`shared`
  because the *model* is `core`'s, not because a second page still renders them.
- **This page never creates `ReviewerPreference` rows.** A row *is* assignment-pool membership
  (`analyzer.services.reviewer_assignment.build_reviewer_catalog`) and `auto_assign` defaults to
  `True`, so a "create my preferences" affordance on a public sign-in surface would let any
  contributor into the pool. A reviewer with no rows gets an explanatory empty state pointing at the
  Zulip bot; bootstrapping stays in `zulip_bot.services.registration_bootstrap`.
- Ownership scoping lives in the shared builder: the queryset is narrowed to the supplied rows *and*
  their owner on GET and POST alike, so a posted `form-<n>-id` naming someone else's row fails
  validation. Never widen that queryset at a call site.
- Timezone comes from `zulip_bot.services.user_timezone.resolve_user_timezone_name` (Zulip's zone →
  `core.User.timezone` → Django default) and is what naive `away_until` input means. The page renders
  no countdown: the session bounds it, and `SESSION_SAVE_EVERY_REQUEST` slides that window.
- Styling/JS: `templates/console/prefs.html` extends `console/base.html` and uses its `extra_css` /
  `extra_js` blocks to load `console/prefs_form.css|js`, both console-owned. The JS is progressive
  enhancement only (unsaved-changes guard, "clear away time" buttons) with **no countdown** — the
  session bounds this page, not a link TTL — which is why the expiry helpers stayed behind in
  `zulip_bot/static/zulip_bot/expiry.js` for the close-pr / label-pr token pages. Its vitest spec lives
  with those, in `qb_site/zulip_bot/frontend/tests/`.

## Accept / decline / assign-anyway / unassign (the load-bearing handlers)
- All `POST`. Accept and decline re-validate live state via the single
  `analyzer.services.assignment_proposal_validity.proposal_validity` authority before acting; a
  no-longer-actionable proposal renders `unavailable.html` (and the stale row is retired to its
  terminal state) instead of erroring.
- **Accept**: gated by `ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED`. When on, reuses
  the verbatim 046 mutation path via the shared `_github_assign_self` helper
  (`analyzer.services.reviewer_assignment_apply.assign_reviewer_and_record`: GitHub assign +
  `ReviewerAssignmentApplication` + `syncer.sync_pr`), then marks the proposal `accepted` (conditional
  `UPDATE ... WHERE state='proposed'`). When off, it leaves the proposal pending and tells the
  reviewer — never records an acceptance it cannot fulfil. The "did it actually land?" check
  (`applied`, or `already_recorded` only when the row is `APPLIED`) lives in `_github_assign_self`.
- **Assign-anyway** (`console:assign-anyway`): the escape hatch shown on `unavailable.html`. It
  **deliberately bypasses the validity gate** — the point is to let a reviewer take a PR whose
  proposal lapsed/was superseded/declined — but keeps one honest precondition, `_can_self_assign`
  (PR is open AND the reviewer is not already an assignee). That precondition *is* the "all
  recoverable states" rule: it admits expired / off-queue / assigned-to-others / opted-out and
  excludes closed-merged and already-held PRs, with no per-reason enumeration. Same ownership check,
  `ASSIGN_ON_ACCEPT` gate, and `_github_assign_self` mutation as accept; on success it also clears
  any active per-PR `ReviewerOptOut` (so the builder won't undo the just-made assignment) and retires
  a still-pending proposal to `accepted`.
- **Decline**: marks the proposal `declined` and upserts an active `ReviewerOptOut` (permanent
  per-PR "no", enforced by the builder). No GitHub write.
- **Unassign** (`console:unassign`): self-service removal from one or more assigned PRs, gated by
  `ANALYZER_ASSIGNMENT_PROPOSALS_CONSOLE_UNASSIGN_ENABLED`. Posts `repo_id` + `pr_numbers[]`. The
  login removed is **always the authenticated reviewer's own**, never taken from the request, so this
  surface can only ever unassign the person operating it. Uses `GitHubAssignmentClient.unassign` with
  the `unassign_pr` operation token, confirms the reviewer actually left the assignee set, enqueues a
  per-PR sync per success, and renders `unassigned.html` with the removed/failed split (partial
  failures are contained, not fatal).

## Suggestions page + claim (`views.suggestions` / `views.claim`, design doc 053)
- `/console/suggestions/` ("Find PRs to review", `console:suggestions`) renders on-demand
  assignment suggestions per repo from the single authority
  `analyzer.services.assignment_suggestions.suggest_prs_for_reviewer` — the console never
  re-derives eligibility. Read path is gated by `ANALYZER_ASSIGNMENT_SUGGESTIONS_ENABLED`; linked
  from the home header row and the home empty state (which was previously a dead end).
- `?repo=<id>` and `?labels=a,b` pre-fill the request (the Zulip `suggest-prs` footer link carries
  them) but are **validated, never trusted**: the repo must be one the session reviewer has a
  `ReviewerPreference` in (otherwise ignored), and labels go through the service's
  `ANALYZER_ASSIGNMENT_SUGGESTIONS_MAX_LABELS` cap and unknown-label reporting. The reviewer whose
  suggestions are computed is always the session reviewer.
- Each repo section shows the honest load line (`reviewer_load`-derived, the reviewer's *real*
  capacity — never the request's capacity override; Invariant 7), a label-override search form, the
  suggestion rows (matched labels highlighted, queue age, scarcity, per-PR load contribution), and
  the `format_skip_summary` "why not more?" line.
- `POST /console/suggestions/claim/` (`console:claim`, gated by
  `ANALYZER_ASSIGNMENT_SUGGESTIONS_CONSOLE_CLAIM_ENABLED` on top of the read flag) takes
  `repo_id` + `pr_numbers[]` + the `labels` override the offer was made under. Every posted number
  is **re-verified against a fresh `suggest_prs_for_reviewer` run** before any GitHub write
  (Invariant 6 — without it this endpoint degrades into a general self-assign API bypassing
  conflict-of-interest/opt-out rules), and the login assigned is always the session reviewer's own.
  Assignment reuses the 046 path (`assign_reviewer_and_record`, `assign_pr` operation token,
  `snapshot=None`), with the same "did it land?" semantics as accept. Partial failures render as an
  assigned/failed split (`claimed.html`), which also surfaces co-assignees — there is no hold on a
  suggested PR (Invariant 8), so two claimers legally share the assignee set.

## Base URL
- Absolute links (the DM console URL, the OAuth `redirect_uri`) come from
  `core.services.site_urls.build_site_url`, which resolves `QUEUEBOARD_BASE_URL`. Do not read the
  setting directly.

## Testing
- `docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test console`
- This app is step `[12/13]` of `scripts/repo_check_compose.sh`. It was missing from that script until
  the prefs work added it, so console tests had never run in CI — if you add an app, add its step.
- View tests mock `console.views.GitHubOAuthClient`, `console.views.assign_reviewer_and_record`,
  `console.views.GitHubAssignmentClient` (unassign), `console.views._enqueue_pr_sync`, and
  `console.views.resolve_user_timezone_name` (prefs), and seed the session directly; no real GitHub
  or Zulip calls. `tests/test_prefs.py` pins the prefs invariants (no row creation, admission gate at
  sign-in *and* per view, cross-reviewer POST rejection, timezone interpretation) and
  `tests/test_prefs_form_fields.py` covers fields, validation and the label catalog. Canonical full run
  stays `bash scripts/repo_check_compose.sh`.
