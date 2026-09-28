"""Nginx installation: OS/distro/arch detection and idempotent installs.

Requirements 22 and 37: detect what is missing, install only what is missing,
and remain safe to re-run any number of times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..utils.logging import get_logger
from ..utils.security import validate_filesystem_path
from ..utils.system import CommandError, detect_host, is_root, package_commands, run, which
from . import modules as module_detector
from .manager import NginxManager

_log = get_logger(__name__)

# Package names per distro family, in the order nginx + feature modules.
PACKAGE_NAMES: dict[str, dict[str, tuple[str, ...]]] = {
    "debian": {
        "nginx": ("nginx",),
        "stream": ("libnginx-mod-stream",),
        "stream_ssl": ("libnginx-mod-stream",),
        "stream_ssl_preread": ("libnginx-mod-stream",),
        "http_ssl": ("libnginx-mod-http-ssl",),
        "http_v2": ("nginx",),
        "http_grpc": ("nginx",),
    },
    "rhel": {
        "nginx": ("nginx",),
        "stream": ("nginx-mod-stream",),
        "stream_ssl": ("nginx-mod-stream",),
        "stream_ssl_preread": ("nginx-mod-stream",),
        "http_ssl": ("nginx-mod-http-ssl", "nginx"),
        "http_v2": ("nginx",),
        "http_grpc": ("nginx",),
    },
    "alpine": {
        "nginx": ("nginx",),
        "stream": ("nginx-stream",),
        "stream_ssl": ("nginx-stream",),
        "stream_ssl_preread": ("nginx-stream",),
        "http_ssl": ("nginx",),
        "http_v2": ("nginx",),
        "http_grpc": ("nginx",),
    },
    "arch": {
        "nginx": ("nginx",),
        "stream": ("nginx-mainline",),
        "stream_ssl": ("nginx-mainline",),
        "stream_ssl_preread": ("nginx-mainline",),
        "http_ssl": ("nginx-mainline",),
        "http_v2": ("nginx-mainline",),
        "http_grpc": ("nginx-mainline",),
    },
}

# Directories this application owns. Nothing else is rewritten.
MANAGED_SUBDIRS = ("", "backups", "state")


@dataclass
class InstallReport:
    """What the installer did (useful for CLI/API output)."""

    host: dict = field(default_factory=dict)
    installed_packages: list[str] = field(default_factory=list)
    created_directories: list[str] = field(default_factory=list)
    modules: dict = field(default_factory=dict)
    nginx_version: str = ""
    already_installed: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "host": self.host,
            "installed_packages": self.installed_packages,
            "created_directories": self.created_directories,
            "modules": self.modules,
            "nginx_version": self.nginx_version,
            "already_installed": self.already_installed,
            "notes": self.notes,
        }


class NginxInstaller:
    """Detects the host and provisions nginx with the required modules."""

    def __init__(self, managed_dir: str = "/etc/nginx/pg-router", family: Optional[str] = None) -> None:
        self.managed_dir = str(validate_filesystem_path(managed_dir))
        self.host = detect_host()
        self.family = family or self.host.distro_family
        self.manager = NginxManager()

    # ------------------------------------------------------------------
    def install(self, required_features: Optional[list[str]] = None) -> InstallReport:
        """Install nginx plus any missing required modules (idempotent)."""
        if not is_root():
            raise CommandError("Install requires root privileges (run with sudo)")

        report = InstallReport(host=self.host.__dict__)
        _log.info(
            "Host detected: system=%s distro=%s family=%s version=%s arch=%s",
            self.host.system, self.host.distro, self.host.distro_family,
            self.host.version, self.host.arch,
        )

        if self.host.system != "linux":
            report.notes.append(
                f"Auto-install on {self.host.system} is not supported; "
                "ensure nginx and required modules are installed manually."
            )

        required = list(required_features or ["http", "http_ssl", "http_v2", "stream", "stream_ssl_preread"])
        modules = module_detector.detect()
        if modules.binary:
            report.nginx_version = modules.version
            missing = [feature for feature in required if not modules.supports(feature)]
            if not missing:
                report.already_installed = True
                report.modules = modules.as_dict()["features"]
                _log.info("nginx %s already installed with all required modules", modules.version)
                self.ensure_directories(report)
                return report
            _log.info("nginx present but missing modules: %s", missing)
            if self.host.system != "linux":
                report.notes.append(f"Missing nginx modules: {missing}")
                return report
            self._install_packages_for_features(missing, report)
        else:
            _log.info("nginx not found; installing")
            if self.host.system != "linux":
                report.notes.append("nginx is not installed; install it manually")
                return report
            self._install_packages_for_features(["nginx"] + required, report)

        modules = module_detector.detect()
        report.nginx_version = modules.version
        report.modules = modules.as_dict()["features"]
        still_missing = [feature for feature in required if not modules.supports(feature)]
        if still_missing:
            report.notes.append(
                f"Still missing after install: {still_missing}. The distro may not ship "
                "them; consider nginx-mainline or a custom build."
            )
        else:
            _log.info("All required nginx modules available")

        self.ensure_directories(report)
        return report

    # ------------------------------------------------------------------
    def _install_packages_for_features(self, features: list[str], report: InstallReport) -> None:
        names = self._package_names(features)
        if not names:
            return
        update_cmd, install_prefix = package_commands(self.family)
        if which("apt-get") and self.family == "debian":
            run(["apt-get", "update", "-qq"], check=False, timeout=300)
        else:
            run(update_cmd, check=False, timeout=300)
        args = install_prefix + list(names)
        _log.info("Installing packages: %s", " ".join(names))
        run(args, check=True, timeout=600)
        report.installed_packages.extend(names)

    def _package_names(self, features: list[str]) -> list[str]:
        table = PACKAGE_NAMES.get(self.family)
        if table is None:
            raise CommandError(f"No package mapping for distro family {self.family!r}")
        ordered: list[str] = []
        for feature in features:
            for package in table.get(feature, ()):
                if package not in ordered:
                    ordered.append(package)
        return ordered

    # ------------------------------------------------------------------
    def ensure_directories(self, report: Optional[InstallReport] = None) -> None:
        """Create the managed directory tree (idempotent, owned by this app)."""
        created: list[str] = []
        root = Path(self.managed_dir)
        for subdir in MANAGED_SUBDIRS:
            directory = root / subdir if subdir else root
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(str(directory))
        if created and report is not None:
            report.created_directories.extend(created)
        for directory in created:
            _log.info("Created managed directory %s", directory)
