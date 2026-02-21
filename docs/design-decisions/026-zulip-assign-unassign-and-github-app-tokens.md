# Zulip Assign/Unassign Commands and GitHub App Installation Tokens

## Context
- We want new Zulip bot commands to manage PR assignees:
  - `assign <pr> <optional zulip mention(s)>`
  - `unassign <pr> <optional zulip mention(s)>`
- Command input is tricky because in Zulip usage, PRs are often written as linkifiers (`#123`, `repo#123`) in `message.content`.
- To reliably identify the GitHub PR URL/repo/number, parsing must inspect `message.rendered_content` (HTML), not only raw text content.
- If no mention is provided, the target reviewer should default to the command sender.
- We should support multiple mentioned reviewers in one command.
- Validation requirements:
  - target Zulip user must map to a Queueboard reviewer identity (GitHub login + `ReviewerPreference`)
  - reviewer preferences must include the PR repository
  - assign: PR must not be closed/merged
  - unassign: target reviewer must currently be assigned
- UX requirements:
  - success: react to the command message with an emoji (no success text reply)
  - failure: post a detailed, private error message
  - idempotent outcomes (`already assigned`, `not assigned`) should be reported as mild warnings
- Data availability requirement:
  - if PR is not available locally, try fetching from GitHub and proceed
- Token/auth requirement:
  - current PAT setup is insufficient for assignment operations
  - we need GitHub App installation-token minting for assignments now
  - we also need a reusable multi-app token system for future syncer migration away from PATs and other operations

## Decision
- Implement `assign` and `unassign` as first-class Zulip bot commands with private failure responses and success emoji reactions.
- Support multiple target reviewers per command, with partial-success execution and per-reviewer outcome reporting.
- Build command parsing around a structured argument parser that uses `rendered_content` for PR extraction and mention resolution, with safe fallbacks.
- Keep command authorization broad initially (allowed for everyone who can invoke command via existing Zulip policy), but isolate restriction logic behind explicit hooks so later per-user/per-group/per-repo restrictions can be added without redesign.
- Add a GitHub App installation-token provider layer before assignment execution:
  - supports dedicated app(s) for assignment mutations
  - supports an arbitrary number of app definitions for syncer and future operations
  - supports operation-to-app selection rules
  - mints, caches, and refreshes installation tokens centrally
- If local PR row is missing, fetch PR metadata from GitHub and either:
  - run command directly on live GitHub data and schedule a sync to converge local DB
  - optionally upsert a minimal local PR/repository stub when safe

## Command Behavior Contract
- Syntax:
  - `assign <pr> <optional zulip mention(s)>`
  - `unassign <pr> <optional zulip mention(s)>`
- `<pr>` accepted forms:
  - full GitHub PR URL in content or rendered HTML
  - Zulip linkifier text resolved via anchor in `rendered_content`
- `<optional zulip mention(s)>` behavior:
  - omitted => target reviewer set is `{sender}`
  - present => target reviewer set is all mentioned users
  - separators in args can be spaces and/or commas; rendered mention entities are source of truth
- Success:
  - no textual response body for success path
  - bot adds emoji reaction to the command message (`message.id`)
- Failure:
  - return `ResponseMode.PRIVATE` message with explicit reason and fix hint
- Mixed outcome:
  - execute per-reviewer operations independently
  - if any reviewer fails or yields idempotent warning, send one private summary with grouped outcomes
  - still add success reaction when at least one mutation actually succeeded

## Detailed Plan

### 1) Parsing and Normalization
- Add a dedicated parser module for assignment commands (for example under `qb_site/zulip_bot/commands/` or `qb_site/zulip_bot/services/`).
- Parse `args` first for fast-path URL/mention extraction, but always reconcile with `payload["message"]["rendered_content"]` when available.
- Extract PR candidates from rendered HTML anchor `href` values matching:
  - `https://github.com/<owner>/<repo>/pull/<number>`
- Reject ambiguous input:
  - no PR found
  - multiple distinct PR links
- Mention parsing:
  - use Zulip mention markup in rendered HTML where possible
  - support one or more mentions in one command
  - deduplicate repeated mentions
  - fallback to sender when omitted
  - if any mention is syntactically present but unresolved, report per-mention failure in summary

### 2) Reviewer and Repo Validation
- Resolve each target reviewer from Zulip identity (`zulip_user_id`) to `core.User`.
- Require each reviewer `github_login` (non-empty).
- Require each reviewer has `ReviewerPreference` for target repo.
- Keep this in a separate validator function/service so future restrictions can be layered:
  - allow only self-assignment
  - allow only specific groups
  - allow only maintainers per repo

### 3) PR Resolution (Local First, GitHub Fallback)
- Try local lookup by repository + PR number (`syncer.PullRequest` + `core.Repository`).
- If missing:
  - fetch minimal PR header from GitHub (state/open/closed, repo identity, assignees)
- Use live data for command preconditions when local row is absent/stale.
- Preferred initial behavior:
  - read-through only for command execution
  - enqueue targeted sync after command attempt
  - avoid immediate DB upserts in the command path to keep command latency and transaction complexity lower
- Always enqueue/trigger post-action sync to converge local `assignees` and state.

### 4) Command Preconditions
- Shared preconditions:
  - reviewer exists and is linked to GitHub login
  - reviewer has repo-matching `ReviewerPreference`
  - PR is resolvable
- `assign` preconditions:
  - PR is open (not closed/merged)
  - if reviewer already assigned: record mild warning (non-exception path)
- `unassign` preconditions:
  - reviewer currently assigned; otherwise record mild warning (non-exception path)

### 5) GitHub App Installation-Token Platform
- Introduce a generic GitHub App credential subsystem (service layer, not command-specific):
  - app definition model/config:
    - `app_id`
    - private key (PEM)
    - optional key id / metadata
    - allowed operation scopes (assignments, syncer-read, syncer-write, etc.)
  - installation resolution:
    - per repository owner/name (cache installation id lookups)
  - JWT generation for app authentication
  - installation-token minting and refresh with expiration-aware caching
- Provide an operation-oriented token API, e.g.:
  - `get_token(operation="assign_pr", repo="owner/name")`
  - `get_token(operation="syncer_ingest", repo="owner/name")`
- Initial operation namespace:
  - `assign_pr`
  - `unassign_pr`
  - `syncer_repo_discovery`
  - `syncer_pr_read`
  - `syncer_ci_read`
- Token source policy:
  - support multiple apps with deterministic selection rules:
    - explicit operation mapping
    - fallback chain
    - future per-repo override (explicitly deferred; not part of initial implementation)
- Observability:
  - structured logs for token minting, selection, cache hit/miss, expiration refresh
  - redact private keys and token values from logs/errors
- Settings and secret shape:
  - represent app credentials as structured config (JSON/env + optional file references), supporting N apps without code changes
  - keep PAT path as temporary fallback only where explicitly allowed during migration

### 6) Assignment/Unassignment Execution
- Add GitHub operation client functions using installation tokens:
  - assign via Issues assignees endpoint
  - unassign via corresponding remove endpoint
- Normalize GitHub API errors to user-facing categories:
  - permission denied (app not installed / missing permission)
  - reviewer login invalid
  - PR not found / repo mismatch
  - transient GitHub failure
- On success:
  - react to command message with configured emoji (API call to add reaction)
  - return no success message
- On failure:
  - return private message with exact failure reason and next step
- For multi-target execution:
  - continue processing remaining reviewers after individual failures
  - aggregate `success`, `warning`, and `failure` buckets in one private summary

### 7) Zulip Reaction Behavior
- Add Zulip client support for adding reactions to a message id.
- Default success emoji is shared across assignment commands and set to `thumbs_up`.
- Keep reaction behavior configurable (emoji name default in settings, with command-level policy override support).
- If reaction API fails after successful GitHub operation:
  - do not rollback GitHub mutation
  - log warning
  - send private message noting operation succeeded but reaction failed

### 8) Policy and Extensibility Hooks
- Keep existing `ZULIP_COMMAND_POLICY` as top-level allow/deny gate.
- Allow command-level reaction emoji override in policy/config (for example under each command rule).
- Add command-internal authorization hook layer for future restrictions:
  - actor vs target checks
  - repo-level permissions
  - optional role/group-based constraints
- For now, default hook policy is permissive ("allowed for everyone who can invoke command").

### 9) Testing Plan
- Parser tests:
  - linkifier-derived PR extraction from `rendered_content`
  - URL parsing, multiple links, missing links, malformed markup
  - mention omitted vs present vs unresolved
- Command tests:
  - all validation failure branches with explicit error text
  - assign/unassign success paths
  - idempotent branches (already assigned / not assigned)
  - local-miss + GitHub-fallback path
- GitHub app token tests:
  - JWT creation, installation lookup, token cache refresh, app selection rules
- Integration-style webhook tests:
  - include `message.rendered_content`
  - assert success path produces reaction call and no success text response
  - assert failure path returns private message

## Implementation Progress

### 2026-02-20: Chunk 1 (Parsing foundation)
- Status: completed
- Implemented:
  - Added `zulip_bot.services.assignment_command_parser` with:
    - PR extraction from both `args` and `rendered_content` anchor `href`s
    - strict single-PR enforcement (`missing_pr`, `ambiguous_pr`)
    - mention parsing from rendered HTML entities (`data-user-id`)
    - sender fallback when no mentions are present
    - unresolved mention reporting when mention syntax is present but no resolvable Zulip user id is available
  - Extended `CommandContext` and webhook context builder to carry `message.rendered_content` for command handlers.
  - Added focused parser tests in `zulip_bot.tests.test_assignment_command_parser`.
- Nuances discovered during implementation:
  - To keep false positives low, mention resolution currently treats rendered HTML mention entities as source of truth and only uses raw `@**...**` tokens as unresolved hints when no rendered mention ids are available.
  - The parser intentionally supports full GitHub PR URLs (including those surfaced through Zulip linkifier anchors) and does not attempt to resolve bare `#123` without a rendered GitHub anchor.
  - When mentions are syntactically present but unresolved, parser output returns no fallback target (does not silently default to sender), so command handlers can surface explicit private warnings/errors.

### 2026-02-20: Chunk 2 (Reviewer/repo validation service)
- Status: completed
- Implemented:
  - Added `zulip_bot.services.assignment_validation.validate_assignment_targets(...)` to enforce reviewer/repo preconditions before GitHub mutations.
  - Validation outputs per-target structured results with explicit codes:
    - `ok`
    - `unknown_reviewer`
    - `missing_github_login`
    - `repository_not_configured`
    - `missing_preference`
  - Added DB-backed tests in `zulip_bot.tests.test_assignment_validation` for success and each failure mode.
- Nuances discovered during implementation:
  - Validation currently keys repository lookup by exact `owner`/`repo` match from parsed PR URL; no case-normalization is applied yet.
  - If the repository is absent locally, all otherwise-valid reviewer targets currently fail with `repository_not_configured`; this is intentional for now and will be relaxed in the later GitHub read-through fallback chunk.
  - Validation is intentionally command-agnostic (`assign` vs `unassign`) so command-specific preconditions (open/closed state, currently assigned checks) can be layered separately.

### 2026-02-20: Chunk 3 (Command wiring with preflight summaries)
- Status: completed
- Implemented:
  - Added first-class commands:
    - `zulip_bot.commands.assign`
    - `zulip_bot.commands.unassign`
  - Added shared `zulip_bot.services.assignment_preflight.run_assignment_preflight(...)` that composes:
    - parser output (`assignment_command_parser`)
    - reviewer/repo validation (`assignment_validation`)
    - private textual summary for success/fail/mixed preflight outcomes
  - Wired command registration import in `zulip_bot.views` so webhook command dispatch recognizes `assign`/`unassign`.
  - Added command tests in `zulip_bot.tests.commands.test_assign_unassign_commands`.
  - Added webhook integration test in `zulip_bot.tests.test_webhook_endpoint` for `assign`.
- Nuances discovered during implementation:
  - Until GitHub mutation/reaction plumbing lands, commands intentionally return a private “preflight passed” text response even on valid targets; this is a temporary behavior for incremental rollout.
  - Parser+validator integration currently prioritizes rendered mention ids over raw mention tokens to avoid accidental targeting from ambiguous plain text mentions.
  - Preflight summaries include stable machine-readable failure codes in parentheses to simplify future command-level error bucketing and regression assertions.

### 2026-02-20: Chunk 4 (Outcome bucketing + Zulip reaction client scaffold)
- Status: completed
- Implemented:
  - Refined `assignment_preflight` output into explicit grouped buckets:
    - `Successes`
    - `Warnings`
    - `Failures`
  - Added Zulip reaction API helper `ZulipClient.add_reaction(message_id, emoji_name)` targeting `/messages/{id}/reactions`.
  - Added default settings knob `ZULIP_ASSIGNMENT_SUCCESS_EMOJI` (default `thumbs_up`) for later command success reaction behavior.
  - Added/updated tests:
    - `zulip_bot.tests.test_zulip_client` now covers reaction endpoint payload/shape.
    - `zulip_bot.tests.commands.test_assign_unassign_commands` now asserts bucketed summary sections.
- Nuances discovered during implementation:
  - Preflight summaries now mirror the eventual mixed-outcome contract, so switching from preflight-only to mutation execution should mostly replace producer logic, not output structure.
  - The reaction client method intentionally keeps payload minimal (`message_id`, `emoji_name`) and defers custom emoji/reaction_type handling until needed.
  - Keeping success emoji configurable early avoids coupling command behavior to hardcoded reaction names when policy-level overrides are introduced.

### 2026-02-20: Chunk 5 (Feature-flagged mutation execution path)
- Status: completed
- Implemented:
  - Added `zulip_bot.services.assignment_execution` with:
    - parser + reviewer validation orchestration
    - local PR preconditions/idempotency checks (open-state guard, already-assigned/not-assigned warnings from local assignee snapshot)
    - feature-flagged mutation path via GitHub REST assignee endpoints
    - normalized mutation error codes (`permission_denied`, `pr_not_found`, `validation_failed`, `github_transient`, `github_error`)
    - success reaction attempt via Zulip API
  - Added `GitHubAssignmentClient` for REST assignment/unassignment calls.
  - Switched `assign`/`unassign` commands to `assignment_execution` service.
  - Added command response suppression support:
    - `CommandResult.response_not_required`
    - webhook response renderer now emits `{response_not_required: true}` when set.
  - Added new settings:
    - `ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`
    - `GITHUB_ASSIGNMENT_TOKEN`
  - Added tests:
    - `zulip_bot.tests.test_assignment_execution`
    - `zulip_bot.tests.test_github_assignment_client`
    - webhook test for clean mutation success path returning `response_not_required`.
- Nuances discovered during implementation:
  - Mutation execution is intentionally gated behind `ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`; default behavior remains preflight-only to keep rollout safe.
  - Local `PullRequest.assignees` is treated as an idempotency hint, so stale local data can produce warnings that differ from live GitHub state until read-through fallback is added.
  - Clean success now aligns with desired UX (`response_not_required` + reaction), while mixed outcomes continue to produce private summaries.

### 2026-02-20: Chunk 5a (Post-test correction)
- Status: completed
- Implemented:
  - Fixed local PR lookup in `assignment_execution._load_local_pr` by removing invalid `select_related("repository")` usage alongside deferred fields.
- Nuances discovered during implementation:
  - Django raises `FieldError` when a relation is both deferred via `.only(...)` and traversed via `select_related(...)`; for this lookup path, `select_related` is unnecessary because only PR-local fields (`id`, `state`, `assignees`) are read.

### 2026-02-20: Chunk 5b (Post-test correction)
- Status: completed
- Implemented:
  - Adjusted assignment execution summaries so `No valid reviewers to <action> after validation.` is always emitted when the valid target set is empty, even if other failures already exist.
  - Updated clean-success tests to patch `ZulipClient` construction (not just `add_reaction`) so reaction success paths are exercised without requiring full Zulip credential settings in test environments.
- Nuances discovered during implementation:
  - Patching instance methods alone is insufficient when the class constructor can fail first (e.g., missing config in `__init__`); patching the class boundary is safer for command-path tests.

### 2026-02-20: Chunk 6 (Live read-through fallback + post-action sync enqueue)
- Status: completed
- Implemented:
  - Added GitHub live PR read-through fallback in `assignment_execution` for cases where local `syncer.PullRequest` is missing.
  - Live fallback currently checks:
    - PR openness (`state` + `merged_at`)
    - current assignee set for idempotency checks
  - Added best-effort post-action sync enqueue hook after successful mutations:
    - attempts `syncer.sync_pr` enqueue for the affected repo/PR
    - failures are logged, not surfaced to end users
  - Added tests for:
    - live fallback closed-PR rejection
    - live fallback unassign idempotency warning behavior
    - successful mutation path invoking post-action sync enqueue
- Nuances discovered during implementation:
  - Live fallback uses the assignment token path; when no token is available, command logic continues using existing local/preflight behavior.
  - Enqueue failures are intentionally non-user-facing so clean mutation successes can still return `response_not_required`.

### 2026-02-20: Chunk 6a (Post-test correction)
- Status: completed
- Implemented:
  - Restricted live GitHub read-through fallback to mutation-enabled mode (`ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`) so preflight-only behavior remains deterministic and local-data based.
  - Downgraded post-action sync enqueue failure logging from exception traceback to warning-level event to reduce noisy test output in environments without Redis/Celery broker.
- Nuances discovered during implementation:
  - Running live fallback in preflight-only mode can create non-deterministic test behavior (depends on live GitHub PR state) and violates the intended incremental rollout boundary.

### 2026-02-20: Chunk 7 (GitHub App installation-token provider + assignment integration)
- Status: completed
- Implemented:
  - Added `zulip_bot.services.github_app_tokens` with:
    - structured multi-app config parsing (`GITHUB_APP_TOKEN_CONFIG`)
    - operation-to-app mapping (`operation_app_map`) with deterministic fallback to first app advertising the requested operation
    - GitHub App JWT generation (RS256)
    - installation-id lookup (`/repos/{owner}/{repo}/installation`) with cache
    - installation-token minting (`/app/installations/{id}/access_tokens`) with expiration-aware cache refresh
    - redaction-safe structured logging for cache hits/mints and error categories
  - Added new settings support:
    - `GITHUB_APP_TOKEN_CONFIG` (JSON object parsed at settings load)
  - Wired `assignment_execution` token resolution to:
    - request operation-scoped app tokens (`assign_pr`, `unassign_pr`) first
    - fall back to existing PAT/env token paths (`GITHUB_ASSIGNMENT_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`) when no app token is available or app resolution fails
  - Extended tests:
    - `zulip_bot.tests.test_github_app_tokens` covers mint+cache reuse, near-expiry refresh, and explicit operation mapping selection
    - `zulip_bot.tests.test_assignment_execution` now asserts assign flow uses app-token provider when available
- Nuances discovered during implementation:
  - `GITHUB_API_URL` remains the default REST base for app-token operations; if this setting includes a non-REST path in some environments, app config should explicitly set `api_base_url`.
  - Caching is process-local in this chunk (in-memory singleton provider); cross-process cache sharing is intentionally deferred.
  - Operation mapping errors are logged and PAT fallback is still allowed, matching the current migration-safe stance rather than strict enforcement.

### 2026-02-20: Chunk 7a (Operation-level strict mode for PAT fallback)
- Status: superseded by Chunk 7c
- Implemented:
  - Added operation-level strict mode handling in `assignment_execution`:
    - reads `GITHUB_APP_TOKEN_CONFIG.strict_operations` (list of operation names)
    - when the current operation is strict (`assign_pr`/`unassign_pr`), PAT/env fallback is disabled if app-token resolution yields no token or raises an app-token error
  - Added tests in `zulip_bot.tests.test_assignment_execution`:
    - strict `assign_pr` prevents fallback to `GITHUB_ASSIGNMENT_TOKEN` when app token is missing
    - non-strict `unassign_pr` continues to use PAT fallback
- Nuances discovered during implementation:
  - Strict mode currently only changes token-source selection; user-facing failure remains the existing generic message (`GitHub assignment token is not configured.`), which keeps contract stable but does not yet explicitly indicate strict-mode enforcement.
  - Strictness is interpreted at command execution time from settings, so behavior can be toggled without restarting workers only where settings reload semantics allow.

### 2026-02-20: Chunk 7b (Strict-mode failure UX: explicit GitHub App reasons)
- Status: superseded by Chunk 7c
- Implemented:
  - Refined assignment token resolution to return structured metadata (`AssignmentTokenResolution`) including:
    - resolved token (if any)
    - operation name
    - strict-operation flag
    - optional `GitHubAppTokenError` details
  - Updated `run_assignment_command` failure messaging:
    - strict operations now emit explicit private failures when app token resolution fails, including normalized app-token error code/message when available
    - non-strict operations keep existing generic token-missing behavior for compatibility
  - Added tests in `zulip_bot.tests.test_assignment_execution`:
    - strict-mode missing app token now yields strict-mode-specific failure text
    - strict-mode provider error (e.g. `installation_not_found`) is surfaced in private summary and blocks mutation
- Nuances discovered during implementation:
  - Exposing app-token error codes in private failures significantly improves operator/debuggability for rollout misconfigurations (app not installed, auth mismatch) without leaking secrets.
  - Live fallback token lookup now uses the same structured token-resolution path, keeping strict-mode behavior consistent across precondition reads and mutation execution.

### 2026-02-21: Chunk 7c (Strict mode removal)
- Status: completed
- Implemented:
  - Removed operation-level strict behavior from assignment token resolution.
  - Assignment flow now always follows one token policy:
    - try GitHub App installation token first
    - fallback to existing token sources (`GITHUB_ASSIGNMENT_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`) when app token is unavailable
  - Removed strict-mode-specific command failures and strict-mode-specific assignment tests.
- Nuances discovered during implementation:
  - This simplifies rollout and support by eliminating a second policy path; token-source behavior is now uniform across assign/unassign operations.

### 2026-02-21: Chunk 8 (Refactor: move GitHub services from Zulip app to Core app)
- Status: completed
- Implemented:
  - Moved GitHub-specific service modules from `zulip_bot` to `core.services`:
    - `github_oauth` -> `core.services.github_oauth`
    - `github_app_tokens` -> `core.services.github_app_tokens`
    - extracted assignment mutation client/error to `core.services.github_assignment`
  - Updated all callsites/imports in webhook/views, registration linking, assignment execution, and tests to reference `core.services.*`.
  - Removed old `zulip_bot.services.github_oauth` and `zulip_bot.services.github_app_tokens` modules after migration.
- Nuances discovered during implementation:
  - Assignment command tests can keep patching `zulip_bot.services.assignment_execution.GitHubAssignmentClient` because the symbol is imported into the command module namespace, even though implementation now lives in `core.services.github_assignment`.
  - This refactor is intentionally behavior-preserving; it changes ownership boundaries (service location) but not runtime command semantics.

### 2026-02-21: Chunk 8a (Refactor: move GitHub service unit tests to Core app)
- Status: completed
- Implemented:
  - Moved GitHub service-focused unit tests from `zulip_bot/tests` to `core/tests`:
    - `test_github_oauth.py`
    - `test_github_app_tokens.py`
    - `test_github_assignment_client.py`
  - Kept Zulip command/webhook/registration flow tests in `zulip_bot/tests` unchanged.
- Nuances discovered during implementation:
  - No behavior assertions changed; this is test ownership and path cleanup only.
  - Import targets already referenced `core.services.*`, so relocation required no runtime code changes.

### 2026-02-21: Chunk 9 (Shared operation-token resolver in Core)
- Status: completed
- Implemented:
  - Added `core.services.github_operation_tokens.resolve_github_operation_token(...)` as shared token resolution logic:
    - operation-scoped app token lookup first
    - fallback chain next (named Django settings token(s), then env token list)
    - warning-level logging on app-token lookup errors, with fallback continuing
  - Migrated `zulip_bot.services.assignment_execution` to use this resolver (behavior-preserving).
  - Added new core unit tests:
    - `core.tests.test_github_operation_tokens`
    - covers app-token success, setting fallback, env fallback, app-error fallback, and no-operation fallback path
- Nuances discovered during implementation:
  - Centralizing resolution reduces duplication and makes syncer adoption incremental: syncer can now call the same resolver without copying assignment-specific fallback logic.
  - Existing assignment-specific compatibility is preserved by passing `("GITHUB_ASSIGNMENT_TOKEN",)` as the named settings fallback in command execution.

### 2026-02-21: Chunk 10 (Syncer command-path adoption of operation tokens)
- Status: completed
- Implemented:
  - Extended `syncer.services.github_client.GitHubClient` to accept optional operation/repo context:
    - `operation`, `owner`, `repo` constructor args
    - when provided and no explicit token is passed, client attempts shared operation-token resolution first
    - existing env-token chooser behavior remains as fallback
  - Wired repo-scoped operation context into syncer management commands:
    - `list_changed_prs` now uses operation `syncer_repo_discovery`
    - `sync_repo` now uses operation `syncer_pr_read`
  - Added syncer client test coverage for operation-token initialization path.
- Nuances discovered during implementation:
  - This chunk intentionally targets command paths only; Celery/task paths still instantiate `GitHubClient()` without operation context, preserving current production behavior while enabling incremental migration.
  - Fallback order in `GitHubClient` remains compatible with existing rate-budget token selection when no operation token is resolved.

## Consequences
- Pros:
  - robust handling of real Zulip input patterns (linkifiers, mentions)
  - safer and more scalable auth model via GitHub Apps
  - clear UX (emoji success, detailed private failures)
  - strong foundation for future syncer PAT replacement
- Trade-offs:
  - more infrastructure complexity (app JWT/token lifecycle, app selection rules)
  - more edge cases around partial success (GitHub success + Zulip reaction failure)
  - rollout requires careful secrets/config management

## Operational Notes
- Rollout sequencing:
  - ship token platform first (feature-flag command execution)
  - enable assignment app credentials and installation permissions
  - then enable `assign`/`unassign` command handlers
- Required app permissions:
  - assignments require appropriate repository issues/pull request permissions on GitHub App installation
- Suggested settings additions:
  - app registry config for multiple GitHub Apps
  - operation-to-app mapping config
  - assignment success reaction emoji name (global default: `thumbs_up`)
  - optional per-command reaction emoji override in `ZULIP_COMMAND_POLICY`
- Migration path for syncer:
  - start by routing read-only sync operations through app-token provider
  - deprecate PAT env vars after parity and soak period

## Alternatives
- Keep PAT-only model and add assignment scope PAT:
  - rejected; does not scale to multi-purpose operations and long-term credential hygiene.
- Use one GitHub App for everything:
  - deferred; may be acceptable, but design keeps operation/app mapping flexible to avoid over-permissioning.
- Always require local PR presence (no GitHub fallback):
  - rejected; causes poor UX and avoidable command failures.

## Open Questions
- None currently.
