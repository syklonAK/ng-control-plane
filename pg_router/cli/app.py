"""Command line interface.

The CLI is intentionally thin: every command delegates to a service. This
keeps it usable from automation, and lets a future REST API or Telegram bot
reuse exactly the same code paths (requirement 30).

Output is human-friendly by default and machine-readable with ``--json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .. import __version__
from ..plugins.providers import register_defaults
from ..services import (
    ConfigService,
    DeployService,
    HealthService,
    NginxService,
    RouterService,
    TunnelService,
)
from ..utils.logging import configure, get_logger
from ..utils.security import ValidationError
from ..utils.system import CommandError, run

_log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-router",
        description="Generic Nginx + PasarGuard traffic orchestration control plane",
    )
    parser.add_argument("--version", action="version", version=f"pg-router {__version__}")
    parser.add_argument("--config", "-c", help="Configuration file (or PG_ROUTER_CONFIG)")
    parser.add_argument("--managed-dir", help="Managed nginx fragment directory")
    parser.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--yes", "-y", action="store_true", help="Answer confirmations yes")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install", help="Install nginx with required modules").add_argument(
        "--features", nargs="*", help="Required nginx features (default: full set)"
    )
    init = sub.add_parser("init", help="Create a starter configuration")
    init.add_argument("--path", help="Where to write the configuration")
    init.add_argument("--interactive", "-i", action="store_true", help="Prompt for values")

    sub.add_parser("validate", help="Validate the configuration")
    gen = sub.add_parser("generate", help="Generate nginx fragments")
    gen.add_argument("--dry-run", action="store_true", help="Validate without touching nginx")
    gen.add_argument("--output-dir", help="Write fragments to this directory for inspection")

    app = sub.add_parser("apply", help="Validate, generate, test and deploy atomically")
    app.add_argument("--dry-run", action="store_true", help="Everything except live deployment")
    app.add_argument("--check-health", action="store_true", help="Run health checks first")

    rb = sub.add_parser("rollback", help="Restore the previous known-good configuration")
    rb.add_argument("snapshot", nargs="?", help="Snapshot name (default: latest)")

    sub.add_parser("status", help="Overall status: nginx, objects, snapshots")

    routes = sub.add_parser("routes", help="Route management")
    routes_sub = routes.add_subparsers(dest="subcommand", required=True)
    routes_sub.add_parser("list", help="List routes")
    test = routes_sub.add_parser("test", help="Simulate a request against configured routes")
    test.add_argument("--host")
    test.add_argument("--path")
    test.add_argument("--sni")
    test.add_argument("--alpn")
    test.add_argument("--protocol")
    test.add_argument("--transport")
    test.add_argument("--source-ip")
    test.add_argument("--port", type=int)

    backends = sub.add_parser("backends", help="Backend management")
    backends.add_subparsers(dest="subcommand", required=True).add_parser("list", help="List backends")

    tunnels = sub.add_parser("tunnels", help="Tunnel management")
    tunnels_sub = tunnels.add_subparsers(dest="subcommand", required=True)
    tunnels_sub.add_parser("list", help="List tunnels")
    tunnels_sub.add_parser("status", help="Probe tunnels")

    nginx = sub.add_parser("nginx", help="Nginx operations")
    nginx_sub = nginx.add_subparsers(dest="subcommand", required=True)
    nginx_sub.add_parser("test", help="nginx -t")
    nginx_sub.add_parser("reload", help="Reload nginx")
    nginx_sub.add_parser("restart", help="Restart nginx")
    nginx_sub.add_parser("modules", help="Show detected modules")
    nginx_sub.add_parser("status", help="Service status")

    health = sub.add_parser("health", help="Health checks and failover")
    health_sub = health.add_subparsers(dest="subcommand", required=True)
    health_sub.add_parser("check", help="Probe all targets")
    health_sub.add_parser("failover", help="Apply health state to failover groups")

    sub.add_parser("update", help="Update pg-router from git and reinstall")
    sub.add_parser("uninstall", help="Remove the CLI, virtualenv and (with --purge) fragments").add_argument(
        "--purge", action="store_true", help="Also delete the configuration and nginx fragments"
    )
    sub.add_parser("menu", help="Interactive terminal menu")

    return parser


class CLI:
    """Command dispatcher returning structured results."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.managed_dir = args.managed_dir
        self.config_service = ConfigService(args.config, self.managed_dir)

    # ------------------------------------------------------------------
    def run(self) -> int:
        handlers = {
            "install": self._install,
            "init": self._init,
            "validate": self._validate,
            "generate": self._generate,
            "apply": self._apply,
            "rollback": self._rollback,
            "status": self._status,
            "routes": self._routes,
            "backends": self._backends,
            "tunnels": self._tunnels,
            "nginx": self._nginx,
            "health": self._health,
            "update": self._update,
            "uninstall": self._uninstall,
            "menu": self._menu,
        }
        handler = handlers.get(self.args.command)
        if handler is None:
            _log.error("Unknown command %s", self.args.command)
            return 2
        try:
            result = handler()
            self._emit(result)
            return self._exit_code(result)
        except ValidationError as exc:
            _log.error("%s", exc)
            return 1
        except CommandError as exc:
            _log.error("%s", exc)
            return 3
        except KeyboardInterrupt:
            _log.warning("Interrupted")
            return 130

    # ------------------------------------------------------------------
    def _exit_code(self, result) -> int:
        """Non-zero exit for report-style commands that found problems.

        Automation relies on ``pg-router validate`` failing loudly when the
        configuration is invalid.
        """
        if self.args.command == "validate" and isinstance(result, dict):
            return 0 if result.get("valid") else 1
        if self.args.command == "generate" and isinstance(result, dict):
            return 0 if not result.get("errors") else 1
        return 0

    # ------------------------------------------------------------------
    def _emit(self, result) -> None:
        if result is None:
            return
        if self.args.json:
            print(json.dumps(_jsonable(result), indent=2, default=str))
        else:
            print(_human(result))

    # ------------------------------------------------------------------
    def _install(self):
        return NginxService(self.managed_dir).install(self.args.features).as_dict()

    def _init(self):
        path = self.config_service.init_config(
            path=self.args.path, interactive=self.args.interactive
        )
        return {"created": str(path)}

    def _validate(self):
        report = self.config_service.validate()
        return {
            "valid": report.valid,
            "errors": [str(problem) for problem in report.errors],
            "warnings": [str(problem) for problem in report.warnings],
        }

    def _generate(self):
        service = DeployService(self.config_service.load(), self.managed_dir)
        result = service.generate(dry_run=self.args.dry_run)
        payload = result.as_dict()
        payload["fragments"] = result.fragments if not self.args.output_dir else None
        if self.args.output_dir:
            from pathlib import Path

            directory = Path(self.args.output_dir)
            directory.mkdir(parents=True, exist_ok=True)
            written = {}
            for name, content in result.fragments.items():
                (directory / name).write_text(content, encoding="utf-8")
                written[name] = str(directory / name)
            payload["written"] = written
        payload["dry_run"] = self.args.dry_run
        return payload

    def _apply(self):
        service = DeployService(self.config_service.load(), self.managed_dir)
        result = service.apply(dry_run=self.args.dry_run, check_health=self.args.check_health)
        payload = result.as_dict()
        return payload

    def _rollback(self):
        service = DeployService(self.config_service.load(), self.managed_dir)
        return service.rollback(self.args.snapshot).as_dict()

    def _status(self):
        service = DeployService(self.config_service.load(), self.managed_dir)
        status = service.status()
        status["snapshots"] = service.history()
        status["validation"] = {
            "valid": self.config_service.validate().valid,
        }
        return status

    # ------------------------------------------------------------------
    def _routes(self):
        service = RouterService(self.config_service.load())
        if self.args.subcommand == "list":
            return service.list_routes()
        request = {
            key: getattr(self.args, key.replace("-", "_"))
            for key in ("host", "path", "sni", "alpn", "protocol", "transport", "source_ip", "port")
            if getattr(self.args, key.replace("-", "_"), None) is not None
        }
        return {"request": request, "matches": service.test_request(request)}

    def _backends(self):
        return RouterService(self.config_service.load()).list_backends()

    def _tunnels(self):
        service = TunnelService(self.config_service.load())
        if self.args.subcommand == "status":
            return service.status()
        return service.list_tunnels()

    def _nginx(self):
        service = NginxService(self.managed_dir)
        if self.args.subcommand == "test":
            ok, output = service.test()
            return {"ok": ok, "output": output}
        if self.args.subcommand == "reload":
            service.reload()
            return {"reloaded": True}
        if self.args.subcommand == "restart":
            service.restart()
            return {"restarted": True}
        if self.args.subcommand == "modules":
            return service.detect()
        return service.status()

    def _health(self):
        service = HealthService(self.config_service.load())
        if self.args.subcommand == "failover":
            return service.failover()
        return service.check()

    def _update(self):
        # Delegate to the shipped update.sh; it performs the git pull and
        # reinstall. Invoked as an argv list so no shell interpolation of
        # configuration values can occur. Output is streamed because the
        # updater is interactive and prints its own [INFO] progress lines.
        script = _find_shipped_script("update.sh")
        _log.info("running updater: %s", script)
        subprocess.run([str(script)], check=False, shell=False)
        return {"updated": True, "script": str(script)}

    def _uninstall(self):
        # Same pattern as _update: run the shipped uninstaller as an argv
        # list. The confirmation prompts live in the script so the logic is
        # not duplicated between shell and Python entry points.
        script = _find_shipped_script("uninstall.sh")
        argv = [str(script)]
        if self.args.yes:
            argv.append("--yes")
        if self.args.purge:
            argv.append("--purge")
        _log.info("running uninstaller: %s", " ".join(argv))
        subprocess.run(argv, check=False, shell=False)
        return {"uninstalled": True, "script": str(script)}

    def _menu(self):
        from .menu import run_menu

        run_menu(_global_argv(self.args))
        return None


def _find_shipped_script(name: str) -> Path:
    """Locate a script shipped at the project root.

    ``pip install`` of a git clone nests the package deep inside the venv
    (``<root>/venv/lib/python3.x/site-packages/pg_router/cli/``), so walking a
    fixed number of parents up from ``__file__`` is not enough. Instead the
    tree is searched upward for the directory that *owns* the project — the one
    containing ``pyproject.toml`` next to a ``pg_router`` package — and the
    script is read from there. ``PG_ROUTER_HOME`` overrides everything.
    """
    override = os.environ.get("PG_ROUTER_HOME")
    if override:
        candidate = Path(override) / name
        if candidate.is_file():
            return candidate
        raise CommandError(f"{name} not found in PG_ROUTER_HOME ({override})")

    here = Path(__file__).resolve()
    directory = here.parent
    for _ in range(12):  # bounded: never escapes past the filesystem root
        if (directory / "pyproject.toml").is_file() and (directory / "pg_router").is_dir():
            candidate = directory / name
            if not candidate.is_file():
                raise CommandError(f"{name} missing at project root ({directory})")
            return candidate
        parent = directory.parent
        if parent == directory:
            break
        directory = parent

    raise CommandError(
        f"{name} not found: could not locate the project root from {here} "
        "(set PG_ROUTER_HOME to the installation directory)"
    )


def _global_argv(args: argparse.Namespace) -> list[str]:
    """Rebuild the global flag list so the menu re-enters the CLI identically."""
    out: list[str] = []
    if args.config:
        out += ["--config", args.config]
    if args.managed_dir:
        out += ["--managed-dir", args.managed_dir]
    if args.log_level:
        out += ["--log-level", args.log_level]
    if args.json:
        out += ["--json"]
    if args.yes:
        out += ["--yes"]
    return out


# ---------------------------------------------------------------------------
# output formatting
# ---------------------------------------------------------------------------


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _human(value, indent: int = 0) -> str:
    """Render dicts/lists as aligned key/value text for terminal use."""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_human(item, indent + 1))
            else:
                rendered = _human(item, indent)
                lines.append(f"{pad}{key}: {rendered}" if rendered else f"{pad}{key}:")
        return "\n".join(lines)
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(_human(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
        return "\n".join(lines)
    return "" if value is None else str(value)


def _enter_menu(argv: Optional[list[str]] = None) -> int:
    """Run the interactive menu (used when no subcommand is given).

    The subparser is marked optional on a throwaway parser, because a missing
    subcommand is the *expected* input here — it is supplied interactively.
    """
    register_defaults()
    parser = _build_parser()
    parser._subparsers._group_actions[0].required = False
    args, _ = parser.parse_known_args(argv)
    configure(args.log_level)
    from .menu import run_menu

    return run_menu(_global_argv(args))


def _is_interactive() -> bool:
    import os
    import sys

    if os.environ.get("PG_ROUTER_MENU"):
        return True
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


_VALUE_FLAGS = ("--config", "-c", "--managed-dir", "--log-level")


def _has_command(argv: Optional[list[str]]) -> bool:
    """True when argv names a subcommand (or asks for help/version).

    ``None`` means argparse will read ``sys.argv`` itself, so that is what we
    inspect here.
    """
    import sys

    tokens = list(sys.argv[1:]) if argv is None else [str(token) for token in argv]
    if not tokens:
        return False
    if any(token in ("-h", "--help", "--version") for token in tokens):
        return True
    parser = _build_parser()
    known = set(parser._subparsers._group_actions[0].choices.keys())
    expect_value = False
    for token in tokens:
        if expect_value:
            expect_value = False
            continue
        if token in _VALUE_FLAGS:
            expect_value = True
            continue
        if token in known:
            return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    register_defaults()
    if not _has_command(argv):
        # Without a subcommand there is nothing to run non-interactively, so
        # drop into the menu on a real terminal, or usage on a pipe/cron.
        if _is_interactive():
            return _enter_menu(argv)
        _build_parser().print_help()
        return 2
    args = _build_parser().parse_args(argv)
    configure(args.log_level)
    if not args.json and args.command != "menu":
        _log.info("pg-router %s — command: %s", __version__, args.command)
    return CLI(args).run()


if __name__ == "__main__":
    sys.exit(main())
