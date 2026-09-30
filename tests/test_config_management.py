"""Tests for configuration file management (show / edit / copy / delete / use)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pg_router.cli.app import CLI, _build_parser
from pg_router.config.loader import (
    ACTIVE_CONFIG_FILE,
    forget_config,
    remembered_config_path,
)
from pg_router.config.management import ConfigFileManager
from pg_router.utils.security import ValidationError

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "edge-mixed.yaml"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.pg-router state."""
    state = tmp_path / "state"
    monkeypatch.setattr("pg_router.config.loader.ACTIVE_CONFIG_FILE", state / "active_config")
    monkeypatch.delenv("PG_ROUTER_CONFIG", raising=False)
    forget_config()
    yield state
    forget_config()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    target = tmp_path / "pg-router.yaml"
    target.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _cli(argv: list[str]) -> CLI:
    return CLI(_build_parser().parse_args(argv))


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_list_marks_the_active_file(config_file: Path, monkeypatch):
    monkeypatch.chdir(config_file.parent)
    ConfigFileManager(str(config_file)).use(str(config_file))
    entries = ConfigFileManager(None).list()
    active = [entry for entry in entries if entry["active"]]
    assert len(active) == 1
    assert Path(active[0]["path"]).resolve() == config_file.resolve()


def test_list_reports_missing_files(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entries = ConfigFileManager(str(tmp_path / "nope.yaml")).list()
    assert entries
    assert not any(entry["exists"] for entry in entries)


def test_use_remembers_and_resolves(config_file: Path):
    ConfigFileManager(None).use(str(config_file))
    assert remembered_config_path() == config_file.resolve()
    # A manager with no explicit path now resolves to the remembered file.
    assert ConfigFileManager(None).active_path() == config_file.resolve()


def test_use_refuses_invalid_config(tmp_path: Path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\nlisteners:\n  - id: x\n    port: not-a-port\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid configuration"):
        ConfigFileManager(None).use(str(broken))


def test_forget_clears_the_choice(config_file: Path):
    ConfigFileManager(None).use(str(config_file))
    assert ConfigFileManager(None).forget()["cleared"] is True
    assert remembered_config_path() is None


def test_forget_when_nothing_remembered():
    assert ConfigFileManager(None).forget()["cleared"] is False


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_returns_the_file_text(config_file: Path):
    result = ConfigFileManager(None).show(str(config_file))
    assert result["path"] == str(config_file)
    assert "listeners:" in result["text"]


def test_show_missing_file_errors(tmp_path: Path):
    with pytest.raises(ValidationError, match="does not exist"):
        ConfigFileManager(None).show(str(tmp_path / "nope.yaml"))


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def test_edit_validates_and_keeps_backup(config_file: Path, monkeypatch):
    monkeypatch.setenv("EDITOR", "stub-editor")
    seen: list[list[str]] = []

    def runner(argv):
        seen.append(argv)
        # Simulate an editor that leaves the file untouched: still valid.
        return 0

    manager = ConfigFileManager(None, runner=runner)
    outcome = manager.edit(str(config_file))
    assert outcome.valid is True
    assert outcome.exit_code == 0
    assert outcome.kept_backup == str(config_file.with_suffix(".yaml.bak"))
    assert Path(outcome.kept_backup).is_file()
    # The editor receives the file as a separate argv element, never via shell.
    assert seen == [["stub-editor", str(config_file)]]


def test_edit_reports_invalid_result_but_preserves_the_backup(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EDITOR", "stub-editor")
    broken = tmp_path / "broken.yaml"
    broken.write_text("version: 1\n", encoding="utf-8")

    def runner(argv):
        # Wreck the file the way a bad manual edit would.
        broken.write_text("version: 1\nlisteners: not-a-list\n", encoding="utf-8")
        return 0

    manager = ConfigFileManager(None, runner=runner)
    outcome = manager.edit(str(broken))
    assert outcome.valid is False
    assert outcome.validation_errors
    # The pre-edit content survives in the backup.
    assert Path(outcome.kept_backup).read_text(encoding="utf-8") == "version: 1\n"


def test_edit_uses_editor_from_environment(config_file: Path, monkeypatch):
    monkeypatch.setenv("EDITOR", "my-editor --wait")
    manager = ConfigFileManager(None, runner=lambda argv: 0)
    outcome = manager.edit(str(config_file))
    # EDITOR may carry arguments; they stay argv elements, unsplit by a shell.
    assert outcome.editor == "my-editor"
    assert outcome.valid is True


def test_edit_without_any_editor_errors(config_file: Path, monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    manager = ConfigFileManager(None, runner=lambda command, env: 0)
    manager._editor_command = lambda editor, fallbacks: (_ for _ in ()).throw(
        ValidationError("no editor")
    )
    with pytest.raises(ValidationError, match="no editor"):
        manager.edit(str(config_file))


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def test_copy_writes_a_new_file(config_file: Path, tmp_path: Path):
    destination = tmp_path / "copy.yaml"
    result = ConfigFileManager(None).copy(str(config_file), str(destination))
    assert result["destination"] == str(destination)
    assert destination.is_file()
    assert destination.read_text(encoding="utf-8") == config_file.read_text(encoding="utf-8")


def test_copy_refuses_to_overwrite(config_file: Path):
    with pytest.raises(ValidationError, match="Refusing to overwrite"):
        ConfigFileManager(None).copy(str(config_file), str(config_file))


def test_copy_requires_a_destination(config_file: Path):
    with pytest.raises(ValidationError, match="destination path is required"):
        ConfigFileManager(None).copy(str(config_file))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_requires_confirmation(config_file: Path):
    with pytest.raises(ValidationError, match="without confirmation"):
        ConfigFileManager(None).delete(str(config_file))


def test_delete_removes_the_file_and_keeps_a_backup(config_file: Path):
    result = ConfigFileManager(None).delete(str(config_file), yes=True)
    assert result["deleted"] == str(config_file)
    assert not config_file.exists()
    assert Path(result["backup"]).is_file()


def test_delete_clears_the_active_choice(config_file: Path):
    ConfigFileManager(None).use(str(config_file))
    assert remembered_config_path() is not None
    ConfigFileManager(None).delete(str(config_file), yes=True)
    assert remembered_config_path() is None


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _run(argv: list[str], capsys) -> dict:
    """Invoke the CLI and return the JSON document it emits.

    ``CLI.run`` returns an exit code and prints the result, so the structured
    payload is read back off stdout instead.
    """
    exit_code = _cli(["--json", *argv]).run()
    assert exit_code == 0, f"command failed: {argv}"
    return json.loads(capsys.readouterr().out)


def test_cli_config_list(config_file: Path, monkeypatch, capsys):
    monkeypatch.chdir(config_file.parent)
    result = _run(["config", "list"], capsys)
    assert result["files"]


def test_cli_config_show(config_file: Path, capsys):
    result = _run(["config", "show", str(config_file)], capsys)
    assert "listeners:" in result["text"]


def test_cli_config_use_and_forget(config_file: Path, capsys):
    result = _run(["config", "use", str(config_file)], capsys)
    assert result["active"] == str(config_file)
    assert remembered_config_path() == config_file.resolve()
    result = _run(["config", "forget"], capsys)
    assert result["cleared"] is True


def test_cli_config_edit_fails_loudly_on_invalid_result(config_file: Path, monkeypatch):
    monkeypatch.setenv("EDITOR", "stub-editor")
    monkeypatch.setattr(
        ConfigFileManager,
        "_run_editor",
        lambda self, command, target: Path(target).write_text(
            "version: 1\nlisteners: nope\n", encoding="utf-8"
        )
        or 0,
    )
    cli = _cli(["config", "edit", str(config_file)])
    # Validation problems make the command fail loudly for automation.
    assert cli.run() == 1
    # The original content is recoverable.
    assert config_file.with_suffix(".yaml.bak").is_file()


def test_cli_config_edit_succeeds_when_valid(config_file: Path, monkeypatch, capsys):
    monkeypatch.setenv("EDITOR", "stub-editor")
    monkeypatch.setattr(
        ConfigFileManager, "_run_editor", lambda self, command, target: 0
    )
    result = _run(["config", "edit", str(config_file)], capsys)
    assert result["valid"] is True


def test_cli_config_edit_skips_validation_on_request(config_file: Path, monkeypatch, capsys):
    monkeypatch.setenv("EDITOR", "stub-editor")
    monkeypatch.setattr(
        ConfigFileManager,
        "_run_editor",
        lambda self, command, target: Path(target).write_text(
            "version: 1\nlisteners: nope\n", encoding="utf-8"
        )
        or 0,
    )
    result = _run(["config", "edit", "--no-validate", str(config_file)], capsys)
    assert result["valid"] is False


def test_cli_config_copy(config_file: Path, tmp_path: Path, capsys):
    destination = tmp_path / "via-cli.yaml"
    result = _run(["config", "copy", str(config_file), str(destination)], capsys)
    assert result["destination"] == str(destination)
    assert destination.is_file()


def test_cli_config_delete_refuses_without_yes(config_file: Path):
    assert _cli(["config", "delete", str(config_file)]).run() == 1
    assert config_file.is_file()


def test_cli_config_delete_with_yes(config_file: Path, capsys):
    result = _run(["--yes", "config", "delete", str(config_file)], capsys)
    assert result["deleted"] == str(config_file)
    assert not config_file.exists()
