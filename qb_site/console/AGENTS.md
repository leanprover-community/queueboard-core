# Reviewer Console Guidelines

## Scope
- `qb_site/console/` is the GitHub-OAuth authenticated **reviewer console** (design decision 050):
  a `confirm`-mode reviewer signs in and accepts/declines the assignment proposals made to them.
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

## Base URL
- Absolute links (the DM console URL, the OAuth `redirect_uri`) come from
  `core.services.site_urls.build_site_url`, which resolves `QUEUEBOARD_BASE_URL`. Do not read the
  setting directly.

## Testing
- `docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test console`
- View tests mock `console.views.GitHubOAuthClient`, `console.views.assign_reviewer_and_record`,
  `console.views.GitHubAssignmentClient` (unassign), and `console.views._enqueue_pr_sync`, and seed
  the session directly; no real GitHub calls. Canonical full run stays
  `bash scripts/repo_check_compose.sh`.
