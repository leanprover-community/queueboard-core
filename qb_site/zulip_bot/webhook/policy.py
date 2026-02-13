from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from zulip_bot.commands import CommandContext, list_commands
from zulip_bot.webhook.membership import GroupMembershipChecker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandPolicy:
    allowed_groups: frozenset[int] | None = None
    allowed_contexts: frozenset[str] | None = None


def allowed_command_names(context: CommandContext, checker: GroupMembershipChecker) -> frozenset[str]:
    policy = _load_command_policy()
    allowed: set[str] = set()
    for command in list_commands():
        if _is_command_allowed(command.name, context=context, policy=policy, checker=checker):
            allowed.add(command.name)
    return frozenset(allowed)


def _load_command_policy() -> dict[str, CommandPolicy]:
    raw_policy = getattr(settings, "ZULIP_COMMAND_POLICY", {})
    if not isinstance(raw_policy, dict):
        return {}

    policies: dict[str, CommandPolicy] = {}
    for command_name, rule in raw_policy.items():
        if not isinstance(command_name, str) or not isinstance(rule, dict):
            continue
        groups = _parse_group_set(rule.get("allowed_groups"))
        contexts = _parse_context_set(rule.get("allowed_contexts"))
        policies[command_name] = CommandPolicy(allowed_groups=groups, allowed_contexts=contexts)
    return policies


def _parse_group_set(value: Any) -> frozenset[int] | None:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        return frozenset()
    unrestricted = False
    parsed: set[int] = set()
    for item in value:
        if isinstance(item, int) and item > 0:
            parsed.add(item)
        elif isinstance(item, str) and item.lower() in {"*", "all"}:
            unrestricted = True
    if unrestricted:
        return None
    return frozenset(parsed)


def _parse_context_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        return frozenset()
    unrestricted = False
    parsed: set[str] = set()
    for item in value:
        if isinstance(item, str) and item:
            if item.lower() in {"*", "all"}:
                unrestricted = True
                continue
            parsed.add(item)
    if unrestricted:
        return None
    return frozenset(parsed)


def _is_command_allowed(
    command_name: str,
    *,
    context: CommandContext,
    policy: dict[str, CommandPolicy],
    checker: GroupMembershipChecker,
) -> bool:
    rule = policy.get(command_name)
    if rule is None:
        logger.info("zulip_command_ignored", extra={"reason": "no_policy_entry", "command": command_name})
        return False

    if not checker.is_member_any(user_id=context.sender_id, group_ids=rule.allowed_groups):
        logger.info("zulip_command_ignored", extra={"reason": "not_in_allowed_group", "command": command_name})
        return False

    if not _is_context_allowed(context=context, allowed_contexts=rule.allowed_contexts):
        logger.info("zulip_command_ignored", extra={"reason": "context_not_allowed", "command": command_name})
        return False

    return True


def _is_context_allowed(*, context: CommandContext, allowed_contexts: frozenset[str] | None) -> bool:
    if allowed_contexts is None:
        return True
    if not allowed_contexts:
        return False

    if context.is_private:
        return "dm" in allowed_contexts

    if "stream:*" in allowed_contexts:
        return True
    if context.stream_id is None:
        return False
    return f"stream:{context.stream_id}" in allowed_contexts
