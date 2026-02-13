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
  - Stage A (implemented): secure link issuance, token validation, strict error/reporting behavior.
  - Stage B (implemented): real `ReviewerPreference` edit form(s), DB writes, validation/parsing rules, and authorization hardening.
  - Stage C (future): observability/security polish and broader test coverage.
- Keep the integration inside `qb_site/zulip_bot/` and route through Django `web` process only.
- Use expiring encrypted tokens in URL path for stage A/B continuity.
- Keep frontend behavior as progressive enhancement (plain static JS, no bundling), with optional isolated JS unit tests (`vitest` + `jsdom`) under `qb_site/zulip_bot/frontend/`.

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
- Web endpoints and real UI:
  - Route: `/api/zulip/prefs/<token>/` in `qb_site/zulip_bot/urls.py`.
  - View in `qb_site/zulip_bot/views.py`:
    - validates token
    - loads preferences by token `preference_ids`
    - enforces anti-tamper and ownership checks before render/save
    - resolves form timezone from Zulip API first, then `core.User.timezone`, then Django default timezone
    - renders real formset template (`qb_site/templates/zulip_bot/prefs_form.html`)
    - saves updates via Django model formset
    - allows repeated submissions until token expiry
    - returns invalid/expired page (`qb_site/templates/zulip_bot/prefs_invalid.html`) with HTTP 403.
  - `Cache-Control: no-store` set on prefs form/invalid pages.
- Form architecture and validation:
  - Form module: `qb_site/zulip_bot/forms.py`.
  - Uses `ModelForm` + `modelformset_factory` for editable fields:
    - `maximum_capacity`
    - `auto_assign`
    - `away_until`
    - `preferred_labels`
    - `free_form`
    - `conflict_of_interest`
  - List-like fields (`preferred_labels`, `conflict_of_interest`) accept comma/newline delimiters, trim whitespace, and dedupe case-insensitively while preserving first entry casing.
  - `away_until` input is `datetime-local`; value is interpreted in resolved user timezone.
  - `maximum_capacity` is validated as `>= 1` in form validation.
  - Maintainability guard: test fails if `ReviewerPreference` model fields are added/changed without explicit inclusion/exclusion in form config.
- UX:
  - Responsive desktop/mobile layout with per-repository cards.
  - Countdown timer to expiration; submit button auto-disables at expiration.
  - Explicit submission feedback and inline validation errors.
  - “Clear away time” action per preference row.
  - Unsaved-change warning (`beforeunload`) while link is still valid.
- Frontend testing and CI:
  - JS extracted to `qb_site/zulip_bot/static/zulip_bot/prefs_form.js`.
  - Pure-function and DOM behavior tests live in `qb_site/zulip_bot/frontend/tests/` using `vitest` + `jsdom`.
  - CI checks job runs frontend tests (`npm ci && npm test`) in `qb_site/zulip_bot/frontend`.
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
  - Prefs form tests now cover successful updates, repeated submissions pre-expiry, validation failures, invalid/expired token handling, cross-user token misuse rejection, and timezone selection from Zulip API.
  - Frontend JS unit tests cover countdown expiry logic and submit-disable behavior.

## Future Work (Detailed Outline)
- Stage C1: Logging and observability.
  - Add structured logs for:
    - link issued
    - token invalid/expired
    - successful save (including preference count and user id)
    - rejected save (authz/validation category).
  - Avoid logging raw tokens or sensitive form content.
- Stage C2: Security hardening options (post-MVP).
  - Optional revocation model (stateful one-time token/jti blacklist) if needed.
  - Optional shorter TTL by environment.
  - Optional user confirmation DM on successful preference changes.
- Stage C3: Further testing.
  - Expand JS DOM tests for unsaved-change warning and clear-away behavior.
  - Add coverage for timezone fallback paths (Zulip missing/invalid timezone, fallback to user/default timezone).
  - Keep webhook and policy regression tests alongside prefs changes.

## Consequences
- We now have a production-usable secure-link flow with actual writes and validation in place.
- Current architecture avoids new dyno/process types; all behavior is served by existing `web` process.
- Frontend test coverage improved without introducing a full frontend build pipeline.
- Strict Zulip response parsing lowers ambiguity but may require updates if Zulip changes documented schema.

## Operational Notes
- Required env for membership checks in policy-gated commands:
  - `ZULIP_USER_EMAIL`
  - `ZULIP_USER_API_KEY`
- Required env for Zulip timezone lookup fallback chain:
  - `ZULIP_BASE_URL`
  - `ZULIP_BOT_EMAIL`
  - `ZULIP_BOT_API_KEY`
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
