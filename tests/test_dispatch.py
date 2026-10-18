"""The slash-command table must stay consistent with the code and the menu."""

import inspect

from telegram_commandcode.bot import BOT_COMMANDS
from telegram_commandcode.commands.router import (
    NA_CMDS,
    TG_TO_CC,
    TUI_ONLY,
    CommandRouter,
)


def dispatch() -> dict[str, str]:
    return CommandRouter()._dispatch


def test_every_dispatch_key_is_a_slash_command():
    assert all(key.startswith("/") for key in dispatch())


def test_every_dispatch_target_exists_on_a_handler():
    router = CommandRouter()
    for key, method_name in dispatch().items():
        owners = [h for h in router.handlers if hasattr(h, method_name)]
        assert owners, f"{key} points at missing method {method_name}"
        assert inspect.iscoroutinefunction(getattr(owners[0], method_name)), (
            f"{key} handler {method_name} is not async"
        )


def test_telegram_menu_matches_dispatch_table():
    menu = {TG_TO_CC.get(name, "/" + name) for name, _ in BOT_COMMANDS}
    assert menu == set(dispatch()), "BOT_COMMANDS and CommandRouter have drifted"


def test_menu_entries_are_unique():
    names = [name for name, _ in BOT_COMMANDS]
    assert len(names) == len(set(names))


def test_menu_entries_have_descriptions():
    assert all(desc.strip() for _, desc in BOT_COMMANDS)


def test_tg_to_cc_targets_are_real_commands():
    known = set(dispatch()) | TUI_ONLY | NA_CMDS
    for source, target in TG_TO_CC.items():
        assert target in known, f"{source} maps to unknown {target}"


def test_tui_only_and_na_commands_are_not_in_the_menu():
    menu = {TG_TO_CC.get(name, "/" + name) for name, _ in BOT_COMMANDS}
    assert not (menu & (TUI_ONLY | NA_CMDS))
