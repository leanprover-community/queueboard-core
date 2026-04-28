from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CommandContext:
    sender_id: int | None
    sender_email: str | None
    sender_full_name: str | None
    message_content: str
    message_id: int | None
    stream_id: int | None
    topic: str | None
    is_private: bool
    rendered_content: str | None = None
    allowed_command_names: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CommandResult:
    # Set content to the reply text. Zulip always delivers the reply to the same
    # conversation (stream or DM) as the triggering message — there is no way to
    # redirect it via the webhook response. Commands that must reach the user
    # privately regardless of where they were invoked must send a DM proactively
    # via ZulipClient.send_direct_message() and return response_not_required=True
    # (see commands/close_pr.py for the canonical example of this pattern).
    content: str = ""
    response_not_required: bool = False


CommandHandler = Callable[[CommandContext, str], CommandResult]


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    description: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()


_COMMANDS: dict[str, CommandDefinition] = {}


def register_command(*, name: str, description: str, aliases: tuple[str, ...] = ()) -> Callable[[CommandHandler], CommandHandler]:
    def decorator(handler: CommandHandler) -> CommandHandler:
        definition = CommandDefinition(
            name=name,
            description=description,
            handler=handler,
            aliases=aliases,
        )
        _COMMANDS[name] = definition
        for alias in aliases:
            _COMMANDS[alias] = definition
        return handler

    return decorator


def get_command(name: str) -> CommandDefinition | None:
    return _COMMANDS.get(name)


def list_commands() -> list[CommandDefinition]:
    """Return deduplicated command definitions sorted by canonical name.

    Aliases share the same ``CommandDefinition`` object; deduplication ensures
    each command appears exactly once regardless of how many aliases it has.
    """
    unique = {cmd.name: cmd for cmd in _COMMANDS.values()}
    return sorted(unique.values(), key=lambda cmd: cmd.name)
