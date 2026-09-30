"""Configuration file management.

The wizard *creates* configurations; this module *manages* the files
afterwards: show, edit, copy, delete and selecting which one is active. Every
mutating operation validates the result and keeps a backup, so a bad edit can
never silently leave the control plane pointing at a broken file.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from yaml import YAMLError

from ..config.loader import (
    EDIT_BACKUP_SUFFIX,
    forget_config,
    list_config_files,
    remember_config,
    resolve_path,
)
from ..config.validator import ValidationReport, validate_config
from ..utils.logging import get_logger
from ..utils.security import ValidationError, validate_filesystem_path

_log = get_logger(__name__)


@dataclass
class EditOutcome:
    """What happened during a config edit."""

    path: str
    editor: str
    exit_code: int = 0
    kept_backup: Optional[str] = None
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.validation_errors

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "editor": self.editor,
            "exit_code": self.exit_code,
            "backup": self.kept_backup,
            "valid": self.valid,
            "errors": list(self.validation_errors),
            "warnings": list(self.validation_warnings),
        }


class ConfigFileManager:
    """File-level operations on configuration documents."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        runner: Callable[[list[str]], int] | None = None,
    ) -> None:
        self.explicit_path = config_path
        self._runner = runner

    # ------------------------------------------------------------------
    def active_path(self) -> Path:
        """The file the next command without ``-c`` will load."""
        return resolve_path(self.explicit_path)

    def list(self) -> list[dict]:
        # The file selected with -c is included so the menu's listing reflects
        # what is actually being operated on.
        return list_config_files(self.explicit_path)

    # ------------------------------------------------------------------
    def show(self, path: Optional[str] = None) -> dict:
        """Return the raw text of a configuration file."""
        target = self._target(path)
        return {"path": str(target), "text": target.read_text(encoding="utf-8")}

    # ------------------------------------------------------------------
    def edit(
        self,
        path: Optional[str] = None,
        *,
        editor: Optional[str] = None,
        fallback_editors: tuple[str, ...] = ("nano", "vim", "vi"),
    ) -> EditOutcome:
        """Open the configuration in ``$EDITOR`` and validate the result.

        A ``.bak`` copy is kept next to the file before the editor runs, so a
        bad edit is always recoverable. The editor is invoked with an argument
        list, never a shell string.
        """
        target = self._target(path)
        command = self._editor_command(editor, fallback_editors)

        backup = self._backup(target)
        outcome = EditOutcome(path=str(target), editor=command[0])
        if backup is not None:
            outcome.kept_backup = str(backup)

        _log.info("editing %s with %s", target, " ".join(command))
        exit_code = self._run_editor(command, target)
        outcome.exit_code = exit_code
        if exit_code != 0:
            _log.warning("editor exited with code %s; changes may be incomplete", exit_code)

        report = self._validate(target)
        outcome.validation_errors = [str(problem) for problem in report.errors]
        outcome.validation_warnings = [str(problem) for problem in report.warnings]
        if report.valid:
            _log.info("configuration %s is valid after editing", target)
        else:
            _log.error(
                "configuration %s is invalid after editing; the previous version "
                "is preserved at %s",
                target,
                backup,
            )
        return outcome

    # ------------------------------------------------------------------
    def copy(self, source: Optional[str] = None, destination: Optional[str] = None) -> dict:
        """Duplicate a configuration file. Refuses to overwrite an existing file.

        The copy is byte-exact (``shutil.copy2``) so comments and hand
        formatting survive; re-serializing would silently strip them.
        """
        if not destination:
            raise ValidationError("a destination path is required to copy a configuration")
        src = self._target(source)
        dst = Path(validate_filesystem_path(str(destination)))
        if dst.exists():
            raise ValidationError(f"Refusing to overwrite an existing file: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        _log.info("copied %s -> %s", src, dst)
        return {"source": str(src), "destination": str(dst)}

    # ------------------------------------------------------------------
    def delete(self, path: Optional[str] = None, *, yes: bool = False) -> dict:
        """Delete a configuration file, after confirmation.

        Never deletes the file selected through ``-c``/``PG_ROUTER_CONFIG``
        without an explicit ``--yes``: removing the active configuration would
        make every following command fail.
        """
        target = self._target(path)
        if not yes:
            raise ValidationError(
                f"Refusing to delete {target} without confirmation; pass --yes"
            )
        active = self._safe_active()
        if active is not None and active.resolve() == target.resolve():
            forget_config()
        backup = self._backup(target)
        target.unlink()
        _log.info("deleted %s (backup at %s)", target, backup)
        return {"deleted": str(target), "backup": str(backup) if backup else None}

    # ------------------------------------------------------------------
    def use(self, path: str) -> dict:
        """Remember ``path`` as the active configuration."""
        target = Path(validate_filesystem_path(str(path), must_exist=True))
        if not target.is_file():
            raise ValidationError(f"Not a file: {target}")
        try:
            report = self._validate(target)
        except ValidationError as exc:
            # A file that cannot even be parsed/typed is just as unusable as
            # one that fails a rule; report it the same friendly way.
            raise ValidationError(f"Refusing to select an invalid configuration: {exc}") from exc
        if not report.valid:
            raise ValidationError(
                "Refusing to select an invalid configuration: "
                + "; ".join(str(problem) for problem in report.errors)
            )
        remember_config(target)
        _log.info("%s is now the active configuration", target)
        return {"active": str(target)}

    # ------------------------------------------------------------------
    def forget(self) -> dict:
        """Stop remembering an active configuration."""
        cleared = forget_config()
        return {"cleared": cleared}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _target(self, path: Optional[str]) -> Path:
        candidate = Path(validate_filesystem_path(str(path))) if path else self.active_path()
        if not candidate.is_file():
            raise ValidationError(f"Configuration file does not exist: {candidate}")
        return candidate

    def _safe_active(self) -> Optional[Path]:
        try:
            return self.active_path()
        except ValidationError:
            return None

    @staticmethod
    def _backup(target: Path) -> Optional[Path]:
        backup = target.with_suffix(target.suffix + EDIT_BACKUP_SUFFIX)
        shutil.copy2(target, backup)
        return backup

    @staticmethod
    def _validate(target: Path) -> ValidationReport:
        """Validate a file, degrading to a report instead of raising.

        An editor can leave a document that cannot even be parsed; that must
        surface as ``valid is False`` with the reason recorded, never as an
        obscure traceback mid-command.
        """
        from ..config.loader import load_config

        try:
            config = load_config(target)
        except (ValidationError, ValueError, TypeError, YAMLError) as exc:
            report = ValidationReport()
            report.error(str(target), f"Could not load configuration: {exc}")
            return report
        return validate_config(config, log=False)

    def _editor_command(
        self, editor: Optional[str], fallback_editors: tuple[str, ...]
    ) -> list[str]:
        import os
        import shlex

        from ..utils.system import which

        choice = editor or os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if not choice:
            for candidate in fallback_editors:
                resolved = which(candidate)
                if resolved:
                    choice = resolved
                    break
        if not choice:
            raise ValidationError(
                "No editor available: set $EDITOR (e.g. 'export EDITOR=nano') "
                "or install one of: " + ", ".join(fallback_editors)
            )
        # An editor may carry arguments ("code --wait"); the chosen binary is
        # resolved on PATH and the whole command stays an argv list.
        parts = shlex.split(choice)
        if len(parts) > 1:
            return parts
        resolved = which(parts[0])
        return [resolved or parts[0]]

    def _run_editor(self, command: list[str], target: Path) -> int:
        argv = [*command, str(target)]
        if self._runner is not None:
            return self._runner(argv)
        import subprocess

        return subprocess.run(argv, check=False, shell=False).returncode


__all__ = ["ConfigFileManager", "EditOutcome"]
