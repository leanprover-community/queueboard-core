# Zulip Bot Architecture

## Context
- We need a Zulip outgoing webhook bot to let reviewers interact with Queueboard.
- The integration should be isolated from existing Django apps while still calling into their services.
- Commands must be explicit about response visibility to avoid accidental private data leaks.
- We want to make it easy to add new commands and keep help output accurate.

## Decision
- Add a new Django app at `qb_site/zulip_bot/` to own webhook parsing, auth, and command routing.
- Expose the webhook endpoint at `/api/zulip/webhook/` and register it in `qb_site/qb_site/urls.py`.
- Implement a small command registry (`zulip_bot.commands`) with explicit `response_mode` per command.
- Keep command handlers thin and delegate to services in existing apps (`core`, `syncer`, `analyzer`, `api`).
- Configure the Zulip webhook token via `ZULIP_WEBHOOK_TOKEN` in settings.

## Consequences
- Zulip-specific parsing and auth are isolated, so future Slack/Discord integrations can be added independently.
- Command output visibility is explicit, which reduces the risk of leaking private context.
- Adding commands only requires a new handler and registry registration.

## Operational Notes
- Set `ZULIP_WEBHOOK_TOKEN` in `.env` before enabling the bot in Zulip.
- Commands that may surface private details should use `ResponseMode.PRIVATE`.
- When adding new commands, update help text by registering the command description.

## Alternatives
- Reuse the existing `api` app for webhook endpoints.
  - Rejected to keep Zulip-specific routing and auth isolated from public API paths.
