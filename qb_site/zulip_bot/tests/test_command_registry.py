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


class TestRegisteredCommandsAreDispatchable(TestCase):
    """Every registered command must survive the round trip a real message takes.

    The webhook does exactly two things between reading a message and calling a handler:
    `parse_command` (which normalizes the typed name) and `get_command`. So "is this command
    reachable at all" is precisely "does its registered name survive that round trip" — the property
    `register_test` violated for its whole life, since it registered an underscore that the parser
    always hyphenated. A per-command test cannot catch that class of bug for the *next* command; this
    one iterates the live registry, so a new command with an unreachable name fails immediately.

    Policy is deliberately out of scope: commands are deny-by-default per deployment
    (`ZULIP_COMMAND_POLICY`), which is configuration, not reachability.
    """

    def test_every_registered_name_and_alias_resolves_after_parsing(self) -> None:
        import zulip_bot.views  # noqa: F401  -- imports every command module for its side effects
        from zulip_bot.commands import get_command, list_commands
        from zulip_bot.webhook.payload import parse_command

        definitions = list_commands()
        names = {definition.name for definition in definitions}
        # Guard against a vacuous pass if the command modules ever stop being imported here. These
        # are long-standing names, deliberately not the command this test was written for — the loop
        # below is what must catch an unreachable name, not this sentinel.
        self.assertLessEqual({"help", "prefs", "console"}, names)

        for definition in definitions:
            for spelling in (definition.name, *definition.aliases):
                with self.subTest(command=definition.name, spelling=spelling):
                    parsed = parse_command(spelling)
                    self.assertIsNotNone(parsed)
                    assert parsed is not None
                    self.assertIs(
                        get_command(parsed.name),
                        definition,
                        f"{spelling!r} parses to {parsed.name!r}, which does not resolve to this command",
                    )

    def test_every_registered_name_is_already_canonical(self) -> None:
        """`help` and the unknown-command reply list these names, so they must be typeable as shown."""
        import zulip_bot.views  # noqa: F401
        from zulip_bot.commands import list_commands, normalize_command_name

        for definition in list_commands():
            with self.subTest(command=definition.name):
                self.assertEqual(definition.name, normalize_command_name(definition.name))
