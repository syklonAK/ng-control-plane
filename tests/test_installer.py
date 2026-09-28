"""Installer tests: package mapping, directory idempotency, host detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from pg_router.nginx.installer import NginxInstaller
from pg_router.utils.system import HostInfo, detect_host, package_commands


def test_package_commands_per_family():
    update, install = package_commands("debian")
    assert update[:1] == ["apt-get"]
    assert install[:2] == ["apt-get", "install"]

    update, install = package_commands("rhel")
    assert install[:2] == ["dnf", "install"]

    update, install = package_commands("alpine")
    assert install[:1] == ["apk"]

    update, install = package_commands("arch")
    assert install[:1] == ["pacman"]


def test_package_commands_rejects_unknown_family():
    with pytest.raises(Exception):
        package_commands("gentoo")


def test_host_detection_returns_expected_fields():
    host = detect_host()
    assert isinstance(host, HostInfo)
    assert host.system in ("linux", "windows", "darwin")
    assert host.arch


def test_package_name_mapping_covers_stream_modules():
    installer = NginxInstaller(family="debian")
    names = installer._package_names(["nginx", "stream_ssl_preread"])
    assert "nginx" in names
    assert "libnginx-mod-stream" in names

    installer = NginxInstaller(family="rhel")
    names = installer._package_names(["stream"])
    assert "nginx-mod-stream" in names


def test_ensure_directories_is_idempotent(tmp_path: Path):
    managed = str(tmp_path / "pg-router")
    installer = NginxInstaller(managed_dir=managed)
    installer.ensure_directories()
    installer.ensure_directories()
    assert Path(managed).is_dir()
    assert (Path(managed) / "backups").is_dir()
    assert (Path(managed) / "state").is_dir()
    # Root itself is not a child entry: only backups and state remain, and
    # repeated creation must not add or remove anything.
    assert sorted(item.name for item in Path(managed).iterdir()) == ["backups", "state"]


def test_installer_records_host_info(tmp_path: Path):
    installer = NginxInstaller(managed_dir=str(tmp_path / "pg-router"))
    assert installer.host.system in ("linux", "windows", "darwin")
    assert installer.family in ("debian", "rhel", "alpine", "arch", "unknown")
