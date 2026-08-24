# Zulip Reviewer Preferences Form Design

> Status: **Stages A/B implemented; auth-model amendment phases 1–2 implemented** (behind
> `CONSOLE_PREFS_ENABLED`, default off). With the flag on, `/console/preferences/` is the advertised
> entry point and the expiring-link flow under "Implemented (Current Behavior)" is dormant but intact;
> with it off, nothing changes. Only phase 3 (retiring the token path) remains — see "Amendment:
> GitHub-OAuth Session Auth" below. The amendment closes the deferred follow-up recorded in
> `050-reviewer-assignment-acceptance-gate.md` ("Stable `/prefs` URL sharing the console session").

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
  - Absolute URL comes from `core.services.site_urls.build_site_url` (`QUEUEBOARD_BASE_URL`).
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

## Amendment: GitHub-OAuth Session Auth

> Phases 1–2 implemented; phase 3 pending. Supersedes the token-link *auth model* above (everything
> about the form, fields, validation, and UX carries over unchanged).

### Why

- The prefs link is a bearer secret in a URL with a 30-minute TTL: not bookmarkable, re-requested
  from Zulip for every edit, and gone from browser history's usefulness the moment it expires.
- It is unreachable for reviewers with no Zulip link. `prefs` requires a `core.User` matched by
  `zulip_user_id`, so reviewers created by `import_reviewer_topics` (github handle only) cannot get
  a prefs link at all today.
- Doc 050 shipped a reviewer console with exactly the auth this form wants: GitHub OAuth → Django
  session holding a resolved `core.User` id, at a stable token-less URL.
- One reviewer-facing surface, one auth model. Two auth paths over the same writable rows is the
  drift risk this repo avoids elsewhere (one validity authority, one chunking helper, one
  `_prepare_assignment_inputs`).

### Decision

- Serve the prefs form from the reviewer console at **`/console/preferences/`**, authenticated by
  the console session (`console.session`); retire the token route after a soak window.
- **Admission is unchanged; only authentication changes.**
  - *Admission* — becoming a reviewer at all — stays where it is: the deny-by-default
    `ZULIP_COMMAND_POLICY` gate (`zulip_bot/webhook/policy.py`: a command absent from the policy is
    ignored entirely; entries gate on explicit `allowed_user_ids`, live Zulip user-group membership,
    and context) on the commands that hand out a registration link — `prefs` for an unknown sender,
    and `register_test` — plus the two maintainer-side paths, `import_reviewer_topics` and the
    Django admin.
  - *Authentication* — proving you are that reviewer — becomes GitHub OAuth instead of a DM'd token.
  - The console therefore adds **no new way to become a reviewer**; invariants 1–2 below are what
    make that true in code rather than by convention.

### Invariants

1. **The console never creates `ReviewerPreference` rows.**
   `core.services.github_identity.resolve_user_from_identity` is resolve-only, but it resolves *any*
   `core.User` — and `syncer.services.sub.pull_request_sync` upserts one for every PR author
   (`upsert_user_from_github(..., create_missing=True)`), so "known GitHub account" means thousands
   of contributors, not reviewers. Because a `ReviewerPreference` row *is* candidate-pool membership
   (`analyzer.services.reviewer_assignment.build_reviewer_catalog` hydrates the pool from these rows)
   and `auto_assign` defaults to `True`, a "create my preferences" affordance on a public sign-in
   surface would let any contributor enter the assignment pool. The prefs page edits existing rows
   only; bootstrapping stays in the registration flow (`ensure_default_preferences_for_user`).
2. **Console admission is "is a reviewer here", not "is a known GitHub account".** A session is
   granted only when the resolved user has ≥1 `ReviewerPreference` row **or** an active
   `AssignmentProposal` for their login. The proposal clause preserves the console's promise when a
   maintainer removes a prefs row while a proposal is pending: accept/decline still work, and the
   hourly expiry sweep retires the proposal otherwise.
   - This **tightens the shipped console**: today any syncer-ingested contributor can complete
     sign-in and see an empty dashboard. Enforced in one helper that every console view calls, and at
     `oauth_callback` so refusal renders the existing "only for registered reviewers" 403 instead of
     an empty page.
   - "≥1 preference row" is the durable definition of reviewer-ness, deliberately not "has
     registered": registration bootstraps rows for all active repos, but rows also arrive via
     `import_reviewer_topics` and the admin, and it is the rows the engine actually reads.
3. **Ownership scoping replaces token scoping.** The formset queryset is filtered by
   `user=<session reviewer>` on GET *and* POST, so a posted `form-N-id` cannot reach another
   reviewer's row (Django validates ids against the supplied queryset). The token-era anti-tamper
   checks (`_load_authorized_preferences`: ids belong to the claimed user, `zulip_user_id` matches)
   become unnecessary rather than reimplemented.
4. **Scope becomes live rather than a snapshot.** The page always edits the reviewer's current rows,
   so a repository activated after registration shows up without a fresh link.

### Shape of the change

- **One authority for the form.** `ReviewerPreferenceForm` and
  `reviewer_preference_unaccounted_fields` live in `core/forms.py` (it is a `core` model form);
  formset assembly — ownership-scoped queryset, label catalog, topic-label pattern — lives in
  `core/services/reviewer_prefs.py` (`build_preferences_formset`, `preferences_for_user`). It reads
  `syncer.models.LabelDef` at module scope, matching the existing `core/admin.py` precedent. Both
  entry points call it while both exist, so the two pages cannot drift.
- **Timezone resolution lives in `zulip_bot/services/user_timezone.py`, not `core`.** Its
  authoritative source is Zulip's own user record, and `core/services` otherwise carries no app
  dependencies; the console imports it (console access stays independent of Zulip *reachability* —
  an unlinked or unreachable Zulip just falls through to the fallbacks).
- **Route.** `console/urls.py`: `path("preferences/", views.prefs, name="prefs")`. Method-restricted,
  `Cache-Control: no-store`, POST → redirect `?saved=1` (no token left in browser history).
  `_safe_next` / `_login_url` already handle `?next=/console/preferences/` unchanged.
- **Templates.** `templates/console/prefs.html` extends `console/base.html` (which gained
  `extra_css` / `extra_js` blocks); the fields live in `templates/shared/_reviewer_prefs_fields.html`,
  included by both pages. Header is the console's "Signed in as X · Sign out" row; no countdown
  section. A reviewer with no rows gets an explanatory empty state — never a create affordance.
- **Static: the assets stay in `zulip_bot/static/zulip_bot/`.** Moving them to `static/shared/` was
  the plan and does not work: `close_pr_form.js` and `label_pr_form.js` import the expiry helpers
  from `./prefs_form.js` as a *sibling*, and a relative ES import has to resolve both against the
  served static path (browser, where `zulip_bot/` and `shared/` are siblings) and the on-disk path
  (vitest, where they are not). Only colocation satisfies both, so the console page loads
  `zulip_bot/prefs_form.css|js` directly. Relocating all three scripts together is phase-3 cleanup,
  once the token pages' fate is settled.
- **`mountPrefsForm` must tolerate a missing expiry block.** It currently returns a no-op unless
  `#countdown-text`, `#countdown-label` *and* `#expires-at` all exist, which would silently drop the
  unsaved-changes guard and the "clear away time" buttons on a page with no countdown. Make the
  countdown optional and keep the guard unconditional. CI runs these tests
  (`.github/workflows/ci.yml` → `qb_site/zulip_bot/frontend`, vitest + jsdom).
- **Cross-links.** "Reviewer preferences" from console home; back to the console from prefs.
- **Session lifetime replaces token TTL.** Set `SESSION_SAVE_EVERY_REQUEST = True` so activity slides
  the two-week `SESSION_COOKIE_AGE`; otherwise a long edit can lose its POST to a cookie that lapsed
  mid-session. The `beforeunload` dirty guard remains the backstop.

### Timezone

- Today the token carries `zulip_user_id` and the view asks Zulip for the reviewer's timezone
  (`_fetch_zulip_user_timezone_name` → `ZulipClient().get_user_by_id`), falling back to
  `core.User.timezone`, then the Django default. That timezone is what the naive `datetime-local`
  `away_until` is interpreted in (`ReviewerPreferenceForm.clean_away_until`).
- Under session auth the same chain runs with `user.zulip_user_id`. Every registered reviewer has a
  Zulip link (registration is Zulip-command-driven and stays that way), so behavior is unchanged; the
  fallbacks matter only for importer-created rows, which land on the project default timezone.
- Follow-up (deliberately not now): `core.User.timezone` is **admin-only — no code path writes it**.
  Persisting a browser-reported `Intl.DateTimeFormat().resolvedOptions().timeZone` would make the
  fallback real and let us drop the per-render Zulip API round-trip.

### Phases

1. **Additive — done.** Shared form/context extraction, `/console/preferences/`, the
   reviewer-admission gate (invariant 2, applied to the whole console), the optional-countdown JS fix,
   and tests. Behind `CONSOLE_PREFS_ENABLED` (default off), wired in `settings/base.py` **and**
   `.env.example`; `SESSION_SAVE_EVERY_REQUEST = True` added. Token route untouched and still the
   advertised entry point. Deltas from the plan: the static move was dropped (see above) and timezone
   resolution landed in `zulip_bot/services/user_timezone.py` rather than `core`.
2. **Make it the entry point — done.** One flag-aware seam,
   `zulip_bot.services.prefs_links.build_prefs_entry_link`, answers "where do reviewers edit
   preferences" (`PrefsEntryLink(url, expires_at_unix)`; `expires_at_unix is None` ⇒ the stable console
   URL), so the command and the registration DM/page cannot disagree. With the flag on:
   - `prefs` replies **in place** with the stable URL (mirroring `commands/console.py` — nothing secret
     to DM). Its registration-link branch stays a DM, because *that* link is a bearer secret, and its
     "no preferences to edit" branch stays: a reviewer with no rows would be refused by the console's
     admission gate, so Zulip is a better place to say so than a 403.
   - the registration success DM and `register_callback.html` advertise the stable URL, and
     `register_github_callback` promotes the console session (`console.session.set_reviewer`) so the
     "Edit Preferences Now" link lands signed in — no second OAuth round-trip. Success path only, and
     only when the new reviewer actually has rows. The promotion is strictly stronger than a console
     sign-in: registration proves Zulip identity (the registration token) *and* GitHub identity (OAuth).
   - the `console` command mentions preferences alongside proposals.
   The token-branch page copy is left verbatim (an existing assertion pins it), so flipping the flag
   off restores the old flow exactly.
3. **Retire the token path** (separate PR, after a soak). Pre-flight: expect zero
   `ReviewerPreference` rows whose user has a null/blank `github_login` — under GitHub OAuth that,
   not `zulip_user_id`, is what would lock someone out. Then delete `prefs/<token>/`,
   `zulip_bot/services/prefs_links.py`, the prefs branch of `prefs_invalid.html`, the
   `ZULIP_PREFS_TOKEN_*` settings from `base.py` and `.env.example`, and the token tests.
   `close_pr` / `label_pr` keep their own independent token modules.

### Test coverage (added in phase 1: `qb_site/console/tests/test_prefs.py`)

- Anonymous GET → console login with `next=/console/preferences/`.
- Signed-in GET renders one card per owned row; POST saves and redirects `?saved=1`.
- A known-but-not-reviewer account (a syncer-ingested PR author) is refused at sign-in **and** by
  every console view, and **no `ReviewerPreference` row is created** (invariant 1).
- A reviewer with a pending proposal but no preference rows keeps access (invariant 2's OR clause).
- Posting another reviewer's `form-N-id` is rejected (invariant 3).
- Timezone resolution with and without `zulip_user_id`.
- vitest: `mountPrefsForm` keeps the dirty guard and clear-away buttons when no expiry block exists.

### Consequences of the amendment

- Prefs editing gains a stable, bookmarkable URL and becomes reachable for reviewers with no Zulip
  link; the whole reviewer-facing surface shares one auth model.
- Prefs editing now depends on GitHub OAuth being configured (sign-in renders 503 otherwise) — the
  Zulip DM path alone no longer suffices. `QUEUEBOARD_BASE_URL` and the root-registered OAuth
  callback (`docs/zulip_github_oauth_setup.md`) become prerequisites for prefs, not just the console.
- Tightening console admission is user-visible for contributors who could previously sign in to an
  empty console; they now get the "only for registered reviewers" page.
- Editing scope widens from a token snapshot to all current rows — intended, and the reason
  ownership filtering must be applied on POST as well as GET.

## Future Work (Detailed Outline)
- Stage C1: Logging and observability.
  - Add structured logs for:
    - link issued
    - token invalid/expired
    - successful save (including preference count and user id)
    - rejected save (authz/validation category).
  - Avoid logging raw tokens or sensitive form content.
- Stage C2: Security hardening options (post-MVP).
  - Optional revocation model (stateful one-time token/jti blacklist) if needed. *(Moot once the
    token path is retired — see the amendment; session logout is the revocation.)*
  - Optional shorter TTL by environment. *(Moot for the same reason.)*
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
  - `QUEUEBOARD_BASE_URL` (absolute link base, shared by every reviewer-facing surface)
  - `ZULIP_PREFS_TOKEN_SECRET`
  - `ZULIP_PREFS_TOKEN_SALT`
  - `ZULIP_PREFS_TOKEN_TTL_SECONDS`
- Rotating token secret/salt invalidates all active prefs links.

## Alternatives (Optional)
- Stateful DB-backed one-time links from day 1.
  - Deferred to keep initial slice simple and low-maintenance.
- Building prefs editor under `/admin/` only.
  - Rejected because the requirement is DM-driven self-service for reviewers.
