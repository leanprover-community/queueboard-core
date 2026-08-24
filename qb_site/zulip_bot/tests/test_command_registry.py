from __future__ import annotations

from unittest import TestCase

from zulip_bot.commands import CommandContext, CommandDefinition, CommandResult, register_command


def _make_handler(label: str):
    def handler(context: CommandContext, args: str) -> CommandResult:
        return CommandResult(content=label)

    return handler


class TestCommandRegistry(TestCase):
    """Tests for the alias support in the command registry."""

    def setUp(self) -> None:
        # Import the live _COMMANDS dict so we can restore it after each test.
        import zulip_bot.commands as registry_module

        self._registry_module = registry_module
        self._original_commands = dict(registry_module._COMMANDS)

    def tearDown(self) -> None:
        self._registry_module._COMMANDS.clear()
        self._registry_module._COMMANDS.update(self._original_commands)

    def _register(self, name: str, aliases: tuple[str, ...] = ()) -> CommandDefinition:
        handler = _make_handler(name)
        register_command(name=name, description=f"desc for {name}", aliases=aliases)(handler)
        from zulip_bot.commands import get_command

        cmd = get_command(name)
        assert cmd is not None
        return cmd

    def test_get_command_by_canonical_name(self) -> None:
        from zulip_bot.commands import get_command

        self._register("my-cmd", aliases=("my_cmd",))
        cmd = get_command("my-cmd")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd.name, "my-cmd")

    def test_get_command_by_alias_returns_canonical_definition(self) -> None:
        from zulip_bot.commands import get_command

        self._register("my-cmd", aliases=("my_cmd",))
        cmd = get_command("my_cmd")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        # The canonical name is preserved — the alias resolves to the same definition.
        self.assertEqual(cmd.name, "my-cmd")

    def test_alias_and_canonical_are_same_object(self) -> None:
        from zulip_bot.commands import get_command

        self._register("my-cmd", aliases=("my_cmd",))
        self.assertIs(get_command("my-cmd"), get_command("my_cmd"))

    def test_list_commands_deduplicates_aliases(self) -> None:
        from zulip_bot.commands import list_commands

        self._register("my-cmd", aliases=("my_cmd", "myCmd"))
        names = [cmd.name for cmd in list_commands() if cmd.name.startswith("my")]
        self.assertEqual(names, ["my-cmd"])

    def test_list_commands_excludes_alias_names(self) -> None:
        from zulip_bot.commands import list_commands

        self._register("my-cmd", aliases=("my_cmd",))
        all_names = [cmd.name for cmd in list_commands()]
        self.assertNotIn("my_cmd", all_names)

    def test_underscore_declared_command_is_reachable_by_the_parsed_name(self) -> None:
        # Regression: `register_test` was declared with an underscore while the webhook parser
        # hyphenates every incoming name, so `get_command` never found it and the command could not
        # be dispatched in either spelling. Registration now normalizes the name.
        from zulip_bot.commands import get_command
        from zulip_bot.webhook.payload import parse_command

        self._register("under_scored")

        parsed = parse_command("under_scored some args")
        cmd = get_command(parsed.name)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd.name, "under-scored")
        # And the hyphenated spelling a user might type instead resolves to the same definition.
        self.assertIs(get_command("under-scored"), cmd)

    def test_command_without_aliases(self) -> None:
        from zulip_bot.commands import get_command, list_commands

        self._register("no-alias")
        cmd = get_command("no-alias")
        self.assertIsNotNone(cmd)
        assert cmd is not None
        self.assertEqual(cmd.aliases, ())
        self.assertIn("no-alias", [c.name for c in list_commands()])
