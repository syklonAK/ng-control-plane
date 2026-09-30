"""Runs the shell test suites for install.sh.

The installer's provisioning logic (interpreter selection, EOL archive
re-pointing, the deadsnakes PPA, the source-build fallback) is bash, so it is
tested in bash against a sandboxed fake system rather than re-implemented in
Python.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SHELL_DIR = Path(__file__).parent / "shell"
INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

ALL_SCENARIOS = (
    "bionic-ppa",
    "bionic-key",
    "focal",
    "debian-src",
    "rocky",
    "alpine",
    "unsupported",
    "nginx",
    "nginx-skip",
    "nginx-fails",
)


def _find_bash() -> str | None:
    candidates = [shutil.which("bash")]
    if sys.platform.startswith("win"):
        # git-bash is not necessarily on PATH.
        candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash available to run the installer tests")


def _run(script: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    # The harness deletes its work tree, so it cannot be the process cwd on
    # Windows (the directory would stay locked and the removal would fail).
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    # Forward slashes keep the paths usable inside git-bash on Windows.
    return subprocess.run(
        [BASH, SHELL_DIR.joinpath(script).as_posix(), *args],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )


def test_installer_helpers(tmp_path: Path):
    result = _run("installer_units.sh", tmp_path, INSTALL_SH.as_posix())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PASSED" in result.stdout


def test_installer_ref_pinning(tmp_path: Path):
    """The installer must clone/update to exactly a pinned ref when given one,
    and refuse rather than fall back to the branch tip when the ref is bad —
    a host must never silently move to unreviewed code."""
    result = _run("installer_refpin.sh", tmp_path, INSTALL_SH.as_posix())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL PASSED" in result.stdout


def test_installer_scenarios(tmp_path: Path):
    result = _run(
        "installer_scenarios.sh",
        tmp_path,
        INSTALL_SH.as_posix(),
        (tmp_path / "work").as_posix(),
        *ALL_SCENARIOS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL SCENARIOS PASSED" in result.stdout


@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_installer_scenario(scenario: str, tmp_path: Path):
    # Each scenario also passes on its own, so a failure names the one that
    # broke instead of hiding behind the whole-suite output.
    result = _run(
        "installer_scenarios.sh",
        tmp_path,
        INSTALL_SH.as_posix(),
        (tmp_path / "work").as_posix(),
        scenario,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
