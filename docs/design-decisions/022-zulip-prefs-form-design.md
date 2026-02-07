# Zulip Reviewer Preferences Form Design

## Context
- We want reviewers to self-serve edits to `core.ReviewerPreference` via a Zulip DM command.
- The desired UX is:
  - user DMs bot with `prefs`
  - bot returns a short-lived secure link
  - user opens a web form and submits updates before expiry
- We need low operational overhead for Heroku (`web` dyno handles HTTP), while preserving strong authz boundaries.
- We already ship Zulip command policy checks and private-response controls (`docs/design-decisions/021-zulip-bot-architecture.md`).

## Decision
- Implement the feature in staged increments:
  - Stage A (implemented): secure link issuance, token validation, dummy web form, strict error/reporting behavior.
  - Stage B (future): real `ReviewerPreference` edit form(s), DB writes, validation/parsing rules, and authorization hardening.
- Keep the integration inside `qb_site/zulip_bot/` and route through Django `web` process only.
- Use expiring encrypted tokens in URL path for stage A/B continuity.

## Implemented (Current Behavior)
- Command:
  - `prefs` command is registered in `qb_site/zulip_bot/commands/prefs.py`.
  - Command requires:
    - Zulip sender identity (`context.sender_id`)
    - mapped `core.User` via `zulip_user_id`
    - at least one `ReviewerPreference` row for that user.
  - Bot message uses:
    - markdown link label for readability
    - Zulip `<time:...>` element for exact expiration display.
- Link/token:
  - Implemented in `qb_site/zulip_bot/services/prefs_links.py`.
  - Token includes claims: `user_id`, `zulip_user_id`, `preference_ids`, `iat`, `exp`.
  - Claims are encrypted with Fernet; key derived from:
    - `ZULIP_PREFS_TOKEN_SECRET` (or `SECRET_KEY` fallback)
    - `ZULIP_PREFS_TOKEN_SALT`
  - TTL comes from `ZULIP_PREFS_TOKEN_TTL_SECONDS` (default 1800).
  - URL base uses `ZULIP_PREFS_URL_BASE` when configured.
- Web endpoints and placeholder UI:
  - Route: `/api/zulip/prefs/<token>/` in `qb_site/zulip_bot/urls.py`.
  - View in `qb_site/zulip_bot/views.py`:
    - validates token
    - shows dummy form template (`qb_site/templates/zulip_bot/prefs_form.html`)
    - returns invalid/expired page (`qb_site/templates/zulip_bot/prefs_invalid.html`) with HTTP 403.
  - `Cache-Control: no-store` set on prefs form/invalid pages.
- Zulip policy/membership integration:
  - Membership checks use documented Zulip response field `is_user_group_member` only.
  - Membership endpoint auth is strict user-auth only (`user_required`); no bot fallback.
  - Config:
    - `ZULIP_USER_EMAIL`
    - `ZULIP_USER_API_KEY`
  - Query booleans for Zulip API are encoded as JSON-style tokens (`true`/`false`) in query params.
- Error and ignore behavior:
  - Webhook wraps unexpected errors and returns private structured error blocks (spoilered JSON).
  - Intentional ignore paths return Zulip no-op JSON: `{"response_not_required": true}` to avoid client-side “Invalid JSON” failures.
- Tests:
  - Command, token/form, policy, webhook error behavior, membership parsing, and Zulip client auth/param encoding are covered under `qb_site/zulip_bot/tests/`.

## Future Work (Detailed Outline)
- Stage B1: Replace dummy form with real editable form model.
  - Add `ModelForm`/formset for `ReviewerPreference` fields:
    - `maximum_capacity`
    - `auto_assign`
    - `away_until`
    - `preferred_labels`
    - `free_form`
    - `conflict_of_interest`
  - Show `repository` and identity context read-only.
  - If multiple preference rows exist, support per-repo sections in one page.
- Stage B2: Write-path authorization and anti-tamper checks.
  - On GET and POST, load DB rows by `preference_ids` from token claims.
  - Enforce ownership (`preference.user_id == claims.user_id`) before rendering or saving.
  - Reject missing/extra IDs and any user mismatch with explicit 403.
- Stage B3: Input normalization and validation policy.
  - Define parsing rules for list-like fields:
    - line- or comma-separated parsing for `preferred_labels` and `conflict_of_interest`
    - dedupe and trim rules (case policy documented explicitly)
  - Define `away_until` timezone handling:
    - expected input format(s)
    - conversion and display timezone behavior
    - nullable semantics.
- Stage B4: UX and template improvements.
  - Replace placeholder content with clear per-field help text.
  - Add submission success state with changed fields summary.
  - Add explicit expired-link UX with “DM `prefs` again” guidance.
- Stage B5: Logging and observability.
  - Add structured logs for:
    - link issued
    - token invalid/expired
    - successful save
    - rejected save (authz/validation).
  - Avoid logging raw tokens or sensitive form content.
- Stage B6: Security hardening options (post-MVP).
  - Optional revocation model (stateful one-time token/jti blacklist) if needed.
  - Optional shorter TTL by environment.
  - Optional user confirmation DM on successful preference changes.
- Stage B7: Test expansion.
  - Integration tests for end-to-end `prefs` flow with actual model updates.
  - Authorization tests for cross-user token misuse.
  - Validation tests for each editable field and parser rule.
  - Regression tests for error and no-op webhook responses.

## Consequences
- We get an immediately usable secure-link flow while keeping data writes gated until validation rules are explicit.
- Current architecture avoids new dyno/process types; all behavior is served by existing `web` process.
- Strict Zulip response parsing lowers ambiguity but may require updates if Zulip changes documented schema.

## Operational Notes
- Required env for membership checks in policy-gated commands:
  - `ZULIP_USER_EMAIL`
  - `ZULIP_USER_API_KEY`
- Prefs-link env:
  - `ZULIP_PREFS_URL_BASE`
  - `ZULIP_PREFS_TOKEN_SECRET`
  - `ZULIP_PREFS_TOKEN_SALT`
  - `ZULIP_PREFS_TOKEN_TTL_SECONDS`
- Rotating token secret/salt invalidates all active prefs links.

## Alternatives (Optional)
- Stateful DB-backed one-time links from day 1.
  - Deferred to keep initial slice simple and low-maintenance.
- Building prefs editor under `/admin/` only.
  - Rejected because the requirement is DM-driven self-service for reviewers.
