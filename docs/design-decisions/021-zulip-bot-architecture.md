# Zulip Bot Architecture

## Context
- We need a Zulip outgoing webhook bot to let reviewers interact with Queueboard.
- The integration should be isolated from existing Django apps while still calling into their services.
- Commands must be explicit about response visibility to avoid accidental private data leaks.
- We want to make it easy to add new commands and keep help output accurate.
- We need policy-based command authorization by user group and message context.

## Decision
- Add a new Django app at `qb_site/zulip_bot/` to own webhook parsing, auth, and command routing.
- Expose the webhook endpoint at `/api/zulip/webhook/` and register it in `qb_site/qb_site/urls.py`.
- Implement a small command registry (`zulip_bot.commands`) with explicit `response_mode` per command.
- Keep command handlers thin and delegate to services in existing apps (`core`, `syncer`, `analyzer`, `api`).
- Configure Zulip credentials via settings/env:
  - `ZULIP_WEBHOOK_TOKEN`
  - `ZULIP_BASE_URL`
  - `ZULIP_BOT_EMAIL`
  - `ZULIP_BOT_API_KEY`
- Configure per-command access policy via:
  - Django settings `ZULIP_COMMAND_POLICY`, or
  - env var `ZULIP_COMMAND_POLICY` (JSON object).

## Policy Format
- `ZULIP_COMMAND_POLICY` is a dictionary keyed by command name.
- Each command rule currently supports:
  - `allowed_groups`: list of Zulip user-group IDs.
  - `allowed_contexts`: list of context selectors.
- Empty or omitted `allowed_groups` means no allowed groups (deny).
- Empty or omitted `allowed_contexts` means no allowed contexts (deny).
- Use `"*"` or `"all"` in either list to mean unrestricted.
- Supported context selectors:
  - `dm`
  - `stream:<stream_id>`
  - `stream:*`
  - `*`
  - `all`

Example:
```python
ZULIP_COMMAND_POLICY = {
    "help": {
        "allowed_groups": [1234],
        "allowed_contexts": ["dm", "stream:5678", "stream:9012"],
    },
    "echo": {
        "allowed_groups": [1234],
        "allowed_contexts": ["dm"],
    },
}
```

## Enforcement Rules
- Commands are denied by default unless they have a matching policy entry.
- If `ZULIP_COMMAND_POLICY` is empty or unset, all commands are ignored.
- If a command is missing from `ZULIP_COMMAND_POLICY`, that command is ignored.
- Webhook payloads are validated against expected Zulip message fields, including:
  - `message.id`
  - `message.type`
  - `message.content`
  - `message.sender_id`
  - `message.sender_email`
  - `message.sender_full_name`
  - and `message.stream_id` for stream messages.
- Commands outside policy are silently ignored (`HTTP 200` with empty response body).
- Unknown commands:
  - Return private filtered help if user/context is allowed for at least one command.
  - Are ignored if user/context is not allowed for any command.
- `help` output is filtered to only commands allowed for the triggering user/context.
- Invalid payloads return `HTTP 400` and include parse/validation errors plus received payload data.
- Sender bot detection is based on Zulip API user lookup by `message.sender_id` (cached per webhook request).

## Consequences
- Zulip-specific parsing and auth are isolated, so future Slack/Discord integrations can be added independently.
- Command output visibility is explicit, which reduces the risk of leaking private context.
- Access control can be tuned per command without editing command handler code.
- Using IDs in policy avoids runtime name-to-ID lookups and reduces ambiguity from renamed streams/groups.

## Operational Notes
- Set the Zulip env vars in `.env` before enabling the bot:
  - `ZULIP_WEBHOOK_TOKEN`
  - `ZULIP_BASE_URL`
  - `ZULIP_BOT_EMAIL`
  - `ZULIP_BOT_API_KEY`
- Set `ZULIP_COMMAND_POLICY` in Django settings (`qb_site/qb_site/settings/local.py` or an environment-specific settings module).
- For env-only deployments, set `ZULIP_COMMAND_POLICY` to a compact JSON object.
- Local helper tool for building/validating policy JSON:
  - `uv run python qb_site/manage.py zulip_policy init .zulip-policy.local.json`
  - `uv run python qb_site/manage.py zulip_policy validate .zulip-policy.local.json`
  - `uv run python qb_site/manage.py zulip_policy sync .zulip-policy.local.json`
  - `uv run python qb_site/manage.py zulip_policy to-env .zulip-policy.local.json --export`
- Commands that may surface private details should use `ResponseMode.PRIVATE`.
- When adding new commands, update help text by registering the command description.
- For each new command, add a policy entry when `ZULIP_COMMAND_POLICY` is enabled; otherwise that command is ignored.

## Alternatives
- Reuse the existing `api` app for webhook endpoints.
  - Rejected to keep Zulip-specific routing and auth isolated from public API paths.
- Store policy by names instead of IDs.
  - Deferred. Names are easier to read, but would require Zulip API resolution and caching to avoid ambiguity.
