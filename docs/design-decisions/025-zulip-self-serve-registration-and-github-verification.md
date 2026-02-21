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

## Detailed Flow

### 1) Trigger
- User sends `prefs` via DM.
- Command policy/group checks remain unchanged.
- `prefs` handler branches:
  - Existing linked user path: unchanged.
  - Missing linked user path: issue a short-lived registration link (with explicit expiration time) instead of dead-end message.
- Optional operator/debug path:
  - `register_test` command issues a fresh registration link in DM regardless of whether a user is already linked.

### 2) Registration link and pre-auth state
- Add signed/encrypted registration token (similar to prefs token style) that includes:
  - `zulip_user_id`
  - `zulip_sender_email` (optional metadata only)
  - `zulip_sender_full_name` (optional metadata only)
  - `iat`, `exp`
  - random nonce for replay/mix-and-match protection
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
- If validation fails, show registration invalid page with reason-specific messaging (`expired`, `oauth_invalid`, `oauth_failed`, etc.).

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
  - Bootstrap default `ReviewerPreference` rows for all active repositories (`Repository.is_active=True`) if missing.
  - Bootstrap is idempotent and does not duplicate existing preference rows.
  - Build the standard expiring prefs-form link (`/api/zulip/prefs/<token>/`) and show it on callback success page.
  - Send a Zulip DM confirmation including:
    - linked GitHub login
    - same prefs-form link
    - expiration timestamp in Zulip `<time:...>` format
  - If DM delivery fails, callback page still succeeds and includes a fallback message.

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
- Post-link UX policy implemented:
  - Always provide a preferences link immediately after successful linking (when preferences exist).
  - Also push the same link via Zulip DM for continuity back in chat.
- Zulip API payload nuance:
  - Direct-message recipients and user-group member update lists must be JSON-encoded in form payloads.
  - `send_direct_message` uses `type="direct"` with JSON-encoded `to`.
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
- Implement deterministic default creation for active repositories.
- Keep path idempotent for repeated callback runs.
- (Optional refinement) introduce explicit repo picker instead of default-all.

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
- `docs/design-decisions/021-zulip-bot-architecture.md`
- `docs/design-decisions/022-zulip-prefs-form-design.md`
- `qb_site/zulip_bot/commands/prefs.py`
- `qb_site/core/models/user.py`
