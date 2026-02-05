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


@dataclass(frozen=True)
class CommandResult:
    content: str
    response_mode: ResponseMode


CommandHandler = Callable[[CommandContext, str], CommandResult]


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    description: str
    handler: CommandHandler
    response_mode: ResponseMode


_COMMANDS: dict[str, CommandDefinition] = {}


def register_command(*, name: str, description: str, response_mode: ResponseMode) -> Callable[[CommandHandler], CommandHandler]:
    def decorator(handler: CommandHandler) -> CommandHandler:
        definition = CommandDefinition(
            name=name,
            description=description,
            handler=handler,
            response_mode=response_mode,
        )
        _COMMANDS[name] = definition
        return handler

    return decorator


def get_command(name: str) -> CommandDefinition | None:
    return _COMMANDS.get(name)


def list_commands() -> list[CommandDefinition]:
    return sorted(_COMMANDS.values(), key=lambda cmd: cmd.name)
