"""Deployment tests: dry-run, atomic apply, staging failure and rollback.

A fake NginxManager stands in for the real binary so the full pipeline is
exercised without root privileges or an installed nginx.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.deploy.deployer import DeploymentError, Deployer
from pg_router.nginx.manager import NginxManager, NginxStatus
from pg_router.plugins.providers import register_builtin_providers
from pg_router.plugins.registry import ProviderRegistry, certificates as certificate_registry
from pg_router.utils.system import CommandError

CONFIG = """
version: 1
node:
  id: test-01
  roles: [edge]
certificates:
  - {id: c1, provider: existing, chain: /certs/fullchain.pem, key: /certs/privkey.pem}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {mode: terminate, certificate: c1}
backends:
  - {id: local, type: local, host: 127.0.0.1, port: 62050}
routes:
  - id: ws
    listener: https
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: local
"""


class FakeNginxManager(NginxManager):
    """NginxManager with injected test outcomes."""

    def __init__(self, test_ok: bool = True, reload_ok: bool = True) -> None:
        super().__init__(binary="nginx")
        self.test_ok = test_ok
        self.reload_ok = reload_ok
        self.reloads = 0
        self.tests = 0

    def status(self) -> NginxStatus:
        return NginxStatus(installed=True, running=True, version="1.24.0 (fake)")

    def test(self, main_conf=None, prefix=None):
        self.tests += 1
        if self.test_ok:
            return True, "nginx: configuration file test is successful"
        return False, "nginx: [emerg] unknown directive \"bogus\""

    def reload(self) -> None:
        self.reloads += 1
        if not self.reload_ok:
            raise CommandError("reload failed")

    def ensure_privileges(self) -> None:
        pass  # tests do not run as root

    @property
    def modules(self):
        from pg_router.nginx.modules import NginxModules

        return NginxModules(
            binary="nginx",
            version="1.24.0",
            features={feature: True for feature in (
                "http", "http_ssl", "http_v2", "http_grpc", "http_websocket",
                "stream", "stream_ssl", "stream_ssl_preread",
            )},
        )


@pytest.fixture
def managed_dir(tmp_path: Path) -> str:
    directory = tmp_path / "pg-router"
    directory.mkdir()
    return str(directory)


@pytest.fixture(autouse=True)
def _providers():
    """Isolated provider registries so tests do not depend on import order."""
    register_builtin_providers(ProviderRegistry(), certificate_registry)


def make_deployer(managed_dir: str, manager: FakeNginxManager) -> Deployer:
    return Deployer(parse_config_string(CONFIG), managed_dir, manager)


def test_dry_run_writes_nothing_and_reports_success(managed_dir):
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    result = deployer.apply(dry_run=True)

    assert result.dry_run is True
    assert result.applied is False
    assert manager.reloads == 0
    assert not list(Path(managed_dir).glob("*.conf"))
    assert result.staged_test_ok is True


def test_apply_writes_fragments_and_reloads(managed_dir):
    manager = FakeNginxManager()
    result = make_deployer(managed_dir, manager).apply()

    assert result.applied is True
    assert result.rolled_back is False
    assert manager.reloads == 1
    for name in ("maps.conf", "upstreams.conf", "http.conf", "stream.conf"):
        assert (Path(managed_dir) / name).is_file()
    state_file = Path(managed_dir) / "state" / "last-deploy.json"
    assert state_file.is_file()
    assert '"routes": 1' in state_file.read_text()


def test_apply_is_idempotent(managed_dir):
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    first = deployer.apply()
    first_fragments = {name: (Path(managed_dir) / name).read_text() for name in
                       ("maps.conf", "upstreams.conf", "http.conf", "stream.conf")}
    second = deployer.apply()
    second_fragments = {name: (Path(managed_dir) / name).read_text() for name in
                        ("maps.conf", "upstreams.conf", "http.conf", "stream.conf")}

    assert first.applied and second.applied
    assert first_fragments == second_fragments
    assert manager.tests == 4  # staging + live, twice


def test_failed_live_test_rolls_back(managed_dir):
    manager = FakeNginxManager(test_ok=False)
    deployer = make_deployer(managed_dir, manager)

    with pytest.raises(DeploymentError) as excinfo:
        deployer.apply()

    assert excinfo.value.result.rolled_back is True
    assert excinfo.value.result.applied is False
    assert manager.reloads == 0
    # Backup snapshot must exist for the rollback record.
    snapshots = list((Path(managed_dir) / "backups").glob("*"))
    assert len(snapshots) >= 1


def test_failed_reload_rolls_back(managed_dir):
    manager = FakeNginxManager(reload_ok=False)
    deployer = make_deployer(managed_dir, manager)

    with pytest.raises(DeploymentError) as excinfo:
        deployer.apply()

    assert excinfo.value.result.rolled_back is True
    assert excinfo.value.result.reloaded is False


def test_rollback_restores_previous_fragments(managed_dir):
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    # First deployment creates the fragments; a second deployment snapshots
    # them, so rollback has a known-good state to restore.
    deployer.apply()
    deployer.apply()

    http_path = Path(managed_dir) / "http.conf"
    original = http_path.read_text()
    http_path.write_text("# tampered\n", encoding="utf-8")

    result = deployer.rollback()
    assert result.rolled_back is True
    assert http_path.read_text() == original


def test_history_lists_snapshots(managed_dir):
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()
    deployer.apply()
    history = deployer.history()

    assert len(history) == 2
    # The first apply of a fresh directory has nothing to back up; the second
    # snapshot captured the first deployment's four fragments.
    assert history[0].fragments == []
    assert len(history[1].fragments) == 4


def test_invalid_configuration_is_rejected_before_writing(managed_dir):
    broken = CONFIG.replace("backend: local", "backend: nonexistent")
    manager = FakeNginxManager()
    deployer = Deployer(parse_config_string(broken), managed_dir, manager)

    with pytest.raises(DeploymentError):
        deployer.apply()

    assert not list(Path(managed_dir).glob("*.conf"))
    assert manager.tests == 0
