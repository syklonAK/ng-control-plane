"""System helpers: safe command execution and host introspection.

No command anywhere in this project is built by string concatenation with
user data. Every subprocess call uses an argument list with shell=False.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from ..utils.logging import get_logger
from ..utils.security import is_secret_key

_log = get_logger(__name__)


class CommandError(RuntimeError):
    """Raised when a subprocess command fails."""


@dataclass(frozen=True)
class CommandResult:
    """Result of a subprocess run."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    args: list[str],
    *,
    check: bool = True,
    timeout: int = 60,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
    cwd: Optional[str] = None,
) -> CommandResult:
    """Run a command with an argument list. Never a shell string.

    ``args`` is passed verbatim to subprocess; no shell interpretation, so
    shell injection is structurally impossible.
    """
    _log.debug("exec %s", " ".join(_redact_arg(a) for a in args))
    try:
        proc = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            input=input_text,
            cwd=cwd,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"Command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"Command timed out after {timeout}s: {args[0]}") from exc

    result = CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not result.ok:
        raise CommandError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip()}"
        )
    return result


def _redact_arg(arg: str) -> str:
    if is_secret_key(arg):
        return "***"
    return arg


def which(binary: str) -> Optional[str]:
    """Locate a binary on PATH."""
    return shutil.which(binary)


def is_root() -> bool:
    """True if the process runs with privileges (root on POSIX)."""
    if platform.system() == "Windows":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


@dataclass(frozen=True)
class HostInfo:
    """Detected host facts used by the installer."""

    system: str  # linux / windows / darwin
    distro: str  # ubuntu / debian / centos / fedora / arch / ...
    distro_family: str  # debian / rhel / arch / alpine / unknown
    version: str
    arch: str  # x86_64 / aarch64 / ...
    kernel: str


def detect_host() -> HostInfo:
    """Detect OS, distribution, version and architecture."""
    system = platform.system().lower()
    arch = platform.machine().lower() or "unknown"
    kernel = platform.release()

    if system != "linux":
        return HostInfo(system, "unknown", "unknown", platform.version(), arch, kernel)

    distro, family, version = "unknown", "unknown", ""
    info: dict[str, str] = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        if os.path.exists(path):
            info = _parse_os_release(path)
            break
    if info:
        distro = info.get("ID", "unknown").strip().lower()
        version = info.get("VERSION_ID", "")
        family = _distro_family(distro, info.get("ID_LIKE", ""))
    return HostInfo(system, distro, family, version, arch, kernel)


def _parse_os_release(path: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def _distro_family(distro: str, like: str) -> str:
    likes = like.lower()
    if distro in ("ubuntu", "debian") or "debian" in likes:
        return "debian"
    if distro in ("centos", "rhel", "rocky", "almalinux", "fedora") or "rhel" in likes:
        return "rhel"
    if distro == "alpine" or "alpine" in likes:
        return "alpine"
    if distro == "arch" or "arch" in likes:
        return "arch"
    return "unknown"


def package_commands(family: str) -> tuple[list[str], list[str]]:
    """Return (update_cmd, install_cmd prefix) for a distro family."""
    if family == "debian":
        return (["apt-get", "update", "-qq"], ["apt-get", "install", "-y", "-qq"])
    if family == "rhel":
        return (["dnf", "makecache"], ["dnf", "install", "-y"])
    if family == "alpine":
        return (["apk", "update", "-q"], ["apk", "add", "--quiet"])
    if family == "arch":
        return (["pacman", "-Sy", "--noconfirm"], ["pacman", "-S", "--noconfirm"])
    raise CommandError(f"Unsupported distro family for package install: {family}")
