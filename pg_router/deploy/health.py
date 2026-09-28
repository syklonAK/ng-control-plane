"""Health checks and failover.

The health manager probes backends, tunnel endpoints and PasarGuard inbounds.
Probing is passive by default: a failing probe only reports. It influences
live traffic when the backend's health check declares ``failover: true``,
in which case the backend is marked down and the deployer regenerates the
upstream so backup members take over.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..config.schema import Backend, HealthCheck, RouterConfig, Tunnel
from ..model.topology import TopologyResolver
from ..plugins.registry import tunnels as tunnel_registry
from ..utils.logging import get_logger

_log = get_logger(__name__)


@dataclass
class ProbeResult:
    """Outcome of one probe."""

    target: str
    kind: str            # backend | tunnel
    address: str
    up: bool
    latency_ms: Optional[float] = None
    error: str = ""
    checked_at: str = ""
    probe_type: str = "tcp"

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "kind": self.kind,
            "address": self.address,
            "up": self.up,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "checked_at": self.checked_at,
            "probe_type": self.probe_type,
        }


@dataclass
class _Window:
    """Consecutive success/failure counts implementing rise/fall hysteresis."""

    consecutive_up: int = 0
    consecutive_down: int = 0
    state: Optional[bool] = None


class HealthManager:
    """Probes configured targets and (optionally) drives failover state."""

    def __init__(self, config: RouterConfig, resolver: Optional[TopologyResolver] = None) -> None:
        self.config = config
        self.resolver = resolver or TopologyResolver(config)
        self.results: dict[str, ProbeResult] = {}
        self._windows: dict[str, _Window] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def check_once(self) -> dict[str, ProbeResult]:
        """Probe every backend and tunnel with an enabled health check."""
        results: dict[str, ProbeResult] = {}
        for backend in self.config.backends:
            check = backend.health_check or self.config.defaults.health_check
            if not check.enabled:
                continue
            result = self._check_backend(backend, check)
            results[f"backend:{backend.id}"] = result
        for tunnel in self.config.tunnels:
            check = tunnel.health_check
            if not check or not check.enabled:
                continue
            result = self._check_tunnel(tunnel, check)
            results[f"tunnel:{tunnel.id}"] = result
        self.results = results
        return results

    # ------------------------------------------------------------------
    def _check_backend(self, backend: Backend, check: HealthCheck) -> ProbeResult:
        try:
            endpoints = self.resolver.resolve_backend(backend.id)
        except Exception as exc:  # unresolvable backend: report as down
            return ProbeResult(
                target=backend.id,
                kind="backend",
                address="unresolvable",
                up=False,
                error=str(exc),
                checked_at=_now(),
                probe_type=check.type,
            )
        # Probe the primary member; a group is up if its primary answers.
        endpoint = endpoints[0]
        port = check.port or endpoint.port
        result = self._probe(backend.id, "backend", endpoint.host, port, endpoint.socket, check)
        result.target = backend.id
        if not result.up and backend.type == "failover" and len(endpoints) > 1:
            # fall back to the next member for reporting purposes
            backup = endpoints[1]
            backup_result = self._probe(
                backend.id, "backend", backup.host, check.port or backup.port,
                backup.socket, check,
            )
            if backup_result.up:
                result.error = f"primary down ({result.error}); backup {backup.address} up"
                result.up = True
        return result

    def _check_tunnel(self, tunnel: Tunnel, check: HealthCheck) -> ProbeResult:
        try:
            provider = tunnel_registry.get(tunnel.provider)
            endpoint = provider.health_endpoint(tunnel)
        except Exception as exc:
            return ProbeResult(
                target=tunnel.id,
                kind="tunnel",
                address="unresolvable",
                up=False,
                error=str(exc),
                checked_at=_now(),
                probe_type=check.type,
            )
        if endpoint is None:
            # Socket-based tunnels: nothing connectable to probe directly.
            return ProbeResult(
                target=tunnel.id,
                kind="tunnel",
                address=endpoint_label(tunnel),
                up=True,
                error="socket tunnel: probe not applicable",
                checked_at=_now(),
                probe_type="none",
            )
        result = self._probe(tunnel.id, "tunnel", endpoint.host, endpoint.port, endpoint.socket, check)
        result.target = tunnel.id
        return result

    # ------------------------------------------------------------------
    def _probe(
        self,
        target: str,
        kind: str,
        host: str,
        port: Optional[int],
        socket_path: Optional[str],
        check: HealthCheck,
    ) -> ProbeResult:
        address = f"unix:{socket_path}" if socket_path else f"{host}:{port}"
        started = time.monotonic()
        timeout = _seconds(check.timeout)
        if check.type == "http":
            up, error = self._http_probe(host, port, socket_path, check, timeout)
        else:
            up, error = self._tcp_probe(host, port, socket_path, timeout)
        latency = round((time.monotonic() - started) * 1000, 1)
        return ProbeResult(
            target=target,
            kind=kind,
            address=address,
            up=up,
            latency_ms=latency if up else None,
            error=error,
            checked_at=_now(),
            probe_type=check.type,
        )

    @staticmethod
    def _tcp_probe(host: str, port: Optional[int], socket_path: Optional[str], timeout: float) -> tuple[bool, str]:
        try:
            if socket_path:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(timeout)
                    sock.connect(socket_path)
            else:
                if port is None:
                    return False, "no port to probe"
                with socket.create_connection((host, int(port)), timeout=timeout):
                    pass
        except OSError as exc:
            return False, str(exc)
        return True, ""

    @staticmethod
    def _http_probe(host, port, socket_path, check: HealthCheck, timeout: float) -> tuple[bool, str]:
        if socket_path:
            return False, "http probe over unix socket not supported"
        if port is None:
            return False, "no port to probe"
        url = f"http://{host}:{port}{check.path}"
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except Exception as exc:
            return False, str(exc)
        if code != check.expect_code:
            return False, f"HTTP {code} (expected {check.expect_code})"
        return True, ""

    # ------------------------------------------------------------------
    # failover
    # ------------------------------------------------------------------
    def apply_failover_state(self) -> list[str]:
        """Move traffic away from failing members when failover is enabled.

        Uses rise/fall hysteresis so a single flapping probe does not churn
        the configuration. Returns ids of backends whose state changed.
        """
        changed: list[str] = []
        results = self.results or self.check_once()
        for key, result in results.items():
            if not key.startswith("backend:"):
                continue
            backend_id = key.split(":", 1)[1]
            backend: Optional[Backend] = next(
                (item for item in self.config.backends if item.id == backend_id), None
            )
            if backend is None:
                continue
            check = backend.health_check or self.config.defaults.health_check
            if not check.failover:
                continue
            window = self._windows.setdefault(backend_id, _Window())
            if result.up:
                window.consecutive_up += 1
                window.consecutive_down = 0
                if window.state is not False and window.consecutive_up >= check.rise:
                    if backend.down:
                        backend.down = False
                        changed.append(backend_id)
                        _log.info("Backend %s recovered; returned to service", backend_id)
                    window.state = True
            else:
                window.consecutive_down += 1
                window.consecutive_up = 0
                if window.state is not True and window.consecutive_down >= check.fall:
                    if not backend.down:
                        backend.down = True
                        changed.append(backend_id)
                        _log.warning("Backend %s failed %s probes; failing over", backend_id, check.fall)
                    window.state = False
        return changed

    # ------------------------------------------------------------------
    # monitoring loop
    # ------------------------------------------------------------------
    def monitor(self, interval: Optional[float] = None, on_change=None) -> None:
        """Probe continuously in a background thread until ``stop()``."""
        if self._thread and self._thread.is_alive():
            return
        interval = interval or _seconds(self.config.defaults.health_check.interval)

        def _loop() -> None:
            while not self._stop.is_set():
                self.check_once()
                if on_change:
                    on_change(self.results)
                self._stop.wait(interval)

        self._stop.clear()
        self._thread = threading.Thread(target=_loop, name="pg-router-health", daemon=True)
        self._thread.start()
        _log.info("Health monitor started (interval %.0fs)", interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        _log.info("Health monitor stopped")

    def summary(self) -> dict:
        results = self.results or self.check_once()
        return {
            "up": sum(1 for result in results.values() if result.up),
            "down": sum(1 for result in results.values() if not result.up),
            "targets": {key: result.as_dict() for key, result in results.items()},
        }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(duration) -> float:
    text = str(duration)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = text[-1:]
    if unit in units:
        try:
            return int(text[:-1]) * units[unit]
        except ValueError:
            return 3.0
    try:
        return float(text)
    except ValueError:
        return 3.0


def endpoint_label(tunnel: Tunnel) -> str:
    if tunnel.mode == "reverse":
        return f"reverse-listener {tunnel.listener_address}:{tunnel.listener_port}"
    return f"direct-remote {tunnel.remote_host}:{tunnel.remote_port}"
