# Zulip Assign and Unassign Commands

## Context
- Queueboard needs first-class Zulip commands to manage GitHub PR assignees:
  - `assign <pr> <optional zulip mention(s)>`
  - `unassign <pr> <optional zulip mention(s)>`
- Zulip command text often contains PR linkifiers (`#123`, `repo#123`) that are only reliably resolvable via `message.rendered_content` (HTML), not plain `message.content`.
- Reviewer targets must be validated against Queueboard identity and repository configuration before any GitHub mutation.
- UX requirements:
  - clean success should not post a message body
  - success should add a reaction to the command message
  - failures must be private and explicit
  - idempotent outcomes (`already assigned`, `not assigned`) should be warnings, not hard failures
- Local sync state can be missing or stale; command behavior must remain robust.

## Decision
- Implement `assign` and `unassign` as first-class Zulip bot commands with structured parsing, validation, and execution services.
- Parse PR and mention targets primarily from rendered HTML, with conservative fallbacks from raw args.
- Support multi-target execution (multiple mentioned reviewers) with partial success and grouped outcome summaries.
- Keep command authorization aligned with existing `ZULIP_COMMAND_POLICY` gates; keep internal authorization hooks permissive for now and extensible later.
- Use local PR data first; when mutation mode is enabled and local PR is missing, use live GitHub read-through for command preconditions.
- Execute post-mutation convergence by enqueueing targeted sync (`sync_pr`) on best effort.
- Keep GitHub App token platform concerns in a separate decision document.

## Command Contract
- Syntax:
  - `assign <pr> <optional zulip mention(s)>`
  - `unassign <pr> <optional zulip mention(s)>`
- PR input resolution:
  - accepts full GitHub PR URLs in args/content
  - accepts Zulip linkifier-rendered links in `message.rendered_content`
  - rejects missing PR and ambiguous multi-PR input
- Mention behavior:
  - no mention => target defaults to sender
  - one or more mentions => all mentioned users are targets (deduplicated)
  - unresolved syntactic mentions are reported as warnings/failures; parser does not silently retarget to sender in that case
- Response behavior:
  - clean success: no message body (`response_not_required`) + success reaction
  - mixed outcomes: private summary grouped into `Successes`, `Warnings`, `Failures`
  - full failure: private summary with explicit causes and actionable hints

## Detailed Design

### 1) Parsing and Normalization
- Service: `zulip_bot.services.assignment_command_parser`.
- Inputs:
  - raw command args
  - `message.rendered_content`
  - sender Zulip user id
- Behavior:
  - extracts PR URL candidates from rendered anchor `href`s matching `https://github.com/<owner>/<repo>/pull/<number>`
  - enforces exactly one PR target (`missing_pr`, `ambiguous_pr`)
  - resolves mentions from rendered mention entities (`data-user-id`) as source of truth
  - uses raw mention syntax only as unresolved hints when rendered ids are unavailable

### 2) Reviewer and Repository Validation
- Service: `zulip_bot.services.assignment_validation.validate_assignment_targets(...)`.
- Validation checks per target:
  - Zulip user maps to Queueboard user (`core.User.zulip_user_id`)
  - mapped user has `github_login`
  - target repository exists locally
  - reviewer has `ReviewerPreference` for target repository
- Validation results are structured and code-based (`ok`, `unknown_reviewer`, `missing_github_login`, `repository_not_configured`, `missing_preference`) for stable reporting and tests.

### 3) Preconditions and Idempotency
- Local-first PR resolution:
  - use local `syncer.PullRequest` when available
  - guard that PR is open before mutation
  - apply idempotency checks from assignee snapshot
- Live read-through (mutation mode only):
  - when local PR is missing, read PR openness and assignees from GitHub
  - apply the same idempotency semantics against live assignee state
- Action-specific idempotency:
  - `assign`: already-assigned target => warning, no mutation for that target
  - `unassign`: target not assigned => warning, no mutation for that target

### 4) Mutation Execution
- Service: `zulip_bot.services.assignment_execution.run_assignment_command(...)`.
- Feature flag:
  - live mutation requires `ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`
  - when disabled, command runs preflight and returns private summary
- GitHub mutation client:
  - `core.services.github_assignment.GitHubAssignmentClient`
  - `POST/DELETE /repos/{owner}/{repo}/issues/{number}/assignees`
- Error normalization:
  - permission denied
  - PR not found
  - assignee validation failed
  - transient GitHub failure
  - generic GitHub error

### 5) Reaction and Response Semantics
- Success reaction:
  - `ZulipClient.add_reaction(message_id, emoji_name)`
  - default emoji from `ZULIP_ASSIGNMENT_SUCCESS_EMOJI` (`thumbs_up`)
- If reaction fails after successful mutation:
  - mutation is not rolled back
  - command returns private warning noting reaction failure

### 6) Post-action Convergence
- After at least one successful mutation:
  - enqueue best-effort `sync_pr` task for the affected repository/PR
- Enqueue failures are logged and not surfaced as hard user-facing failures.

### 7) Authorization Extensibility
- Current behavior:
  - command availability and contexts are controlled by `ZULIP_COMMAND_POLICY`
  - internal target restrictions are permissive
- Explicit extension point remains for future policy hardening:
  - actor-vs-target constraints
  - repository-level restrictions
  - role/group based authorization

## Security and Trust Model
- Target identity trust chain:
  - Zulip sender/mentions are resolved via webhook payload and rendered entities
  - GitHub mutation identity uses Queueboard-linked GitHub logins
- Visibility model:
  - failures and mixed outcomes are private
  - clean success avoids channel noise via reaction-only acknowledgement
- Token platform and credential handling are documented separately in `027`.

## Operational Notes
- Required settings for live mutation mode:
  - `ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`
  - `ZULIP_ASSIGNMENT_SUCCESS_EMOJI` (optional override)
- Parsing depends on `message.rendered_content` being present in webhook payload context.
- Local repository rows and reviewer preferences must be configured for target reviewers.
- GitHub App setup and operation mapping:
  - `docs/github_app_setup.md`
  - `docs/design-decisions/027-github-app-operation-token-services.md`

## Testing Strategy
- Parser tests:
  - PR extraction from rendered links
  - ambiguous/missing PR handling
  - mention resolution and unresolved mention behavior
- Validation tests:
  - each validation code path
- Command execution tests:
  - preflight-only behavior
  - mutation success path with reaction-only response
  - mixed outcomes across multiple targets
  - local-miss live-read-through behavior
  - post-action sync enqueue behavior
- Webhook integration tests:
  - ensure `rendered_content` is threaded into command context
  - ensure response format matches clean success vs private summary paths

## Consequences
- Pros:
  - robust parsing for real Zulip usage patterns
  - clear and low-noise success UX
  - explicit private failures for operators and users
  - behavior-preserving path from preflight to live mutation mode
- Trade-offs:
  - command complexity is higher due to multi-stage parsing/validation/preconditions
  - local-vs-live state differences can produce warning variance
  - success path depends on both GitHub and Zulip APIs for full UX fidelity

## Alternatives Considered
- Parse only raw message content.
  - Rejected: unreliable for Zulip linkifier-heavy usage.
- Require local PR presence only.
  - Rejected: leads to avoidable user failures when sync lags.
- Always post textual success messages.
  - Rejected: too noisy for frequent assignment operations.

## Open Questions
- None currently.

## References
- `docs/design-decisions/021-zulip-bot-architecture.md`
- `docs/design-decisions/027-github-app-operation-token-services.md`
- `docs/github_app_setup.md`
- `qb_site/zulip_bot/services/assignment_command_parser.py`
- `qb_site/zulip_bot/services/assignment_validation.py`
- `qb_site/zulip_bot/services/assignment_execution.py`
