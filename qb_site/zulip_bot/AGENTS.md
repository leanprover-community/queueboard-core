# Zulip Bot Guidelines

## Scope
- `qb_site/zulip_bot/` owns webhook intake, command routing, policy checks, preference/registration flows, and Zulip API interactions.
- Keep command surface in `commands/`, reusable logic in `services/`, and webhook request parsing/policy in `webhook/`.

## High-Value Commands and Tests
```bash
# App test suite
docker compose exec -T web env DJANGO_SETTINGS_MODULE=qb_site.settings.ci python qb_site/manage.py test zulip_bot

# Policy inspection helper
docker compose exec -T web python qb_site/manage.py zulip_policy

# Frontend tests (prefs form)
cd qb_site/zulip_bot/frontend && npm test
```

## Command Architecture Notes
- Commands live in `commands/`: `assign`, `unassign`, `assigned-prs`, `pr-info`, `prefs`, `console`, `help`, `echo`, `register-test`, `close-pr`, `label-pr`.
- **Command names are normalized** by `commands.normalize_command_name` (trim, lowercase, `_`→`-`) at
  registration, at parse time, and for `ZULIP_COMMAND_POLICY` keys. Register hyphenated names; a name
  outside that space used to be silently undispatchable (`register_test` was, for exactly this reason).
- `console`: replies in place with the stable, token-less reviewer console URL (`build_site_url(reverse("console:home"))`, design doc 050) where a reviewer accepts/declines assignment proposals. The link is non-secret and identical for everyone (the console self-authenticates via GitHub OAuth), so it is an in-place reply, not a proactive DM.
- `pr-info`: parses GitHub PR links from Zulip `rendered_content`, reacts with 👀, then sends one message per PR (up to 10) with queue info sourced from `analyzer.services.pr_info`. Replies in the same conversation as the triggering message. Renders the acceptance-gate "Proposed to X (awaiting acceptance, expires …)" state (design doc 050) on its own line, distinct from Assignees.
- Assignment command flow (all under `services/`) is split for clarity:
  - parse: `assignment_command_parser.py`,
  - validate: `assignment_validation.py`,
  - preflight/mutation orchestration: `assignment_execution.py` + `assignment_preflight.py`.
- `close-pr` command: checks GitHub permission at command time, then issues a short-lived private link to a confirmation form. Services: `close_pr_links.py` (token), `close_pr_execution.py` (permission check + `close_pull_request` + `add_pr_labels` + `post_pr_comment`). Feature flag: `ZULIP_CLOSE_PR_MUTATIONS_ENABLED`. Uses operation `close_pr` via the GitHub App token system. The confirmation form includes an optional add-only label picker (checkboxes from `LabelDef` DB, none pre-checked); selected labels are POSTed to GitHub before closing and mentioned in the DM/log.
- `label-pr` command: same secure-link pattern as `close-pr`. Accepts both `/pull/NNN` and `/issues/NNN` URLs. Requires write/admin collaborator access (no author exception). Services: `label_pr_links.py` (token), `label_pr_execution.py` (permission check + `PUT /issues/{number}/labels`). Feature flag: `ZULIP_LABEL_PR_MUTATIONS_ENABLED`. Uses operation `label_pr` (mapped to `queueboard-assignment`). The picker catalog comes from `LabelDef` in DB; current-label pre-selection comes from the live `GET /issues/{number}` response (because `PUT /labels` replaces the full set, a stale source would silently drop labels). Live labels not in the catalog are appended as extra pre-checked rows; if the live fetch fails, the form refuses to render the picker. URL parsing for both PR and issue URLs is in `assignment_command_parser._parse_single_issue_or_pr_ref`.
- Keep user-facing command responses explicit and safe for partial failures.
- Prefer private failure responses for sensitive mutation/policy errors.

## How Command Replies Are Routed

**Zulip outgoing webhook responses always go back to the same conversation as the triggering message** — stream messages get stream replies, DM messages get DM replies. There is no supported mechanism to redirect a webhook response to a different destination via the response body.

This has a critical implication: **never return sensitive content (token links, private URLs) in `CommandResult.content` if the command can be invoked from a stream.** Doing so would expose that content to all stream subscribers.

### Two patterns for replies

**In-place reply** (most commands): return `CommandResult(content="...")`. Zulip delivers the reply wherever the command was invoked. Use this for non-sensitive content where in-stream visibility is acceptable (e.g., `assign`, `unassign`, `pr-info`, error messages).

**Proactive DM** (commands that send private links): call `ZulipClient().send_direct_message()` directly and return `CommandResult(response_not_required=True)`. Zulip does not deliver the webhook response at all; the DM goes to the user regardless of where the command was invoked. Use this whenever the reply contains a private token link or other content that must not appear in a stream.

Commands currently using the proactive DM pattern: `close-pr`, `label-pr`, `prefs`, `register-test`, `assigned-prs`.
`prefs` stays in that list whatever `CONSOLE_PREFS_ENABLED` says — with the flag on it DMs the stable
`/console/preferences/` URL instead of an expiring token link, but it still answers by DM. The URL is
not secret (the page self-authenticates); the reason is noise: an accidental mention in a public stream
must not post a reply there. `console` is the deliberate exception, an in-place reply by design (doc
050). `prefs`'s registration-link branch is a DM for the stronger reason — *that* link is a bearer
secret.

### Adding new commands

- If the command replies with a private link (or any content that must stay private): use the proactive DM pattern. See `commands/close_pr.py` for the canonical example.
- If the command replies with non-sensitive status text: return `CommandResult(content=...)` directly.
- The `CommandResult.response_not_required` field is also used by commands that send multiple messages proactively (e.g., `assigned-prs` chunks large reports across several DMs).

## Policy and Safety Notes
- Command availability and context restrictions are controlled by `ZULIP_COMMAND_POLICY`.
- Mutation paths are feature-flagged (`ZULIP_ASSIGNMENT_MUTATIONS_ENABLED`, `ZULIP_CLOSE_PR_MUTATIONS_ENABLED`, `ZULIP_LABEL_PR_MUTATIONS_ENABLED`) and depend on GitHub operation-token services.
- Do not log secrets/tokens or raw sensitive payload fragments.

## Per-Repo Zulip Log
- `ZULIP_REPO_LOG` is a JSON setting mapping `"owner/repo"` to `{"stream": "...", "topic": "..."}`.
- Used by both `close-pr` and `label-pr` to post an audit log entry after a mutation.
- If a repo has no entry, the log post is skipped and a WARNING is emitted.

## Registration and Preferences
- Registration-link/state behavior is in `services/`:
  - `registration_links.py`,
  - `registration_oauth_state.py`,
  - `registration_linking.py`,
  - `registration_bootstrap.py` (initial bootstrap helpers),
  - `prefs_links.py` (preference deep-link generation, plus `build_prefs_entry_link` — the single
    flag-aware answer to "where do I send a reviewer to edit preferences", used by the `prefs` command
    and the registration DM/page so they cannot disagree),
  - `close_pr_links.py` (close-PR confirmation link generation),
  - `label_pr_links.py` (label-PR confirmation link generation),
  - `user_timezone.py` (the timezone a reviewer's local times are interpreted in: Zulip's reported
    zone → `core.User.timezone` → Django default). Shared with the console prefs page so a naive
    `away_until` means the same thing on both; it lives here because the authoritative source is
    Zulip's user record, and `core` carries no app dependencies.
- Zulip prefs form/UI behavior spans Django forms/views and `frontend/` tests; keep behavior parity across backend validation and frontend affordances.
- **The preferences form itself is not owned by this app.** `core.forms.ReviewerPreferenceForm` (the
  editable-field set + validation) and `core.services.reviewer_prefs` (formset assembly, ownership
  scoping, label catalog) are shared with the reviewer console, which serves the same form at
  `/console/preferences/` under its GitHub-OAuth session (design doc 022 amendment). The fields live
  in one partial, `templates/shared/_reviewer_prefs_fields.html`. Change those, not a per-page copy —
  and expect both pages to pick the change up.
- `views.prefs_form` is now only the token *auth* path: validate the link, run the anti-tamper checks
  in `_load_authorized_preferences`, then hand the rows to the shared builder. It is slated for
  removal once the token flow is retired (022, phase 3).
- `views.register_github_callback` **opens the console session** (`console.session.set_reviewer`) when
  the console owns preferences and the new reviewer has rows to edit, so the "Edit Preferences Now"
  link lands signed in instead of bouncing through OAuth again. That promotion is strictly stronger
  than a console sign-in: registration proves Zulip identity (the registration token) *and* GitHub
  identity (OAuth). Success path only — a link conflict returns before it.
- `static/zulip_bot/prefs_form.js` is imported by `close_pr_form.js` and `label_pr_form.js` for the
  expiry helpers (`getExpiryState`, `formatRemaining`), so it must stay a sibling of those files —
  relative ES imports resolve against the *served* static path in the browser but the *on-disk* path
  under vitest, and only colocation satisfies both. The console prefs page therefore loads
  `zulip_bot/prefs_form.*` rather than a copy; mounting tolerates a missing countdown block.

## Testing Expectations
- Canonical full validation for repo changes is `bash scripts/repo_check_compose.sh`.
- `tests/test_command_registry.py::TestRegisteredCommandsAreDispatchable` iterates the **live**
  registry and asserts every registered name and alias survives `parse_command` → `get_command`, and
  that each canonical name is already normalized (it is what `help` prints). Adding a command with an
  unreachable name fails there without anyone writing a per-command test. Verified to fail on the
  reintroduced `register_test` bug, not just to pass today.
- In sandboxed environments where Docker is blocked:
  - run app-level tests that are still feasible,
  - run frontend unit tests independently when possible,
  - clearly report which integration paths (webhook + DB + Celery) were not exercised.
