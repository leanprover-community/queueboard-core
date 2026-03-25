from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class ResponseMode(str, Enum):
    STREAM = "stream"
    PRIVATE = "private"


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
    content: str
    response_mode: ResponseMode
    response_not_required: bool = False


CommandHandler = Callable[[CommandContext, str], CommandResult]


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    description: str
    handler: CommandHandler
    response_mode: ResponseMode
    aliases: tuple[str, ...] = ()


_COMMANDS: dict[str, CommandDefinition] = {}


def register_command(
    *, name: str, description: str, response_mode: ResponseMode, aliases: tuple[str, ...] = ()
) -> Callable[[CommandHandler], CommandHandler]:
    def decorator(handler: CommandHandler) -> CommandHandler:
        definition = CommandDefinition(
            name=name,
            description=description,
            handler=handler,
            response_mode=response_mode,
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
