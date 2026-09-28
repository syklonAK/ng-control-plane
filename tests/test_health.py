"""Health check and failover tests."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.schema import HealthCheck
from pg_router.deploy.health import HealthManager
from pg_router.nginx.generator import ConfigGenerator
from pg_router.model.topology import TopologyResolver


def start_local_server() -> tuple[str, int, threading.Event]:
    """A minimal TCP listener used as a probe target."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    host, port = server.getsockname()
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
                connection.close()
            except socket.timeout:
                continue
        server.close()

    threading.Thread(target=accept, daemon=True).start()
    return host, port, stop


def test_tcp_probe_detects_up_and_down():
    host, port, stop = start_local_server()
    try:
        check = HealthCheck(type="tcp", timeout="1s")
        manager = HealthManager(_minimal_config(host, port))
        up_result = manager._probe("t", "backend", host, port, None, check)
        assert up_result.up is True
        assert up_result.latency_ms is not None

        down_result = manager._probe("t", "backend", "127.0.0.1", 1, None, check)
        assert down_result.up is False
        assert down_result.error != ""
    finally:
        stop.set()


def test_http_probe_checks_status_code():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/ok":
                self.send_response(200)
            else:
                self.send_response(503)
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        check = HealthCheck(type="http", path="/ok", timeout="2s")
        manager = HealthManager(_minimal_config("127.0.0.1", port))
        assert manager._probe("t", "backend", "127.0.0.1", port, None, check).up is True

        check_bad = HealthCheck(type="http", path="/bad", timeout="2s")
        assert manager._probe("t", "backend", "127.0.0.1", port, None, check_bad).up is False
    finally:
        server.shutdown()
        server.server_close()


def _minimal_config(host: str, port: int):
    return parse_config_string(
        f"""
version: 1
backends:
  - id: probe-target
    type: local
    host: {host}
    port: {port}
    health_check:
      enabled: true
      type: tcp
      timeout: 1s
"""
    )


def test_check_once_reports_backend_state():
    host, port, stop = start_local_server()
    try:
        config = parse_config_string(
            f"""
version: 1
backends:
  - id: up-backend
    type: local
    host: {host}
    port: {port}
    health_check: {{enabled: true, type: tcp, timeout: 1s}}
  - id: down-backend
    type: local
    host: 127.0.0.1
    port: 1
    health_check: {{enabled: true, type: tcp, timeout: 1s}}
  - id: disabled-backend
    type: local
    host: 127.0.0.1
    port: 1
    health_check: {{enabled: false}}
"""
        )
        results = HealthManager(config).check_once()
        assert results["backend:up-backend"].up is True
        assert results["backend:down-backend"].up is False
        assert "backend:disabled-backend" not in results
    finally:
        stop.set()


def test_failover_requires_explicit_opt_in():
    """A failing probe must not move traffic unless failover is enabled."""
    config = parse_config_string(
        """
version: 1
backends:
  - id: b1
    type: local
    host: 127.0.0.1
    port: 1
    health_check: {enabled: true, type: tcp, timeout: 1s, fall: 1}
"""
    )
    manager = HealthManager(config)
    manager.check_once()
    assert manager.apply_failover_state() == []
    assert config.get("backend", "b1").down is False


def test_failover_marks_backend_down_after_threshold():
    config = parse_config_string(
        """
version: 1
backends:
  - id: b1
    type: local
    host: 127.0.0.1
    port: 1
    health_check: {enabled: true, type: tcp, timeout: 1s, fall: 2, failover: true}
"""
    )
    manager = HealthManager(config)
    manager.check_once()
    assert manager.apply_failover_state() == []          # only 1 failure so far
    manager.check_once()
    assert manager.apply_failover_state() == ["b1"]      # threshold reached
    assert config.get("backend", "b1").down is True


def test_failover_recovers_after_rise_count():
    host, port, stop = start_local_server()
    try:
        config = parse_config_string(
            f"""
version: 1
backends:
  - id: b1
    type: local
    host: {host}
    port: {port}
    health_check: {{enabled: true, type: tcp, timeout: 1s, fall: 1, rise: 2, failover: true}}
"""
        )
        manager = HealthManager(config)
        backend = config.get("backend", "b1")
        backend.down = True
        manager.check_once()
        assert manager.apply_failover_state() == []      # 1 success, need 2
        assert backend.down is True
        manager.check_once()
        assert manager.apply_failover_state() == ["b1"]  # recovered
        assert backend.down is False
    finally:
        stop.set()


def test_failover_backend_down_propagates_to_generated_upstream():
    host, port, stop = start_local_server()
    try:
        config = parse_config_string(
            f"""
version: 1
listeners:
  - {{id: https, address: 0.0.0.0, port: 443, mode: http, tls: {{mode: disabled}}}}
backends:
  - id: group
    type: failover
    primary: up
    backups: [down]
  - {{id: up, type: local, host: {host}, port: {port}}}
  - {{id: down, type: local, host: 127.0.0.1, port: 1}}
routes:
  - id: r1
    listener: https
    transport: {{type: ws}}
    match: {{type: path_prefix, value: /x}}
    backend: group
"""
        )
        config.get("backend", "down").down = True
        result = ConfigGenerator(config, TopologyResolver(config)).generate()
        upstream = result.fragments["upstreams.conf"]
        assert "127.0.0.1:1 backup down max_fails=3 fail_timeout=10s;" in upstream
    finally:
        stop.set()


def test_summary_counts_up_and_down():
    host, port, stop = start_local_server()
    try:
        config = parse_config_string(
            f"""
version: 1
backends:
  - {{id: up, type: local, host: {host}, port: {port},
      health_check: {{enabled: true, type: tcp, timeout: 1s}}}}
  - {{id: down, type: local, host: 127.0.0.1, port: 1,
      health_check: {{enabled: true, type: tcp, timeout: 1s}}}}
"""
        )
        summary = HealthManager(config).summary()
        assert summary["up"] == 1
        assert summary["down"] == 1
    finally:
        stop.set()
