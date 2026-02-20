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
  - strict mode toggle for forbidding PAT fallback per operation
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
