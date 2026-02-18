# Zulip Self-Serve Registration and GitHub Verification

## Context
- Current `prefs` command behavior is:
  - If sender has no mapped `core.User` by `zulip_user_id`, return: "No reviewer profile is linked to your Zulip account yet."
  - If sender has mapped `core.User` but no `ReviewerPreference`, return: "You do not currently have any reviewer preferences to edit."
- This creates a dead end for first-time reviewers who are in allowed Zulip groups but are not yet present in Queueboard tables.
- We want users to self-serve onboarding from Zulip DM instead of relying on manual admin imports only.
- We must not allow unverified self-asserted GitHub logins, because that allows account impersonation.
- Existing identity model supports provider-backed linking:
  - `core.User.github_node_id` (stable GitHub identifier)
  - `core.User.github_login` (mutable username)
  - `core.User.zulip_user_id` (stable Zulip ID in realm)
- Existing secure-link pattern exists (`zulip_bot.services.prefs_links`) and can be reused for a registration handshake token model.
- Existing architecture already routes bot flows through `/api/zulip/*` and supports private responses and policy gating.

## Decision
- Add a self-serve registration flow triggered from the `prefs` command when `core.User` is missing for the sender Zulip ID.
- Use GitHub OAuth as the proof-of-identity mechanism; do not accept typed GitHub login strings as source of truth.
- Treat GitHub identity as canonical by `github_node_id`; treat login as metadata that may change over time.
- Link Zulip and GitHub identities atomically to a `core.User` row under strict conflict rules.
- Keep the onboarding flow in `qb_site/zulip_bot/` to preserve architectural boundaries.
- Roll out in phases so we can ship unblock-first behavior while minimizing migration risk.

## Implementation Status (Updated February 17, 2026)
- Completed in this increment:
  - Added registration token service: `qb_site/zulip_bot/services/registration_links.py`.
  - Added registration token settings in `qb_site/qb_site/settings/base.py`:
    - `ZULIP_REGISTRATION_TOKEN_SALT`
    - `ZULIP_REGISTRATION_TOKEN_TTL_SECONDS`
  - Updated `prefs` missing-user branch to issue a private registration link instead of a dead-end-only message.
  - Added registration start endpoint and routing:
    - `/api/zulip/register/<token>/`
    - `zulip_bot.views.register_start`
  - Added registration start/invalid templates:
    - `qb_site/templates/zulip_bot/register_start.html`
    - `qb_site/templates/zulip_bot/register_invalid.html`
  - Added tests for:
    - command behavior (`test_prefs_command`)
    - token issue/validation and expiry (`test_registration_links`)
    - register start page validity/expiry behavior (`test_registration_start`)
- Completed in this increment (second chunk):
  - Added GitHub OAuth start and callback endpoints:
    - `/api/zulip/register/<token>/github/`
    - `/api/zulip/register/github/callback/`
  - Added OAuth state token service:
    - `qb_site/zulip_bot/services/registration_oauth_state.py`
  - Added GitHub OAuth client:
    - `qb_site/zulip_bot/services/github_oauth.py`
  - Updated registration start page to show active "Continue with GitHub" link when OAuth is configured.
  - Added callback success placeholder page:
    - `qb_site/templates/zulip_bot/register_callback.html`
  - Expanded invalid-reason messaging in:
    - `qb_site/templates/zulip_bot/register_invalid.html`
  - Added tests for OAuth start/callback and state handling:
    - `test_registration_start`
    - `test_registration_oauth_state`
    - `test_github_oauth`
- Completed in this increment (third chunk):
  - Added `register_test` Zulip command that returns a private registration link for live OAuth verification.
  - Added command tests:
    - `test_register_test_command`
- Completed in this increment (fourth chunk):
  - Added DB linking service:
    - `qb_site/zulip_bot/services/registration_linking.py`
  - GitHub OAuth callback now performs link/create in `core.User` with conflict handling.
  - Callback success page now reports link outcome and Queueboard user id.
  - Added link conflict UI handling in registration invalid page.
  - Added tests:
    - `test_registration_linking`
    - `test_registration_callback_linking`
- Completed in this increment (fifth chunk):
  - Added preference bootstrap service:
    - `qb_site/zulip_bot/services/registration_bootstrap.py`
  - OAuth callback now auto-creates default `ReviewerPreference` rows for active repositories when missing.
  - Callback page now shows bootstrap summary counts.
  - Added tests:
    - `test_registration_bootstrap`
    - extended `test_registration_callback_linking` to assert bootstrap creation and idempotency.
- Not yet implemented in this increment:
  - Optional repo picker for explicit preference opt-in (future refinement).

## Detailed Flow

### 1) Trigger
- User sends `prefs` via DM.
- Command policy/group checks remain unchanged.
- `prefs` handler branches:
  - Existing linked user path: unchanged.
  - Missing linked user path: issue a short-lived registration link instead of dead-end message.

### 2) Registration link and pre-auth state
- Add signed/encrypted registration token (similar to prefs token style) that includes:
  - `zulip_user_id`
  - `zulip_sender_email` (optional metadata only)
  - `zulip_sender_full_name` (optional metadata only)
  - `iat`, `exp`
  - random nonce/jti for replay protection (stateful or stateless with bounded replay controls)
- Registration link lands on Queueboard registration start page (`/api/zulip/register/<token>/`).
- Start page only offers "Continue with GitHub" and explanatory text; no free-form GitHub identity input.

### 3) GitHub OAuth handshake
- On start page, backend initiates GitHub OAuth authorization with:
  - CSRF-safe `state` tied to registration token/nonce.
  - minimal scopes required for user identity (`read:user` sufficient for id/login).
- OAuth callback validates:
  - registration token still valid,
  - OAuth `state` matches expected value,
  - GitHub response contains stable id/node id and login.

### 4) Account link/creation logic
- Inside transaction:
  - Lookup by `github_node_id` first.
  - Fallback lookup by case-insensitive `github_login` only when node id not present in an existing record.
  - Apply conflict policy:
    - If matched user has `zulip_user_id` null: set it to current sender id (link success).
    - If matched user has same `zulip_user_id`: idempotent success.
    - If matched user has different `zulip_user_id`: reject with clear support path (no reassignment in self-serve flow).
  - If no user matched: create new `core.User` with GitHub + Zulip fields.
  - Always update `github_login` casing/current value when verified from OAuth.
- Persist only trusted identity attributes from providers; never from user-typed values.

### 5) Post-link onboarding to preferences
- After successful link:
  - If preferences already exist, direct user to existing prefs form link.
  - If no preferences exist, present onboarding step:
    - Option A (first implementation): auto-create default `ReviewerPreference` rows for active repositories.
    - Option B (next iteration): repo picker with explicit opt-in.
- Final step is existing preferences editor flow (tokenized link/form).

## Security and Trust Model
- Identity proof:
  - Zulip identity is proven by incoming webhook sender metadata + command policy gates.
  - GitHub identity is proven by OAuth callback and provider-issued user id.
- Canonical key:
  - `github_node_id` is the durable primary identity key for GitHub account ownership checks.
- Anti-impersonation:
  - Disallow manual GitHub login declaration for linking.
  - Reject linking when GitHub account is already bound to another Zulip ID.
- Token security:
  - short TTL for registration tokens,
  - include anti-replay nonce/jti,
  - bind OAuth `state` to token context,
  - never log raw tokens or OAuth secrets.
- Response visibility:
  - Keep all registration and linking responses private (DM-only semantics).

## Subtleties and Notes Discovered During Implementation
- URL base:
  - Registration links use `ZULIP_PREFS_URL_BASE` (single shared Zulip web URL base).
- Token secret:
  - Registration tokens reuse `ZULIP_PREFS_TOKEN_SECRET` (or `SECRET_KEY` fallback), while keeping a distinct registration salt.
  - This keeps secrets simpler while still providing token namespace separation through salt values.
- Current registration entrypoint is intentionally a placeholder page:
  - It validates token integrity/expiry and provides OAuth entry when configured.
- Callback page is intentionally a placeholder:
  - OAuth identity proof and DB linking are completed and displayed.
  - Preference bootstrap is still pending.
- OAuth callback protection:
  - Callback `state` is encrypted/signed and short-lived.
  - `state` includes both the registration token and registration nonce.
  - Callback re-validates registration token and enforces nonce equality to prevent token/state mix-and-match.
- Link conflict policy implemented:
  - If GitHub account is already linked to a different Zulip id, callback returns a conflict page (no reassignment).
  - If a GitHub login matches an existing row bound to a different `github_node_id`, callback returns conflict.
- Bootstrap policy implemented:
  - For a successfully linked user, create default preferences for all active repositories (`Repository.is_active=True`).
  - Bootstrap is idempotent; existing preference rows are not duplicated.
- The registration token includes sender metadata (`sender_email`, `sender_full_name`) as convenience context only; identity authority remains Zulip sender id + future GitHub OAuth proof.

## Data and Model Notes
- Reuse existing `core.User` fields; no required schema change for MVP.
- Optional follow-up model for stronger replay/idempotency and auditing:
  - `ZulipRegistrationAttempt` (jti, zulip_user_id, consumed_at, oauth_state_hash, outcome).
- Preserve existing uniqueness semantics:
  - `github_node_id` unique,
  - `zulip_user_id` unique,
  - case-insensitive unique `github_login`.

## Operational Notes
- New env/config expected:
  - `GITHUB_OAUTH_CLIENT_ID`
  - `GITHUB_OAUTH_CLIENT_SECRET`
  - `GITHUB_OAUTH_REDIRECT_URI`
  - registration-token salt/ttl settings (`ZULIP_REGISTRATION_TOKEN_SALT`, `ZULIP_REGISTRATION_TOKEN_TTL_SECONDS`).
- Existing env remains relevant:
  - Zulip webhook and policy settings,
  - prefs token settings (including shared token secret).
- Admin/support runbook updates needed:
  - handling identity conflict cases,
  - manual recovery procedure when user linked wrong external account,
  - secret rotation impact on in-flight registration links.

## Implementation Plan (Phased)

### Phase 0: Documentation and guardrails
- Land this design decision.
- Update bot-facing copy for missing-user message to mention registration path.

### Phase 1: Self-serve link + OAuth linking (no repo picker)
- Add registration token service + endpoints in `zulip_bot`.
- Add GitHub OAuth client helper/service.
- Add transactional link-or-create workflow for `core.User`.
- Update `prefs` command missing-user branch to emit registration link.
- Add tests for token/state validation, linking success, and conflict rejection.

### Phase 2: Preference bootstrap
- Add onboarding behavior for users with no `ReviewerPreference`.
- Start with deterministic default creation for active repositories.
- Add tests for first-run preference creation and idempotent retries.

### Phase 3: UX and observability hardening
- Improve registration page content and failure messages.
- Add structured logs and metrics for funnel steps:
  - link issued
  - OAuth started
  - OAuth callback success/failure
  - linked existing user
  - created new user
  - conflict blocked
  - preference bootstrap result
- Add alerting thresholds for repeated conflict failures.

## Consequences
- Removes the dead-end for first-time allowed Zulip users.
- Eliminates the most likely impersonation vector (self-asserted GitHub login claims).
- Introduces OAuth and token-state complexity; requires careful callback and secret management.
- Keeps architecture coherent by extending current Zulip app boundaries rather than introducing a separate auth subsystem immediately.
- Sets foundation for future identity-provider extensions while preserving current model.

## Alternatives Considered
- Manual admin-only onboarding via `reviewer_zulip_ids.json` import.
  - Rejected as primary path: high operational overhead and poor user experience.
- Let users type GitHub login in Zulip command and trust it.
  - Rejected: unacceptable impersonation risk.
- Require GitHub PR comment challenge instead of OAuth.
  - Deferred: possible fallback, but slower UX and more moving parts for MVP.
- Build a full Django session-login area for reviewer onboarding before OAuth-in-bot flow.
  - Deferred: higher scope than needed for immediate unblock.

## Open Questions
- Preference bootstrap policy:
  - create defaults for all active repos vs. explicit repo selection first?
- Conflict recovery path:
  - should admin tooling support safe reassignment with audit trail?
- Replay protection depth:
  - stateless token-only sufficient, or should we require one-time DB-backed token consumption?

## References
- `/Users/bryanchen/Documents/lean/queueboard-core/docs/design-decisions/021-zulip-bot-architecture.md`
- `/Users/bryanchen/Documents/lean/queueboard-core/docs/design-decisions/022-zulip-prefs-form-design.md`
- `/Users/bryanchen/Documents/lean/queueboard-core/qb_site/zulip_bot/commands/prefs.py`
- `/Users/bryanchen/Documents/lean/queueboard-core/qb_site/core/models/user.py`
