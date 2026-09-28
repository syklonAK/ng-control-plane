"""Tests for the interactive menu.

The menu is driven with a scripted ``prompt`` function so the loop can be
exercised without a terminal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pg_router.cli.menu import Menu, MenuExit, _flag_value


def script_menu(base_argv: list[str], answers: list[str]):
    """Run the menu with canned answers, returning exit code and output lines."""
    remaining = iter(answers)
    lines: list[str] = []

    def prompt(_question: str) -> str:
        try:
            return next(remaining)
        except StopIteration as exc:
            raise MenuExit from exc

    code = Menu(base_argv, prompt=prompt, write=lines.append).loop()
    return code, lines


EXAMPLE = str(Path(__file__).resolve().parent.parent / "examples" / "edge-mixed.yaml")


def test_menu_runs_validate_and_quits(capsys):
    code, lines = script_menu(["--config", EXAMPLE], ["2", "", "0"])
    assert code == 0
    joined = "\n".join(lines)
    assert "interactive menu" in joined
    assert "$ pg-router --config" in joined
    # Command output goes to real stdout, exactly as it would in a terminal.
    assert "valid: True" in capsys.readouterr().out


def test_menu_reports_invalid_choice_and_recovers(capsys):
    code, lines = script_menu(["--config", EXAMPLE], ["zzz", "2", "", "0"])
    assert code == 0
    assert any("Unknown choice 'zzz'" in line for line in lines)
    assert "valid: True" in capsys.readouterr().out


def test_menu_dangerous_action_requires_confirmation():
    code, lines = script_menu(["--config", EXAMPLE], ["5", "n", "", "0"])
    assert code == 0
    joined = "\n".join(lines)
    assert "Cancelled." in joined
    # The destructive command line must never have been executed.
    assert "$ pg-router" not in joined.split("Cancelled.")[0]


def test_menu_yes_flag_skips_confirmation(monkeypatch):
    monkeypatch.setattr("pg_router.cli.menu.Menu.confirm", lambda self, action: True)
    code, lines = script_menu(["--config", EXAMPLE, "--yes"], ["5", "", "0"])
    assert code == 0
    joined = "\n".join(lines)
    assert "Cancelled." not in joined
    assert "exit code" in joined or "$ pg-router" in joined


def test_menu_submenus_round_trip():
    code, lines = script_menu(["--config", EXAMPLE], ["t", "1", "", "0", "0"])
    assert code == 0
    joined = "\n".join(lines)
    assert "List configured tunnels" in joined
    assert "$ pg-router --config" in joined


def test_menu_routes_test_prompt_collects_fields(capsys):
    code, lines = script_menu(
        ["--config", EXAMPLE, "--json"], ["8", "example.com", "/nl/10000", "", "", "", "", "", "", "", "0"]
    )
    assert code == 0
    assert "$ pg-router --config" in "\n".join(lines)
    payload = json.loads(capsys.readouterr().out)
    assert {match["route"] for match in payload["matches"]} == {"nl-ws"}


def test_menu_eof_exits_cleanly():
    remaining = iter(["2"])

    def prompt(_question: str) -> str:
        return next(remaining)

    code = Menu(["--config", EXAMPLE], prompt=prompt, write=lambda *_: None).loop()
    # The pause prompt after validate hits StopIteration -> MenuExit -> 0.
    assert code == 0


@pytest.mark.parametrize(
    ("argv", "expected"),
    (
        (["--config", "a.yaml"], "a.yaml"),
        (["-c", "b.yaml"], "b.yaml"),
        (["--config=c.yaml"], "c.yaml"),
        (["--managed-dir", "/tmp/x"], None),
        (["--config", "--managed-dir"], "--managed-dir"),
        ([], None),
    ),
)
def test_flag_value_parsing(argv, expected):
    assert _flag_value(argv, "--config", "-c") == expected


def test_cli_menu_subcommand_runs_interactively(monkeypatch):
    """``pg-router menu`` enters the same loop as the no-argument entry point."""
    from pg_router.cli import app as cli_app

    monkeypatch.setenv("PG_ROUTER_MENU", "1")
    monkeypatch.setattr("pg_router.cli.menu.run_menu", lambda base: 0)
    code = cli_app.main(["--config", EXAMPLE, "menu"])
    assert code == 0


def test_cli_without_command_is_interactive_when_a_tty(monkeypatch):
    from pg_router.cli import app as cli_app

    monkeypatch.setenv("PG_ROUTER_MENU", "1")
    monkeypatch.setattr("pg_router.cli.menu.run_menu", lambda base: 42)
    assert cli_app.main([]) == 42
    assert cli_app.main(["--config", EXAMPLE]) == 42


def test_cli_without_command_shows_usage_when_not_a_tty(monkeypatch, capsys):
    from pg_router.cli import app as cli_app

    monkeypatch.delenv("PG_ROUTER_MENU", raising=False)
    monkeypatch.setattr("sys.stdin", object())  # no isatty -> not interactive
    code = cli_app.main([])
    assert code == 2
    assert "usage:" in capsys.readouterr().out
