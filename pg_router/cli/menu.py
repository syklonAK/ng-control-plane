"""Interactive terminal menu.

Every menu entry re-uses the exact CLI code paths that automation relies on,
so the friendly interface can never drift from the scripted one. Reached with
``pg-router menu``, or by running ``pg-router`` without a subcommand on an
interactive terminal.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import __version__
from ..utils.logging import get_logger
from .app import CLI, _build_parser

_log = get_logger(__name__)


class MenuExit(Exception):
    """Raised to leave the menu loop cleanly (EOF, Ctrl-C)."""


@dataclass
class Item:
    """One menu entry.

    Exactly one of ``argv``, ``submenu`` or ``back`` applies:

    * ``argv``    — run the CLI with the global flags plus these tokens
    * ``submenu`` — descend into a nested menu
    * ``back``    — leave the current menu (quit when already at the top)
    """

    key: str
    label: str
    argv: list[str] = field(default_factory=list)
    submenu: Optional[list["Item"]] = None
    confirm: bool = False
    prompt: Optional[Callable[["Menu"], list[str]]] = None
    back: bool = False


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def _prompt_request(menu: "Menu") -> list[str]:
    argv: list[str] = []
    for flag, label in (
        ("--host", "Host"),
        ("--path", "Path"),
        ("--sni", "SNI"),
        ("--alpn", "ALPN"),
        ("--protocol", "Protocol"),
        ("--transport", "Transport"),
        ("--source-ip", "Source IP"),
        ("--port", "Port"),
    ):
        value = menu.ask(f"{label} (blank to skip)")
        if value:
            argv += [flag, value]
    return argv


def _prompt_snapshot(menu: "Menu") -> list[str]:
    snapshot = menu.ask("Snapshot name (blank for the latest)")
    return ["rollback", snapshot] if snapshot else ["rollback"]


def _prompt_output_dir(menu: "Menu") -> list[str]:
    directory = menu.ask("Output directory", "generated")
    return ["--output-dir", directory]


def _prompt_init(menu: "Menu") -> list[str]:
    path = menu.ask("Configuration path", "pg-router.yaml")
    return ["init", "--path", path]


# ---------------------------------------------------------------------------
# menus
# ---------------------------------------------------------------------------

TUNNEL_MENU = [
    Item("1", "List configured tunnels", ["tunnels", "list"]),
    Item("2", "Probe tunnels", ["tunnels", "status"]),
    Item("0", "Back to main menu", back=True),
]

NGINX_MENU = [
    Item("1", "Test configuration (nginx -t)", ["nginx", "test"]),
    Item("2", "Reload nginx", ["nginx", "reload"], confirm=True),
    Item("3", "Restart nginx", ["nginx", "restart"], confirm=True),
    Item("4", "Show detected modules", ["nginx", "modules"]),
    Item("5", "Service status", ["nginx", "status"]),
    Item("0", "Back to main menu", back=True),
]

HEALTH_MENU = [
    Item("1", "Probe all targets", ["health", "check"]),
    Item("2", "Apply failover state", ["health", "failover"], confirm=True),
    Item("0", "Back to main menu", back=True),
]

MAIN_MENU = [
    Item("1", "Status — nginx, objects, snapshots", ["status"]),
    Item("2", "Validate configuration", ["validate"]),
    Item("3", "Generate fragments (dry run)", ["generate", "--dry-run"]),
    Item("4", "Generate fragments to a directory", ["generate", "--dry-run"], prompt=_prompt_output_dir),
    Item("5", "Apply — validate, generate, test, deploy", ["apply"], confirm=True),
    Item("6", "Rollback to a previous snapshot", ["rollback"], prompt=_prompt_snapshot, confirm=True),
    Item("7", "Routes — list", ["routes", "list"]),
    Item("8", "Routes — simulate a request", ["routes", "test"], prompt=_prompt_request),
    Item("9", "Backends — list", ["backends", "list"]),
    Item("t", "Tunnels", submenu=TUNNEL_MENU),
    Item("n", "Nginx", submenu=NGINX_MENU),
    Item("h", "Health checks and failover", submenu=HEALTH_MENU),
    Item("i", "Initialise a starter configuration", ["init"], prompt=_prompt_init),
    Item("u", "Update pg-router from git", ["update"], confirm=True),
    Item("0", "Quit", back=True),
]


# ---------------------------------------------------------------------------
# menu loop
# ---------------------------------------------------------------------------


class Menu:
    def __init__(
        self,
        base_argv: list[str],
        *,
        prompt: Callable[[str], str] = input,
        write: Callable[..., None] = print,
    ) -> None:
        self.base_argv = [str(token) for token in base_argv]
        self._prompt = prompt
        self._write = write
        self.stack: list[list[Item]] = [MAIN_MENU]

    @property
    def assumed_yes(self) -> bool:
        return any(flag in self.base_argv for flag in ("--yes", "-y"))

    # ------------------------------------------------------------------
    def loop(self) -> int:
        try:
            while self.stack:
                items = self.stack[-1]
                self.render(items)
                choice = self.ask("Choice")
                item = _resolve(items, choice)
                if item is None:
                    self._write(f"Unknown choice '{choice}'. Pick one of the keys listed above.")
                    continue
                if item.back:
                    if len(self.stack) > 1:
                        self.stack.pop()
                    else:
                        return 0
                    continue
                if item.submenu is not None:
                    self.stack.append(item.submenu)
                    continue
                argv = list(self.base_argv) + list(item.argv)
                if item.prompt is not None:
                    argv += item.prompt(self)
                if item.confirm and not self.confirm(item.label):
                    self._write("Cancelled.")
                    continue
                self.execute(argv)
        except MenuExit:
            return 0
        return 0

    # ------------------------------------------------------------------
    def ask(self, label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            text = self._prompt(f"{label}{hint}> ")
        except (EOFError, KeyboardInterrupt, StopIteration):
            raise MenuExit
        return text.strip() or default

    def confirm(self, action: str) -> bool:
        if self.assumed_yes:
            return True
        return self.ask(f"Proceed with '{action}'? (y/N)", "no").lower() in ("y", "yes")

    def execute(self, argv: list[str]) -> int:
        argv = [str(token) for token in argv]
        self._write("")
        self._write(f"$ pg-router {shlex.join(argv)}")
        try:
            args = _build_parser().parse_args(argv)
        except SystemExit as exc:
            self._write(f"Command line rejected (exit {exc.code}); nothing was run.")
            self.pause()
            return 2
        code = CLI(args).run()
        if code:
            self._write(f"(exit code {code})")
        self.pause()
        return code

    def pause(self) -> None:
        self.ask("Press Enter to continue")

    def render(self, items: list[Item]) -> None:
        self._write("")
        self._write(f"pg-router {__version__} — interactive menu")
        config = _flag_value(self.base_argv, "--config", "-c")
        if config:
            self._write(f"config: {config}")
        managed = _flag_value(self.base_argv, "--managed-dir")
        if managed:
            self._write(f"managed dir: {managed}")
        self._write("")
        width = max(len(item.key) for item in items)
        for item in items:
            marker = " *" if item.confirm else "  "
            self._write(f"{marker}{item.key:>{width}}) {item.label}")
        self._write("")


def _resolve(items: list[Item], choice: str) -> Optional[Item]:
    for item in items:
        if item.key.lower() == choice.lower():
            return item
    return None


def _flag_value(argv: list[str], *flags: str) -> Optional[str]:
    """Read the value of a global flag from a raw argv list."""
    for index, token in enumerate(argv):
        token = str(token)
        for flag in flags:
            if token == flag and index + 1 < len(argv):
                return str(argv[index + 1])
            if token.startswith(f"{flag}="):
                return token[len(flag) + 1 :]
    return None


def run_menu(base_argv: list[str], **kwargs) -> int:
    """Run the interactive menu. ``base_argv`` are the global flags only."""
    return Menu(base_argv, **kwargs).loop()
