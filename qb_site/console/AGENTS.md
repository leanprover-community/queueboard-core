# Reviewer Console Guidelines

## Scope
- `qb_site/console/` is the GitHub-OAuth authenticated **reviewer console** (design decision 050):
  a `confirm`-mode reviewer signs in and accepts/declines the assignment proposals made to them.
- It is a plain server-rendered Django app (no DRF, no models of its own). State lives in
  `analyzer.AssignmentProposal` / `analyzer.ReviewerOptOut`; identity is `core.User`.
- Mounted at `/console/` (`qb_site/qb_site/urls.py`). Templates in `qb_site/templates/console/`.

## Home dashboard (`views.home` / `_build_home_context`)
- Beyond pending proposals, the home page is a small per-repo dashboard. A repo earns a section only
  when the reviewer has **a proposal or ≥1 assigned open PR** there; each section shows:
  - a **load line** from `analyzer.services.reviewer_load` (`reviewer_load_for` + `format_load_line`):
    the weighted, engine-matching load incl. pending proposals, so the number agrees with the
    `assigned-prs` command and the daily digest. Absent a queue snapshot, no load line is rendered.
  - **assigned open PRs with status**, sourced from `analyzer.services.reviewer_attention`
    (`build_reviewer_attention_reports`) — the same authority the `assigned-prs` command uses.
  - the **proposals** to accept/decline.
- Do not re-derive load or assigned-PR facts here; reuse `reviewer_load` / `reviewer_attention`.

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

## Accept / decline (the load-bearing handlers)
- Both `POST`, both re-validate live state via the single `analyzer.services.assignment_proposal_validity.proposal_validity`
  authority before acting; a no-longer-actionable proposal renders `unavailable.html` (and the
  stale row is retired to its terminal state) instead of erroring.
- **Accept**: gated by `ANALYZER_ASSIGNMENT_PROPOSALS_ASSIGN_ON_ACCEPT_ENABLED`. When on, reuses
  the verbatim 046 mutation path (`analyzer.services.reviewer_assignment_apply.assign_reviewer_and_record`:
  GitHub assign + `ReviewerAssignmentApplication` + `syncer.sync_pr`), then marks the proposal
  `accepted` (conditional `UPDATE ... WHERE state='proposed'`). When off, it leaves the proposal
  pending and tells the reviewer — never records an acceptance it cannot fulfil.
- **Decline**: marks the proposal `declined` and upserts an active `ReviewerOptOut` (permanent
  per-PR "no", enforced by the builder). No GitHub write.

## Base URL
- Absolute links (the DM console URL, the OAuth `redirect_uri`) come from
  `core.services.site_urls.build_site_url`, which resolves `QUEUEBOARD_BASE_URL`. Do not read the
  setting directly.

## Testing
- `docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test console`
- View tests mock `console.views.GitHubOAuthClient` and `console.views.assign_reviewer_and_record`
  and seed the session directly; no real GitHub calls. Canonical full run stays
  `bash scripts/repo_check_compose.sh`.
