"""Configuration validation.

Validation runs in four phases, all before any Nginx file is written:

1. schema/field validation (done during parsing)
2. reference resolution (every id points at something real)
3. compatibility checks (transport layer vs listener mode, TLS modes,
   ambiguous locations, duplicate SNI values)
4. loop detection (chains, fallbacks, failover groups, tunnel wiring)

A report accumulates every problem so the user can fix them in one pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..utils.logging import get_logger
from ..utils.security import ValidationError
from .schema import (
    Backend,
    Listener,
    Route,
    RouterConfig,
    Tunnel,
)

_log = get_logger(__name__)


@dataclass
class Problem:
    """One validation finding."""

    object_id: str
    message: str
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.object_id}: {self.message}"


@dataclass
class ValidationReport:
    """Accumulated validation results."""

    problems: list[Problem] = field(default_factory=list)

    def error(self, object_id: str, message: str) -> None:
        self.problems.append(Problem(object_id, message, "error"))

    def warning(self, object_id: str, message: str) -> None:
        self.problems.append(Problem(object_id, message, "warning"))

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        """Raise the first error with all problems attached."""
        if self.errors:
            detail = "; ".join(str(problem) for problem in self.errors)
            raise ValidationError(f"Configuration is invalid ({len(self.errors)} error(s)): {detail}")

    def log(self) -> None:
        for problem in self.problems:
            method = _log.error if problem.severity == "error" else _log.warning
            method("%s: %s", problem.object_id, problem.message)


class Validator:
    """Validates a fully parsed RouterConfig."""

    STREAM_ONLY_MATCHERS = ("sni", "alpn")
    HTTP_ONLY_MATCHERS = ("path", "path_prefix", "path_regex")

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self.report = ValidationReport()

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def validate(self) -> ValidationReport:
        self.validate_references()
        self.validate_listener_compatibility()
        self.validate_tls()
        self.validate_uniqueness()
        self.validate_chains()
        self.detect_loops()
        return self.report

    # ------------------------------------------------------------------
    # references
    # ------------------------------------------------------------------
    def validate_references(self) -> None:
        cfg = self.config
        # When the deployment models its fleet (a `nodes:` block), every
        # remote_node reference must resolve. Without a fleet declaration the
        # far node is simply not modelled locally, so the reference stays
        # advisory rather than fatal.
        modelled_nodes = {n.id for n in cfg.nodes}
        modelled_nodes.add(cfg.node.id)
        fleet_modelled = bool(cfg.nodes)

        for route in cfg.routes:
            if not cfg.has("listener", route.listener):
                self.report.error(route.id, f"references unknown listener {route.listener!r}")
            if route.backend and not cfg.has("backend", route.backend):
                self.report.error(route.id, f"references unknown backend {route.backend!r}")
            if route.chain and not cfg.has("chain", route.chain):
                self.report.error(route.id, f"references unknown chain {route.chain!r}")
            if route.fallback and not cfg.has("route", route.fallback):
                self.report.error(route.id, f"references unknown fallback route {route.fallback!r}")

        for backend in cfg.backends:
            if backend.type == "tunnel" and backend.tunnel and not cfg.has("tunnel", backend.tunnel):
                self.report.error(backend.id, f"references unknown tunnel {backend.tunnel!r}")
            if backend.type == "failover":
                if backend.primary and not cfg.has("backend", backend.primary):
                    self.report.error(backend.id, f"references unknown primary backend {backend.primary!r}")
                for backup in backend.backups:
                    if not cfg.has("backend", backup):
                        self.report.error(backend.id, f"references unknown backup backend {backup!r}")

        for tunnel in cfg.tunnels:
            if not tunnel.remote_node:
                continue
            if fleet_modelled and tunnel.remote_node not in modelled_nodes:
                self.report.error(
                    tunnel.id, f"references unknown node {tunnel.remote_node!r}"
                )
            elif not fleet_modelled:
                self.report.warning(
                    tunnel.id,
                    f"references node {tunnel.remote_node!r} but no `nodes:` fleet is "
                    "declared; declare nodes to enable node-reference validation",
                )

        for inbound in cfg.pasarguard:
            if inbound.tunnel and not cfg.has("tunnel", inbound.tunnel):
                self.report.error(inbound.id, f"references unknown tunnel {inbound.tunnel!r}")
            if inbound.backend and not cfg.has("backend", inbound.backend):
                self.report.error(inbound.id, f"references unknown backend {inbound.backend!r}")

        for chain in cfg.chains:
            for hop in chain.hops:
                if not cfg.has(hop.type, hop.id):
                    self.report.error(chain.id, f"references unknown {hop.type} {hop.id!r}")

    # ------------------------------------------------------------------
    # listener / transport compatibility
    # ------------------------------------------------------------------
    def validate_listener_compatibility(self) -> None:
        cfg = self.config
        for route in cfg.routes:
            if not cfg.has("listener", route.listener):
                continue
            listener: Listener = cfg.get("listener", route.listener)
            want = route.transport.layer
            if want != listener.mode:
                self.report.error(
                    route.id,
                    f"transport {route.transport.type!r} needs a {want} listener, "
                    f"but listener {listener.id!r} is mode {listener.mode!r}",
                )
                continue
            matchers = route.match.all_matchers() if route.match else []
            matcher_types = [m.type for m in matchers if m.type]
            bad_stream = [t for t in matcher_types if t in self.HTTP_ONLY_MATCHERS]
            bad_http = [t for t in matcher_types if t in self.STREAM_ONLY_MATCHERS]
            if listener.mode == "stream" and bad_stream:
                self.report.error(
                    route.id,
                    f"matcher(s) {bad_stream} match HTTP paths and cannot be used on a stream listener",
                )
            if listener.mode == "http" and bad_http:
                self.report.error(
                    route.id,
                    f"matcher(s) {bad_http} need ssl_preread and require a stream listener",
                )
            if listener.mode == "stream" and not bad_http and not bad_stream:
                # stream route without an SNI/ALPN matcher: allowed (default backend)
                pass

        # listener bind conflicts
        seen: dict[tuple[str, int, str], str] = {}
        for listener in cfg.listeners:
            for address, port, protocol in listener.all_listens():
                key = (address, port, protocol)
                if key in seen:
                    self.report.error(
                        listener.id,
                        f"binds {address}:{port}/{protocol} already used by listener {seen[key]!r}",
                    )
                else:
                    seen[key] = listener.id

    # ------------------------------------------------------------------
    # TLS
    # ------------------------------------------------------------------
    def validate_tls(self) -> None:
        cfg = self.config
        for listener in cfg.listeners:
            self._check_tls(listener.id, listener.mode, listener.tls)
        for route in cfg.routes:
            if route.tls:
                listener = cfg.get("listener", route.listener) if cfg.has("listener", route.listener) else None
                mode = listener.mode if listener else "http"
                self._check_tls(route.id, mode, route.tls)

    def _check_tls(self, obj_id: str, listener_mode: str, tls) -> None:
        if listener_mode == "http" and tls.mode == "passthrough":
            self.report.error(
                obj_id,
                "tls.mode 'passthrough' requires a stream listener (ssl_preread); "
                "an http listener always terminates TLS",
            )
        if listener_mode == "stream" and tls.mode == "terminate":
            self.report.error(
                obj_id,
                "tls.mode 'terminate' on a stream listener is not generated; use an http "
                "listener to terminate TLS or 'passthrough'/'disabled' on stream",
            )
        if tls.mode == "terminate" and tls.certificate and not self.config.has("certificate", tls.certificate):
            self.report.error(obj_id, f"references unknown certificate {tls.certificate!r}")

    # ------------------------------------------------------------------
    # uniqueness (ambiguous locations / duplicate SNI)
    # ------------------------------------------------------------------
    def validate_uniqueness(self) -> None:
        cfg = self.config
        http_keys: dict[str, dict[str, str]] = {}   # listener -> location key -> route
        sni_values: dict[str, dict[str, str]] = {}  # listener -> sni -> route

        for route in cfg.routes:
            if not cfg.has("listener", route.listener):
                continue
            listener: Listener = cfg.get("listener", route.listener)
            if listener.mode == "http":
                keys = self._http_location_keys(route)
                table = http_keys.setdefault(listener.id, {})
                for key in keys:
                    if key in table:
                        self.report.error(
                            route.id,
                            f"ambiguous location {key!r} already used by route {table[key]!r}",
                        )
                    else:
                        table[key] = route.id
            else:
                snis = self._stream_sni_values(route)
                table = sni_values.setdefault(listener.id, {})
                for sni in snis:
                    if sni in table:
                        self.report.error(
                            route.id,
                            f"duplicate SNI {sni!r} already routed by {table[sni]!r}",
                        )
                    else:
                        table[sni] = route.id

    @staticmethod
    def _http_location_keys(route: Route) -> list[str]:
        """Return canonical nginx location keys a route would emit."""
        if route.match is None:
            return ["=/"]
        keys: list[str] = []
        for matcher in route.match.all_matchers():
            if matcher.type == "path":
                keys.append(f"={matcher.value}")
            elif matcher.type == "path_prefix":
                prefix = matcher.value or "/"
                keys.append(f"^{prefix}")
            elif matcher.type == "path_regex":
                keys.append(f"~{matcher.value}")
        return keys or ["=/"]

    @staticmethod
    def _stream_sni_values(route: Route) -> list[str]:
        if route.match is None:
            return []
        values: list[str] = []
        for matcher in route.match.all_matchers():
            if matcher.type == "sni" and matcher.value:
                values.append(matcher.value.lower())
            if matcher.type == "sni" and matcher.values:
                values.extend(v.lower() for v in matcher.values)
        return values

    # ------------------------------------------------------------------
    # chains
    # ------------------------------------------------------------------
    def validate_chains(self) -> None:
        for chain in self.config.chains:
            seen: set[str] = set()
            for index, hop in enumerate(chain.hops):
                ref = f"{hop.type}:{hop.id}"
                if ref in seen:
                    self.report.error(chain.id, f"hop {index + 1} repeats {ref} (loop)")
                seen.add(ref)
            tunnel_indices = [i for i, hop in enumerate(chain.hops) if hop.type == "tunnel"]
            if len(tunnel_indices) > 1:
                # Multi-hop: tunnels must be contiguous; a gap between tunnel
                # hops means traffic would not actually reach the next tunnel.
                for position, index in enumerate(tunnel_indices[:-1]):
                    next_index = tunnel_indices[position + 1]
                    if next_index != index + 1:
                        self.report.error(
                            chain.id,
                            f"non-contiguous tunnel hops {index + 1} and {next_index + 1}: "
                            "tunnels in a multi-hop chain must be consecutive",
                        )
            if len(chain.hops) > 1 and chain.hops[0].type not in ("listener", "route"):
                self.report.warning(
                    chain.id, f"first hop is usually a listener or route; got {chain.hops[0].type}"
                )
            if tunnel_indices and chain.hops[-1].type not in ("backend", "route"):
                self.report.warning(
                    chain.id, f"last hop is usually a backend or route; got {chain.hops[-1].type}"
                )

    # ------------------------------------------------------------------
    # loop detection
    # ------------------------------------------------------------------
    def detect_loops(self) -> None:
        self._detect_backend_loops()
        self._detect_failover_loops()
        self._detect_tunnel_loops()
        self._detect_fallback_loops()

    def _detect_backend_loops(self) -> None:
        """backend -> tunnel -> backend cycles (a tunnel delivering into itself)."""
        cfg = self.config
        endpoints: dict[tuple[str, int], str] = {}
        for backend in cfg.backends:
            if backend.host and backend.port:
                endpoints[(backend.host, backend.port)] = backend.id
        for tunnel in cfg.tunnels:
            if tunnel.target_host and tunnel.target_port:
                target = endpoints.get((tunnel.target_host, tunnel.target_port))
                if target and self._backend_reaches(cfg, target, tunnel.id):
                    self.report.error(
                        tunnel.id,
                        f"loop: tunnel delivers to backend {target!r} which reaches this tunnel",
                    )

    def _backend_reaches(self, cfg: RouterConfig, backend_id: str, tunnel_id: str, _seen: Optional[set] = None) -> bool:
        """True if backend_id (transitively) proxies into tunnel_id."""
        seen = _seen or set()
        if backend_id in seen:
            return False
        seen.add(backend_id)
        backend: Backend = cfg.get("backend", backend_id)
        if backend.type == "tunnel":
            return backend.tunnel == tunnel_id
        if backend.type == "failover":
            refs = [backend.primary] if backend.primary else []
            refs.extend(backend.backups)
            return any(self._backend_reaches(cfg, ref, tunnel_id, seen) for ref in refs if ref)
        return False

    def _detect_failover_loops(self) -> None:
        cfg = self.config
        for backend in cfg.backends:
            if backend.type != "failover":
                continue
            refs = [backend.primary] if backend.primary else []
            refs.extend(backend.backups)
            for ref in refs:
                if ref and self._failover_chain_reaches(cfg, ref, backend.id):
                    self.report.error(backend.id, f"failover loop through {ref!r}")
                if ref == backend.id:
                    self.report.error(backend.id, "references itself")

    def _failover_chain_reaches(self, cfg: RouterConfig, start: str, target: str, _seen: Optional[set] = None) -> bool:
        seen = _seen or set()
        if start in seen:
            return False
        seen.add(start)
        if start == target:
            return True
        backend: Backend = cfg.get("backend", start)
        if backend.type == "failover":
            refs = [backend.primary] if backend.primary else []
            refs.extend(backend.backups)
            return any(self._failover_chain_reaches(cfg, ref, target, seen) for ref in refs if ref)
        return False

    def _detect_tunnel_loops(self) -> None:
        """Detect tunnels feeding each other in a cycle.

        A tunnel's egress (where traffic leaves the far side, ``target``) that
        coincides with another tunnel's ingress (reverse listener, or direct
        remote endpoint) means traffic chains through that tunnel. A cycle in
        this graph loops traffic forever.
        """
        cfg = self.config
        graph: dict[str, list[str]] = {tunnel.id: [] for tunnel in cfg.tunnels}
        for tunnel in cfg.tunnels:
            egress = self._tunnel_egress(tunnel)
            if egress is None:
                continue
            for other in cfg.tunnels:
                if other.id == tunnel.id:
                    continue
                if self._tunnel_ingress(other) == egress:
                    graph[tunnel.id].append(other.id)

        for start in graph:
            if self._has_cycle(graph, start):
                self.report.error(start, "tunnel topology contains a loop")

    @staticmethod
    def _tunnel_ingress(tunnel: Tunnel) -> Optional[tuple[str, int]]:
        """Where traffic enters this tunnel from Nginx's point of view."""
        return Validator._tunnel_reach_endpoint(tunnel)

    @staticmethod
    def _tunnel_egress(tunnel: Tunnel) -> Optional[tuple[str, int]]:
        """Where traffic exits on the far side of this tunnel."""
        if tunnel.target_host and tunnel.target_port is not None:
            return (tunnel.target_host, tunnel.target_port)
        return None

    @staticmethod
    def _tunnel_reach_endpoint(tunnel: Tunnel) -> Optional[tuple[str, int]]:
        if tunnel.mode == "reverse" and tunnel.listener_address and tunnel.listener_port:
            return (tunnel.listener_address, tunnel.listener_port)
        if tunnel.mode == "direct" and tunnel.remote_host and tunnel.remote_port:
            return (tunnel.remote_host, tunnel.remote_port)
        return None

    @staticmethod
    def _has_cycle(graph: dict[str, list[str]], start: str) -> bool:
        """Iterative DFS cycle detection with WHITE/GRAY/BLACK colouring."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {node: WHITE for node in graph}
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, index = stack[-1]
            neighbours = graph.get(node, [])
            if index < len(neighbours):
                stack[-1] = (node, index + 1)
                nxt = neighbours[index]
                if color.get(nxt, WHITE) == GRAY:
                    return True
                if color.get(nxt, WHITE) == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                stack.pop()
        return False

    def _detect_fallback_loops(self) -> None:
        cfg = self.config
        current: dict[str, str] = {}
        for route in cfg.routes:
            if route.fallback:
                current[route.id] = route.fallback
        for start in current:
            seen: set[str] = set()
            node: Optional[str] = start
            while node and node not in seen:
                seen.add(node)
                node = current.get(node)
            if node is not None:
                self.report.error(start, f"fallback chain loops at {node!r}")


def validate_config(config: RouterConfig, log: bool = True) -> ValidationReport:
    """Convenience entry point used by the service layer."""
    report = Validator(config).validate()
    if log:
        report.log()
    return report
