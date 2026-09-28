"""Nginx process manager: test, reload, restart, start, stop.

Never constructs shell strings; systemctl or the nginx binary is invoked with
an argument vector only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..utils.logging import get_logger
from ..utils.system import CommandError, is_root, run, which
from . import modules as module_detector

_log = get_logger(__name__)

DEFAULT_PREFIX = "/etc/nginx"
DEFAULT_MAIN_CONF = "/etc/nginx/nginx.conf"


@dataclass
class NginxStatus:
    """Snapshot of the Nginx service state."""

    installed: bool = False
    running: bool = False
    enabled: bool = False
    version: str = ""
    binary: str = ""
    features: dict[str, bool] = None  # type: ignore[assignment]
    last_test_ok: Optional[bool] = None

    def as_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "running": self.running,
            "enabled": self.enabled,
            "version": self.version,
            "binary": self.binary,
            "features": dict(self.features or {}),
            "last_test_ok": self.last_test_ok,
        }


class NginxManager:
    """Wraps nginx service operations with privilege awareness."""

    def __init__(
        self,
        binary: Optional[str] = None,
        main_conf: str = DEFAULT_MAIN_CONF,
        prefix: str = DEFAULT_PREFIX,
        use_systemd: Optional[bool] = None,
    ) -> None:
        self.binary = binary or module_detector.find_nginx() or "nginx"
        self.main_conf = main_conf
        self.prefix = prefix
        self._modules: Optional[module_detector.NginxModules] = None
        self.use_systemd = use_systemd if use_systemd is not None else self._systemd_available()

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    @property
    def modules(self) -> module_detector.NginxModules:
        if self._modules is None:
            self._modules = module_detector.detect(self.binary)
        return self._modules

    def require_modules(self, *features: str) -> None:
        self.modules.require(*features)

    @staticmethod
    def _systemd_available() -> bool:
        if not which("systemctl"):
            return False
        try:
            run(["systemctl", "is-system-running"], check=False, timeout=10)
        except CommandError:
            return False
        return True

    def status(self) -> NginxStatus:
        status = NginxStatus(binary=self.binary)
        if which(self.binary) or os.path.exists(self.binary):
            status.installed = True
            modules = self.modules
            status.version = modules.version
            status.features = dict(modules.features)
        if self.use_systemd:
            active = run(["systemctl", "is-active", "--quiet", "nginx"], check=False, timeout=15)
            status.running = active.ok
            enabled = run(["systemctl", "is-enabled", "--quiet", "nginx"], check=False, timeout=15)
            status.enabled = enabled.ok
        else:
            pid = self._read_pid()
            status.running = pid is not None
            status.enabled = False
        return status

    def _read_pid(self) -> Optional[int]:
        pid_file = Path(self.prefix) / "run" / "nginx.pid"
        if not pid_file.exists():
            pid_file = Path(self.prefix) / "nginx.pid"
        if not pid_file.exists():
            return None
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------
    def test(self, main_conf: Optional[str] = None, prefix: Optional[str] = None) -> tuple[bool, str]:
        """Run ``nginx -t``; returns (ok, output)."""
        args = [self.binary, "-t"]
        if prefix:
            args += ["-p", prefix]
        if main_conf:
            args += ["-c", main_conf]
        result = run(args, check=False, timeout=60)
        output = (result.stdout + result.stderr).strip()
        if result.ok:
            _log.info("nginx -t successful")
        else:
            _log.error("nginx -t failed: %s", output)
        return result.ok, output

    def reload(self) -> None:
        """Reload Nginx without dropping connections."""
        if self.use_systemd:
            run(["systemctl", "reload", "nginx"], check=True, timeout=60)
        else:
            run([self.binary, "-s", "reload"], check=True, timeout=60)
        _log.info("Nginx reloaded successfully")

    def restart(self) -> None:
        if self.use_systemd:
            run(["systemctl", "restart", "nginx"], check=True, timeout=90)
        else:
            self.stop()
            self.start()
        _log.info("Nginx restarted")

    def start(self) -> None:
        if self.use_systemd:
            run(["systemctl", "start", "nginx"], check=True, timeout=90)
        else:
            run([self.binary], check=True, timeout=90)
        _log.info("Nginx started")

    def stop(self) -> None:
        if self.use_systemd:
            run(["systemctl", "stop", "nginx"], check=True, timeout=90)
        else:
            run([self.binary, "-s", "stop"], check=False, timeout=90)

    def enable(self) -> None:
        if self.use_systemd:
            run(["systemctl", "enable", "nginx"], check=True, timeout=30)

    def ensure_privileges(self) -> None:
        """Control-plane operations need root on POSIX."""
        if not is_root():
            raise CommandError("This operation requires root privileges (run with sudo)")

    # ------------------------------------------------------------------
    # config hygiene
    # ------------------------------------------------------------------
    def validate_main_conf(self) -> bool:
        ok, _ = self.test()
        return ok

    @staticmethod
    def read_main_conf() -> str:
        path = Path(DEFAULT_MAIN_CONF)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def ensure_managed_include(
        self,
        managed_dir: str,
        fragments: tuple[str, ...],
        dry_run: bool = False,
    ) -> bool:
        """Make nginx.conf include the managed fragments, idempotently.

        Preserves everything else; a ``.bak`` copy is written before any edit
        and the edit is verified with ``nginx -t`` (rolled back on failure).
        Returns True if the file was (or would be) modified.
        """
        text = self.read_main_conf()
        if not text:
            _log.warning("%s not found or empty; skipping include wiring", DEFAULT_MAIN_CONF)
            return False

        modified = text
        changed = False
        http_include = f"    include {managed_dir}/http.conf;"
        stream_include = f"    include {managed_dir}/stream.conf;"
        top_load_lines = "\n".join(
            f"load_module {path};"
            for path in self.modules.load_modules
            if not self._load_module_present(text, path)
        )

        for block, include_line in (("http", http_include), ("stream", stream_include)):
            if include_line in modified:
                continue
            new_text = self._insert_into_block(modified, block, include_line)
            if new_text != modified:
                modified = new_text
                changed = True
            else:
                _log.warning(
                    "Could not locate a `%s {` block in %s; add `%s` manually",
                    block,
                    DEFAULT_MAIN_CONF,
                    include_line.strip(),
                )

        if top_load_lines and top_load_lines not in modified:
            modified = f"{top_load_lines}\n{modified}"
            changed = True

        if not changed or dry_run:
            return changed

        backup = f"{DEFAULT_MAIN_CONF}.bak"
        Path(DEFAULT_MAIN_CONF).rename(backup)
        try:
            Path(DEFAULT_MAIN_CONF).write_text(modified, encoding="utf-8")
        except OSError:
            Path(backup).rename(DEFAULT_MAIN_CONF)
            raise
        ok, output = self.test()
        if not ok:
            _log.error("new nginx.conf failed validation; restoring backup")
            Path(DEFAULT_MAIN_CONF).rename(f"{DEFAULT_MAIN_CONF}.failed")
            Path(backup).rename(DEFAULT_MAIN_CONF)
            raise CommandError(f"Refused to modify {DEFAULT_MAIN_CONF}: nginx -t failed: {output}")
        _log.info("Wired managed includes into %s (backup at %s)", DEFAULT_MAIN_CONF, backup)
        return True

    @staticmethod
    def _load_module_present(text: str, module_path: str) -> bool:
        return module_path in text

    @staticmethod
    def _insert_into_block(text: str, block: str, line: str) -> str:
        """Insert ``line`` right after the opening ``block {`` marker."""
        pattern = re.compile(rf"(^|\n)([ \t]*){block}[ \t]*\{{", re.MULTILINE)
        match = pattern.search(text)
        if not match:
            return text
        insert_at = match.end()
        return text[:insert_at] + f"\n{line}" + text[insert_at:]
