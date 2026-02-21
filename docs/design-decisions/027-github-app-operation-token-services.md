# GitHub App Operation-Token Services

## Context
- Queueboard needs GitHub App installation tokens for assignment mutations and a reusable path for broader GitHub operations.
- The system must support multiple GitHub Apps with operation-based selection to avoid over-permissioning.
- Assignment and syncer subsystems have different token policies:
  - assignment mutations require app-token resolution
  - syncer prefers app tokens but keeps existing env-token fallback for compatibility
- Secret/config handling must scale without code changes when adding apps or remapping operations.

## Decision
- Implement a shared GitHub App installation-token platform in `core.services`.
- Use operation-scoped token resolution (`operation`, `owner`, `repo`) to select app credentials and mint installation tokens.
- Support structured multi-app config from `GITHUB_APP_TOKEN_CONFIG` with deterministic operation-to-app mapping.
- Keep provider-level behavior app-token-only; fallback to legacy env tokens is handled by callers (for example syncer client).
- Cache installation ids and minted tokens in process memory with expiration-aware refresh.

## Architecture

### 1) GitHub App Token Provider
- Service: `core.services.github_app_tokens.GitHubAppInstallationTokenProvider`.
- Responsibilities:
  - parse app definitions from config
  - select app by operation (`operation_app_map` first, then app-advertised operations)
  - generate GitHub App JWT (`RS256`)
  - resolve installation id (`GET /repos/{owner}/{repo}/installation`)
  - mint installation token (`POST /app/installations/{id}/access_tokens`)
  - cache installation ids and tokens
- Error model:
  - typed `GitHubAppTokenError` with code/message (`invalid_private_key`, `installation_not_found`, `token_mint_failed`, etc.)
- Logging:
  - structured cache-hit/mint events
  - redaction-safe (no private key or token value logging)

### 2) Shared Resolver
- Service: `core.services.github_operation_tokens.resolve_github_app_operation_token(...)`.
- Inputs:
  - `operation`
  - `owner`
  - `repo`
- Behavior:
  - attempts provider token resolution
  - returns token string when available
  - returns `None` when no app token resolves
  - logs warning on provider errors and returns `None`
- The resolver intentionally does not apply PAT/env fallback.

### 3) Caller Policies
- Assignment command path (`zulip_bot.services.assignment_execution`):
  - uses operation `assign_pr` / `unassign_pr`
  - requires app token; if absent, command fails privately
  - no PAT/env fallback
- Syncer GitHub client (`syncer.services.github_client.GitHubClient`):
  - when `operation` + `owner` + `repo` are provided, attempts app token first
  - if app token is unavailable, falls back to `GH_TOKEN`/`GITHUB_TOKEN` chooser logic
  - preserves existing rate-budget behavior for fallback tokens

## Configuration Design
- Setting: `GITHUB_APP_TOKEN_CONFIG` (JSON object parsed in settings).
- Top-level keys:
  - `api_base_url` (optional; defaults to `GITHUB_API_URL`)
  - `cache_skew_seconds` (optional; default `60`)
  - `operation_app_map` (optional; operation -> app name)
  - `apps` (list of app definitions)
- App definition keys:
  - `name` (required)
  - `app_id` (required)
  - `operations` (optional list used for fallback selection)
  - installation lookup controls (optional):
    - `installation_lookup`: `repo` (default) or `owner`
    - `installation_owner_type`: `org` (default) or `user`
    - `installation_owner`: fixed org/user identifier for owner lookup
  - credential source (one required):
    - `private_key` (PEM string, `\\n` escaped in env JSON), or
    - `private_key_path` (filesystem path)
- Selection order:
  - if `operation_app_map[operation]` exists, use mapped app (error if missing)
  - otherwise, first app that advertises the operation in `operations`
  - otherwise, no app token resolved

## Operation Namespace
- Current operation names:
  - `assign_pr`
  - `unassign_pr`
  - `syncer_repo_discovery`
  - `syncer_pr_read`
  - `syncer_ci_read`
- Naming intent:
  - operation names represent capability context, not endpoint names
  - names are stable integration contracts between callers and token routing config

## Security and Secret Handling
- Private keys are never logged.
- Minted installation tokens are never logged.
- JWT is short-lived and generated per token acquisition flow.
- Recommended deployment secret handling:
  - Heroku: prefer inline `private_key` in `GITHUB_APP_TOKEN_CONFIG`
  - file-based deployments: use `private_key_path` with mounted secrets
- GitHub App creation, permissions, and install runbook:
  - `docs/github_app_setup.md`

## Caching and Runtime Notes
- Caches are process-local in memory:
  - installation id cache keyed by `(app, owner, repo)`
  - token cache keyed by `(app, installation_id)`
- Token refresh uses expiration skew (`cache_skew_seconds`) to avoid near-expiry reuse.
- Cross-process shared cache is intentionally deferred; behavior remains correct with per-process minting.

## Operational Notes
- Assignment operations fail when app token resolution is unavailable; this is expected policy.
- Syncer remains migration-safe because env-token fallback is still available.
- If `GITHUB_API_URL` includes a non-REST suffix in some environments, set `api_base_url` explicitly in app token config.

## Testing Strategy
- Provider tests (`core.tests.test_github_app_tokens`):
  - token mint + cache reuse
  - near-expiry refresh
  - explicit operation mapping selection
- Resolver tests (`core.tests.test_github_operation_tokens`):
  - app-token resolution success
  - error handling returns `None`
- Assignment execution tests:
  - enforce app-token-required behavior
- Syncer client tests:
  - app-token-first initialization
  - fallback behavior compatibility

## Consequences
- Pros:
  - reusable, centralized GitHub App token platform
  - least-privilege routing with multi-app operation mapping
  - clear subsystem-specific policy (assignment strict, syncer gradual migration)
- Trade-offs:
  - additional credential/config complexity
  - process-local caching may mint duplicate tokens across worker processes
  - operation naming/config drift requires disciplined testing and docs

## Alternatives Considered
- PAT-only model including assignment mutations.
  - Rejected: weaker credential hygiene and poor long-term scaling.
- Single GitHub App for all operations.
  - Deferred: workable but increases over-permission risk.
- Provider-level fallback to PAT/env tokens.
  - Rejected: couples core token service to caller-specific policy and obscures security boundaries.

## Open Questions
- Whether to add cross-process token/installation cache (e.g., Redis-backed) for lower mint churn.
- Whether to adopt per-repository app override routing beyond operation-level selection.

## References
- `docs/github_app_setup.md`
- `qb_site/core/services/github_app_tokens.py`
- `qb_site/core/services/github_operation_tokens.py`
- `qb_site/zulip_bot/services/assignment_execution.py`
- `qb_site/syncer/services/github_client.py`
