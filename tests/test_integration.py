"""Integration tests.

These exercise the whole pipeline against every shipped example, plus the
CLI as a black box, so the pieces are verified working together rather than
in isolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from pg_router.cli.app import main as cli_main
from pg_router.config.loader import load_config
from pg_router.config.validator import validate_config
from pg_router.deploy.deployer import Deployer
from pg_router.nginx.generator import ConfigGenerator
from pg_router.nginx.modules import NginxModules


def load_example(name: str):
    """Load one shipped example configuration."""
    from pg_router.config.loader import parse_config_string

    root = Path(__file__).resolve().parent.parent
    return parse_config_string((root / "examples" / name).read_text(encoding="utf-8"))


EXAMPLES = [
    "single-node.yaml",
    "edge-mixed.yaml",
    "multi-hop-chain.yaml",
    "failover-one-to-many.yaml",
]


class RecordingNginxManager:
    """Stand-in manager so the deploy pipeline runs without nginx or root."""

    def __init__(self) -> None:
        self.reloads = 0
        self.tests = 0

    def status(self):
        from pg_router.nginx.manager import NginxStatus

        return NginxStatus(installed=True, running=True, version="1.24.0 (test)")

    def test(self, main_conf=None, prefix=None):
        self.tests += 1
        return True, "nginx: configuration file test is successful"

    def reload(self) -> None:
        self.reloads += 1

    def ensure_privileges(self) -> None:
        pass

    @property
    def modules(self) -> NginxModules:
        return NginxModules(
            binary="nginx",
            version="1.24.0",
            features={feature: True for feature in (
                "http", "http_ssl", "http_v2", "http_grpc", "http_websocket",
                "stream", "stream_ssl", "stream_ssl_preread",
            )},
        )


@pytest.fixture
def examples_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_example_is_valid(example: str):
    report = validate_config(load_example(example))
    assert report.valid, [str(problem) for problem in report.errors]


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_example_generates_all_fragments(example: str):
    result = ConfigGenerator(load_example(example)).generate()
    assert result.errors == []
    for fragment in ("maps.conf", "upstreams.conf", "http.conf", "stream.conf"):
        assert fragment in result.fragments
        assert "pg-router managed file" in result.fragments[fragment]


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_example_deploys_dry_run(example: str, tmp_path: Path):
    manager = RecordingNginxManager()
    deployer = Deployer(load_example(example), str(tmp_path / "pg-router"), manager)
    result = deployer.apply(dry_run=True)
    assert result.staged_test_ok is True
    assert result.applied is False
    assert manager.reloads == 0
    # Dry run must never leave fragments behind.
    assert not list((tmp_path / "pg-router").glob("*.conf"))


@pytest.mark.parametrize("example", EXAMPLES)
def test_every_example_deploys_and_is_idempotent(example: str, tmp_path: Path):
    managed = str(tmp_path / "pg-router")
    manager = RecordingNginxManager()
    first = Deployer(load_example(example), managed, manager).apply()
    assert first.applied is True

    fragments_before = {
        name: (Path(managed) / name).read_text() for name in
        ("maps.conf", "upstreams.conf", "http.conf", "stream.conf")
    }
    second = Deployer(load_example(example), managed, manager).apply()
    fragments_after = {
        name: (Path(managed) / name).read_text() for name in
        ("maps.conf", "upstreams.conf", "http.conf", "stream.conf")
    }
    assert second.applied is True
    assert fragments_before == fragments_after


def test_generated_config_has_no_duplicate_map_blocks(examples_dir: Path):
    """The original ng-nginx.sh duplicate-map failure mode must be impossible."""
    for example in EXAMPLES:
        maps = ConfigGenerator(load_example(example)).generate().fragments["maps.conf"]
        assert maps.count("map $http_upgrade $connection_upgrade {") <= 1
        assert "map $ssl_preread_server_name" not in maps or maps.count("map $ssl_preread_server_name $pg_sni_") == 1


# ---------------------------------------------------------------------------
# CLI black-box tests
# ---------------------------------------------------------------------------


def run_cli(argv: list[str]) -> tuple[int, str]:
    """Invoke the CLI, capturing stdout as JSON when requested."""
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        code = cli_main(["--json", "--config", _example_path("edge-mixed.yaml")] + argv)
    return code, out.getvalue()


def _example_path(name: str) -> str:
    return str(Path(__file__).resolve().parent.parent / "examples" / name)


def test_cli_validate_reports_valid():
    code, output = run_cli(["validate"])
    payload = json.loads(output)
    assert code == 0
    assert payload["valid"] is True


def test_cli_validate_reports_errors(tmp_path: Path):
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "version: 1\n"
        "listeners:\n"
        "  - {id: l1, port: 443, mode: http}\n"
        "routes:\n"
        "  - {id: r1, listener: missing, transport: {type: ws},"
        " match: {type: path_prefix, value: /x}, backend: also-missing}\n",
        encoding="utf-8",
    )
    code, output = _run_with_config(["validate"], str(broken))
    payload = json.loads(output)
    assert code == 1
    assert payload["valid"] is False
    assert any("unknown listener" in error for error in payload["errors"])


def _run_with_config(argv: list[str], config_path: str) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        code = cli_main(["--json", "--config", config_path] + argv)
    return code, out.getvalue()


def test_cli_routes_list():
    code, output = run_cli(["routes", "list"])
    payload = json.loads(output)
    assert code == 0
    ids = {route["id"] for route in payload}
    assert {"nl-ws", "fr-tcp", "local-default"} <= ids


def test_cli_routes_test_matches_ws_route():
    code, output = run_cli(
        ["routes", "test", "--host", "example.com", "--path", "/nl/10000"]
    )
    payload = json.loads(output)
    assert code == 0
    matched = {match["route"] for match in payload["matches"]}
    assert "nl-ws" in matched
    assert payload["matches"][0]["endpoints"] == ["127.0.0.1:41001"]


def test_cli_routes_test_matches_sni_route():
    code, output = run_cli(["routes", "test", "--sni", "fr.example.com"])
    payload = json.loads(output)
    assert code == 0
    matched = {match["route"] for match in payload["matches"]}
    assert "fr-tcp" in matched


def test_cli_backends_list():
    code, output = run_cli(["backends", "list"])
    payload = json.loads(output)
    assert code == 0
    assert any(backend["id"] == "pg-nl" for backend in payload)


def test_cli_tunnels_list_resolves_endpoints():
    code, output = run_cli(["tunnels", "list"])
    payload = json.loads(output)
    assert code == 0
    tunnels = {tunnel["id"]: tunnel for tunnel in payload}
    assert tunnels["nl-reverse"]["mode"] == "reverse"
    assert tunnels["fr-direct"]["mode"] == "direct"


def test_cli_generate_dry_run_writes_fragments(tmp_path: Path):
    out_dir = tmp_path / "generated"
    code, output = run_cli(["generate", "--dry-run", "--output-dir", str(out_dir)])
    payload = json.loads(output)
    assert code == 0
    written = payload["written"]
    assert set(written) == {"maps.conf", "upstreams.conf", "http.conf", "stream.conf"}
    for path in written.values():
        assert Path(path).is_file()


def test_cli_status_reports_objects_and_snapshots(tmp_path: Path):
    code, output = run_cli(["status"])
    payload = json.loads(output)
    assert code == 0
    assert payload["objects"]["routes"] == 3
    # The snapshot list is opt-in: it is only useful before a rollback, and
    # printing it by default buries the state an operator actually scans for.
    assert "snapshots" not in payload
    # last_deploy and deploy_lock answer "when did this last change?" and
    # "is an operation in flight?", so they stay in the default view.
    assert "last_deploy" in payload
    assert "deploy_lock" in payload

    code, output = run_cli(["status", "--full"])
    payload = json.loads(output)
    assert code == 0
    assert "snapshots" in payload
    assert payload["last_deploy"] == "never deployed"


def test_cli_nginx_test_on_missing_binary(tmp_path: Path):
    """A host without nginx must fail cleanly, not crash with a traceback."""
    code, output = _run_with_config(["nginx", "test"], _example_path("single-node.yaml"))
    # Exit code comes from the command error path, not an unhandled exception.
    assert code in (0, 3)


def test_cli_init_creates_starter_config(tmp_path: Path):
    target = tmp_path / "new-config.yaml"
    code = cli_main(
        ["--json", "init", "--path", str(target)]
    )
    assert code == 0
    assert target.exists()
    reloaded = load_config(str(target))
    assert reloaded.version == 1
    assert reloaded.routes  # starter route present
