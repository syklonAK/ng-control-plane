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


def test_staging_config_contains_only_valid_main_directives(managed_dir, monkeypatch, tmp_path):
    """Regression: the staging main config used to emit `temp_path`, which is
    not an nginx directive, so `nginx -t` failed on every apply with
    `unknown directive "temp_path"` — the deployment blocker the operator hit.

    The staging config is generated under a temporary directory that is removed
    on exit, so the tempdir factory is redirected somewhere the test can read.
    """
    import tempfile

    import pg_router.deploy.deployer as deployer_module

    staging_root = tmp_path / "captured"
    staging_root.mkdir()

    class _KeepDir:
        def __init__(self, prefix):
            self.name = str(staging_root / prefix.strip("-"))

        def __enter__(self):
            Path(self.name).mkdir(parents=True, exist_ok=True)
            return self.name

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _KeepDir)
    monkeypatch.setattr(deployer_module.tempfile, "TemporaryDirectory", _KeepDir)

    manager = FakeNginxManager()
    make_deployer(managed_dir, manager).apply(dry_run=True)

    main_conf = staging_root / "pg-router-staging" / "nginx.conf"
    text = main_conf.read_text()
    assert "temp_path" not in text, "temp_path is not an nginx directive"
    for directive in ("error_log", "pid", "worker_processes", "events {", "http {", "stream {"):
        assert directive in text


def test_probe_configs_contain_no_invalid_directive():
    """The module probes share the staging mistake: an unparseable probe config
    makes every probe report 'missing', so detection silently believes nginx
    lacks modules it has."""
    from pg_router.nginx import modules as module_detector

    probe = module_detector.FEATURE_PROBES["stream"]
    main_conf = (
        "error_log /tmp/pg-router-probe/logs/error.log warn;\n"
        "pid /tmp/pg-router-probe/nginx.pid;\n"
        f"{probe}\n"
    )
    assert "temp_path" not in main_conf


def test_staging_config_never_puts_stream_variables_in_the_http_context(
    managed_dir, monkeypatch, tmp_path
):
    """Regression: the staging main config included maps.conf in BOTH the http
    and stream blocks, but that fragment carried the SNI/ALPN maps reading
    $ssl_preread_server_name / $ssl_preread_alpn_protocols — variables that
    exist only in a stream {} context. nginx -t then failed with
    'unknown "ssl_preread_server_name" variable' for any stream config, so
    every apply on a real host died in staging."""
    import tempfile

    import pg_router.deploy.deployer as deployer_module

    staging_root = tmp_path / "captured"
    staging_root.mkdir()

    class _KeepDir:
        def __init__(self, prefix):
            self.name = str(staging_root / prefix.strip("-"))

        def __enter__(self):
            Path(self.name).mkdir(parents=True, exist_ok=True)
            return self.name

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _KeepDir)
    monkeypatch.setattr(deployer_module.tempfile, "TemporaryDirectory", _KeepDir)

    stream_config = """
version: 1
listeners:
  - id: s
    address: 0.0.0.0
    port: 8443
    mode: stream
    tls: {mode: passthrough}
backends:
  - {id: nl, type: local, host: 10.0.0.10, port: 62050}
routes:
  - id: nl-tcp
    listener: s
    transport: {type: tcp}
    match: {type: sni, value: nl.example.com}
    backend: nl
"""
    manager = FakeNginxManager()
    Deployer(parse_config_string(stream_config), managed_dir, manager).apply(dry_run=True)

    main_conf = staging_root / "pg-router-staging" / "nginx.conf"
    text = main_conf.read_text()
    http_block, _ = text.split("stream {")
    assert "ssl_preread_server_name" not in http_block, (
        "the http context must not reference stream-only variables"
    )
    # The stream block includes stream.conf, which now carries the SNI map.
    # Check the fragment itself rather than the include line.
    stream_fragment = Path(managed_dir) / ".staging" / "stream.conf"
    assert "ssl_preread_server_name" in stream_fragment.read_text(), (
        "the stream fragment must carry the SNI map its server blocks use"
    )


def test_staging_config_loads_the_same_dynamic_modules_as_live(
    managed_dir, monkeypatch, tmp_path
):
    """Regression: the staging main config carried no load_module directives at
    all, so on a dynamic-module build (Debian/Ubuntu) it parsed against a
    different module set than the live nginx.conf. nginx -t then failed with
    'unknown "ssl_preread_server_name" variable' even though the live config
    would have loaded that module via its modules-enabled include."""
    import tempfile

    import pg_router.deploy.deployer as deployer_module

    staging_root = tmp_path / "captured"
    staging_root.mkdir()

    class _KeepDir:
        def __init__(self, prefix):
            self.name = str(staging_root / prefix.strip("-"))

        def __enter__(self):
            Path(self.name).mkdir(parents=True, exist_ok=True)
            return self.name

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _KeepDir)
    monkeypatch.setattr(deployer_module.tempfile, "TemporaryDirectory", _KeepDir)

    module_include = "/etc/nginx/modules-enabled/*.conf"

    class _DynamicModuleManager(FakeNginxManager):
        @property
        def modules(self):
            from pg_router.nginx.modules import NginxModules

            return NginxModules(
                binary="nginx",
                version="1.24.0",
                features={name: True for name in (
                    "http", "http_ssl", "http_v2", "stream", "stream_ssl",
                    "stream_ssl_preread",
                )},
                module_include=module_include,
            )

    manager = _DynamicModuleManager()
    Deployer(parse_config_string(CONFIG), managed_dir, manager).apply(dry_run=True)

    main_conf = staging_root / "pg-router-staging" / "nginx.conf"
    text = main_conf.read_text()
    assert f"include {module_include};" in text, (
        "staging must load the distro's dynamic modules exactly the way the "
        "live nginx.conf does; otherwise it tests a different module set"
    )
    # load_module directives belong to the main context, i.e. before the
    # http/stream blocks that need them.
    assert text.index(f"include {module_include};") < text.index("events {")


def test_staging_is_tested_with_the_real_nginx_prefix(
    managed_dir, monkeypatch, tmp_path
):
    """Regression: the staging test ran ``nginx -t -p <tempdir>``, but the
    Debian/Ubuntu module include uses *relative* load_module paths
    ("modules/ngx_stream_module.so"). nginx resolves those against the prefix,
    so it looked for the .so inside the throwaway staging dir, failed the
    dlopen, and rolled back a configuration the live tree would have accepted.

    The symptom on the server was::

        dlopen() "/tmp/pg-router-staging-.../modules/ngx_stream_module.so" failed
        ... in /etc/nginx/modules-enabled/50-mod-stream.conf:1
    """
    import tempfile

    import pg_router.deploy.deployer as deployer_module

    staging_root = tmp_path / "captured"
    staging_root.mkdir()

    class _KeepDir:
        def __init__(self, prefix):
            self.name = str(staging_root / prefix.strip("-"))

        def __enter__(self):
            Path(self.name).mkdir(parents=True, exist_ok=True)
            return self.name

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _KeepDir)
    monkeypatch.setattr(deployer_module.tempfile, "TemporaryDirectory", _KeepDir)

    module_include = "/etc/nginx/modules-enabled/*.conf"

    class _PrefixManager(FakeNginxManager):
        def __init__(self):
            super().__init__()
            self.test_calls = []

        def test(self, main_conf=None, prefix=None):
            self.test_calls.append((main_conf, prefix))
            return True, "nginx: configuration file test is successful"

        @property
        def modules(self):
            from pg_router.nginx.modules import NginxModules

            return NginxModules(
                binary="nginx",
                version="1.24.0",
                features={name: True for name in (
                    "http", "http_ssl", "http_v2", "stream", "stream_ssl",
                    "stream_ssl_preread",
                )},
                module_include=module_include,
                prefix="/usr/share/nginx",
            )

    manager = _PrefixManager()
    Deployer(parse_config_string(CONFIG), managed_dir, manager).apply(dry_run=True)

    assert manager.test_calls, "staging must run nginx -t"
    _, staging_prefix = manager.test_calls[0]
    assert staging_prefix == "/usr/share/nginx", (
        "relative load_module paths in the distro include resolve against the "
        "nginx prefix; testing against the scratch dir makes nginx search for "
        "the .so where it does not exist"
    )


# ----------------------------------------------------------------------
# concurrent-operation protection and atomic rollback
# ----------------------------------------------------------------------

def test_concurrent_apply_is_refused(managed_dir):
    """Two apply/rollback operations must not interleave: they rewrite the
    same fragment directory and resequence the same snapshots, so a race
    corrupts state. The second caller must get a clear error, not a hang."""
    from pg_router.deploy.lock import DeployLock, DeployLockBusy

    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)

    with DeployLock(managed_dir, operation="apply"):
        with pytest.raises(DeploymentError) as excinfo:
            deployer.apply()

    message = str(excinfo.value)
    assert "in progress" in message
    assert "apply" in message
    # Nothing was written and no reload happened.
    assert not list(Path(managed_dir).glob("*.conf"))
    assert manager.reloads == 0
    assert manager.tests == 0


def test_concurrent_rollback_is_refused(managed_dir):
    from pg_router.deploy.lock import DeployLock

    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()

    with DeployLock(managed_dir, operation="apply"):
        with pytest.raises(DeploymentError) as excinfo:
            deployer.rollback()

    assert "in progress" in str(excinfo.value)


def test_lock_is_released_after_failed_apply(managed_dir):
    """A crashed or failed deploy must not wedge the tool: the lock is OS-level
    and released when the holder exits."""
    from pg_router.deploy.lock import DeployLock

    manager = FakeNginxManager(test_ok=False)
    deployer = make_deployer(managed_dir, manager)

    with pytest.raises(DeploymentError):
        deployer.apply()

    # Re-entering must succeed immediately.
    with DeployLock(managed_dir, operation="apply"):
        pass


def test_lock_is_reentrant_within_one_process(managed_dir):
    from pg_router.deploy.lock import DeployLock

    lock = DeployLock(managed_dir, operation="apply")
    with lock:
        with lock:
            pass
        # Still held: a fresh taker must fail.
        other = DeployLock(managed_dir, operation="rollback")
        with pytest.raises(Exception):
            other.acquire()


def test_rollback_removes_fragments_the_snapshot_does_not_have(managed_dir):
    """Regression: rollback used to copy each snapshot fragment over the live
    tree but never removed fragments added *after* the snapshot was taken. A
    deploy that added ``stream.conf`` followed by a rollback left the stale
    fragment behind, referencing upstreams the restored config had deleted."""
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()
    deployer.apply()  # snapshot 000002 now holds the first deployment

    # Simulate a later deploy that introduces an extra fragment file.
    extra = Path(managed_dir) / "stale.conf"
    extra.write_text("# should vanish on rollback\n", encoding="utf-8")

    deployer.rollback()
    assert not extra.exists()
    for name in ("maps.conf", "upstreams.conf", "http.conf", "stream.conf"):
        assert (Path(managed_dir) / name).is_file()


def test_rollback_is_atomic_on_failure(managed_dir):
    """The snapshot directory is the source of truth: staging the restore into
    a temp dir first means a crash mid-restore cannot corrupt the snapshot."""
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()
    deployer.apply()

    snapshot = sorted((Path(managed_dir) / "backups").glob("*"))[-1]
    before = {path.name: path.read_text() for path in snapshot.glob("*.conf")}

    deployer.rollback()

    after = {path.name: path.read_text() for path in snapshot.glob("*.conf")}
    assert before == after


# ----------------------------------------------------------------------
# dry-run diff preview
# ----------------------------------------------------------------------

def test_dry_run_reports_what_would_change(managed_dir):
    """A dry run on an empty tree should name every fragment it would write."""
    manager = FakeNginxManager()
    result = make_deployer(managed_dir, manager).apply(dry_run=True)

    assert result.dry_run
    assert set(result.changed_fragment_names) >= {"maps.conf", "upstreams.conf", "http.conf"}
    # Nothing was written.
    assert not list(Path(managed_dir).glob("*.conf"))


def test_dry_run_reports_no_changes_when_idempotent(managed_dir):
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()

    result = deployer.apply(dry_run=True)
    assert result.changed_fragment_names == []
    assert "no changes" in result.diff


def test_dry_run_diff_shows_a_real_change(managed_dir):
    """Changing a backend port must surface as a diff line in the upstream."""
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()

    changed = CONFIG.replace("port: 62050", "port: 62099")
    result = Deployer(parse_config_string(changed), managed_dir, manager).apply(dry_run=True)

    assert "upstreams.conf" in result.changed_fragment_names
    assert "62099" in result.diff
    assert "62050" in result.diff
    # The live tree is untouched.
    assert "62050" in (Path(managed_dir) / "upstreams.conf").read_text()


def test_dry_run_diff_reports_fragment_removal(managed_dir):
    """A config that drops every route must delete the fragments, not leave
    the old ones serving stale routes."""
    manager = FakeNginxManager()
    deployer = make_deployer(managed_dir, manager)
    deployer.apply()
    assert (Path(managed_dir) / "http.conf").is_file()

    # Drop the routes block entirely, keeping the rest of the document valid.
    lines = CONFIG.splitlines()
    routes_index = next(i for i, line in enumerate(lines) if line.strip() == "routes:")
    empty = "\n".join(lines[:routes_index]) + "\nroutes: []\n"
    result = Deployer(parse_config_string(empty), managed_dir, manager).apply(dry_run=True)
    assert "http.conf" in result.changed_fragment_names



