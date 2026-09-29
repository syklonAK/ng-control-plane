"""Tests for update.sh / uninstall.sh discovery and the uninstall command."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pg_router.cli.app import CLI, _find_shipped_script, _global_argv, _shell
from pg_router.utils.system import CommandError


class _Completed:
    """Stand-in for subprocess.CompletedProcess used by the patched run."""

    returncode = 0


@pytest.fixture
def fake_project(tmp_path: Path, monkeypatch) -> Path:
    """Build a tree resembling a venv install of the git clone.

    The package lives deep inside ``venv/lib/.../site-packages`` while the
    scripts sit next to ``pyproject.toml`` at the checkout root — exactly the
    layout that broke the previous fixed-parent lookup.
    """
    root = tmp_path / "checkout"
    package_dir = root / "venv/lib/python3.12/site-packages/pg_router/cli"
    package_dir.mkdir(parents=True)
    (package_dir / "app.py").write_text("# simulated install", encoding="utf-8")
    (root / "pg_router").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'pg-router'\n", encoding="utf-8")
    for name in ("update.sh", "uninstall.sh"):
        (root / name).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr("pg_router.cli.app.__file__", str(package_dir / "app.py"))
    monkeypatch.delenv("PG_ROUTER_HOME", raising=False)
    return root


def test_finds_scripts_from_venv_layout(fake_project: Path):
    assert _find_shipped_script("update.sh") == fake_project / "update.sh"
    assert _find_shipped_script("uninstall.sh") == fake_project / "uninstall.sh"


def test_finds_scripts_from_dev_checkout(tmp_path: Path, monkeypatch):
    root = tmp_path / "dev"
    package_dir = root / "pg_router/cli"
    package_dir.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr("pg_router.cli.app.__file__", str(package_dir / "app.py"))
    monkeypatch.delenv("PG_ROUTER_HOME", raising=False)
    assert _find_shipped_script("update.sh") == root / "update.sh"


def test_missing_script_reports_project_root(tmp_path: Path, monkeypatch):
    root = tmp_path / "checkout"
    package_dir = root / "venv/site-packages/pg_router/cli"
    package_dir.mkdir(parents=True)
    (root / "pg_router").mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("pg_router.cli.app.__file__", str(package_dir / "app.py"))
    monkeypatch.delenv("PG_ROUTER_HOME", raising=False)
    with pytest.raises(CommandError, match="update.sh missing at project root"):
        _find_shipped_script("update.sh")


def test_no_project_root_gives_actionable_error(tmp_path: Path, monkeypatch):
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    monkeypatch.setattr("pg_router.cli.app.__file__", str(orphan / "app.py"))
    monkeypatch.delenv("PG_ROUTER_HOME", raising=False)
    with pytest.raises(CommandError, match="PG_ROUTER_HOME"):
        _find_shipped_script("update.sh")


def test_home_override_wins(fake_project: Path, tmp_path: Path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setenv("PG_ROUTER_HOME", str(other))
    assert _find_shipped_script("update.sh") == other / "update.sh"


def _uninstall_cli(tmp_path: Path, argv: list[str], monkeypatch) -> tuple[CLI, list[list[str]]]:
    runs: list[list[str]] = []
    monkeypatch.setattr("pg_router.cli.app.subprocess.run", lambda cmd, **kw: runs.append(cmd) or None)
    args = _build_args(argv, monkeypatch)
    return CLI(args), runs


def _build_args(argv: list[str], monkeypatch):
    from pg_router.cli.app import _build_parser

    # --yes is a global flag, so it must precede the subcommand.
    global_argv = [token for token in argv if token in ("--yes", "-y")]
    sub_argv = [token for token in argv if token not in ("--yes", "-y")]
    return _build_parser().parse_args(global_argv + ["uninstall"] + sub_argv)


@pytest.mark.parametrize(
    ("argv", "expected_flags"),
    (
        ([], []),
        (["--yes"], ["--yes"]),
        (["--purge"], ["--purge"]),
        (["--yes", "--purge"], ["--yes", "--purge"]),
    ),
)
def test_uninstall_invokes_script_with_flags(tmp_path, monkeypatch, argv, expected_flags):
    root = tmp_path / "checkout"
    (root / "pg_router/cli").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "uninstall.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr("pg_router.cli.app.__file__", str(root / "pg_router/cli/app.py"))
    monkeypatch.delenv("PG_ROUTER_HOME", raising=False)

    runs: list[list[str]] = []
    monkeypatch.setattr(
        "pg_router.cli.app.subprocess.run",
        lambda cmd, **kw: runs.append(cmd) or _Completed(),
    )

    cli = CLI(_build_args(argv, monkeypatch))
    result = cli._uninstall()
    assert result["uninstalled"] is True
    # The script is executed via bash, not by path, so a missing executable bit
    # cannot raise PermissionError (the failure seen on the server). The bash
    # prefix may be one or two argv elements depending on the host, so it is
    # resolved from the helper rather than hard-coded here.
    shell_prefix = _shell()
    expected = [*shell_prefix, str(root / "uninstall.sh")] + expected_flags
    assert runs == [expected]


def test_uninstall_without_script_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_ROUTER_HOME", str(tmp_path / "nonexistent"))
    cli = CLI(_build_args([], monkeypatch))
    with pytest.raises(CommandError, match="not found in PG_ROUTER_HOME"):
        cli._uninstall()


def test_global_argv_roundtrip():
    from argparse import Namespace

    args = Namespace(
        config="/etc/pg-router/config.yaml",
        managed_dir=None,
        log_level="DEBUG",
        json=True,
        yes=True,
    )
    assert _global_argv(args) == [
        "--config",
        "/etc/pg-router/config.yaml",
        "--log-level",
        "DEBUG",
        "--json",
        "--yes",
    ]


# ---------------------------------------------------------------------------
# script execution
# ---------------------------------------------------------------------------


def test_shell_returns_argv_list_not_one_string():
    # "/usr/bin/env bash" is two argv elements; joining them into one string
    # would make subprocess look for a file literally named that.
    prefix = _shell()
    assert isinstance(prefix, list)
    assert all(isinstance(part, str) for part in prefix)
    assert len(prefix) >= 1


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the shipped scripts target Linux servers; no bash on this host",
)
def test_run_script_reports_nonzero_exit(tmp_path):
    """A failing updater must surface an error, not report success."""
    from pg_router.cli.app import _run_script

    script = tmp_path / "fail.sh"
    script.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    code = _run_script(script, ["--yes"])
    assert code == 7


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the shipped scripts target Linux servers; no bash on this host",
)
def test_update_command_raises_on_updater_failure(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "pg_router/cli").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "update.sh").write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    monkeypatch.setattr("pg_router.cli.app.__file__", str(root / "pg_router/cli/app.py"))
    monkeypatch.delenv("PG_ROUTER_HOME", raising=False)

    args = _build_args([], monkeypatch)
    args.command = "update"
    cli = CLI(args)
    with pytest.raises(CommandError, match="exit code 3"):
        cli._update()
