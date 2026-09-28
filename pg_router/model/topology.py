"""Topology resolution.

Turns the declarative object graph into concrete connection targets:

    route -> backend -> (tunnel?) -> endpoint host:port

Failover groups expand into an ordered endpoint list (primary first) so the
Nginx upstream generator can emit ``backup`` servers. Chain hops resolve into
an ordered hop list with loop protection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config.schema import Backend, Chain, Route, RouterConfig, Tunnel
from ..plugins.registry import tunnels as tunnel_registry
from ..utils.logging import get_logger
from ..utils.security import ValidationError

_log = get_logger(__name__)


@dataclass
class ResolvedEndpoint:
    """A concrete place Nginx can proxy to."""

    backend_id: str
    host: str
    port: Optional[int] = None
    socket: Optional[str] = None
    tunnel_id: Optional[str] = None
    role: str = "primary"   # primary | backup
    weight: int = 1
    max_fails: int = 3
    fail_timeout: str = "10s"
    down: bool = False

    @property
    def address(self) -> str:
        if self.socket:
            return f"unix:{self.socket}"
        return f"{self.host}:{self.port}"


@dataclass
class ResolvedHop:
    """One hop of a resolved multi-hop chain."""

    kind: str        # listener | route | tunnel | backend
    id: str
    endpoint: Optional[ResolvedEndpoint] = None


@dataclass
class ResolvedRoute:
    """A route with all references resolved to concrete endpoints."""

    route: Route
    listener: object
    endpoints: list[ResolvedEndpoint] = field(default_factory=list)
    hops: list[ResolvedHop] = field(default_factory=list)

    @property
    def primary(self) -> Optional[ResolvedEndpoint]:
        return self.endpoints[0] if self.endpoints else None


class TopologyResolver:
    """Resolves configuration objects into connection targets."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # backends
    # ------------------------------------------------------------------
    def resolve_backend(self, backend_id: str, role: str = "primary", _seen: Optional[set] = None) -> list[ResolvedEndpoint]:
        """Expand a backend id into ordered endpoints (primary, then backups)."""
        seen = _seen or set()
        if backend_id in seen:
            raise ValidationError(
                f"Backend resolution loop through {backend_id!r}", backend_id
            )
        seen.add(backend_id)
        backend: Backend = self.config.get("backend", backend_id)
        return self._expand_backend(backend, role, seen)

    def _expand_backend(self, backend: Backend, role: str, seen: set) -> list[ResolvedEndpoint]:
        if backend.type in ("local", "remote", "tcp", "custom"):
            if not backend.host or backend.port is None:
                raise ValidationError(
                    f"backend {backend.id!r} of type {backend.type!r} needs host and port",
                    backend.id,
                )
            return [
                ResolvedEndpoint(
                    backend_id=backend.id,
                    host=backend.host,
                    port=backend.port,
                    role=role,
                    weight=backend.weight,
                    max_fails=backend.max_fails,
                    fail_timeout=backend.fail_timeout,
                    down=backend.down,
                )
            ]
        if backend.type == "unix_socket":
            assert backend.socket
            return [
                ResolvedEndpoint(
                    backend_id=backend.id,
                    host="unix",
                    socket=backend.socket,
                    role=role,
                    weight=backend.weight,
                    max_fails=backend.max_fails,
                    fail_timeout=backend.fail_timeout,
                    down=backend.down,
                )
            ]
        if backend.type == "tunnel":
            assert backend.tunnel
            tunnel: Tunnel = self.config.get("tunnel", backend.tunnel)
            endpoint = self.resolve_tunnel_endpoint(tunnel)
            return [
                ResolvedEndpoint(
                    backend_id=backend.id,
                    host=endpoint.host,
                    port=endpoint.port,
                    socket=endpoint.socket,
                    tunnel_id=tunnel.id,
                    role=role,
                    weight=backend.weight,
                    max_fails=backend.max_fails,
                    fail_timeout=backend.fail_timeout,
                    down=backend.down,
                )
            ]
        if backend.type == "failover":
            endpoints: list[ResolvedEndpoint] = []
            order = ([backend.primary] if backend.primary else []) + list(backend.backups)
            if not order:
                raise ValidationError(
                    f"failover backend {backend.id!r} has no primary or backups", backend.id
                )
            for index, ref in enumerate(order):
                child_role = "primary" if index == 0 else "backup"
                endpoints.extend(self.resolve_backend(ref, child_role, set(seen)))
            # health-managed administrative state propagates to the group
            if backend.down:
                for endpoint in endpoints:
                    endpoint.down = True
            return endpoints
        raise ValidationError(
            f"Cannot expand backend {backend.id!r} of type {backend.type!r}", backend.id
        )

    def resolve_tunnel_endpoint(self, tunnel: Tunnel):
        """Ask the registered provider where Nginx reaches this tunnel."""
        provider = tunnel_registry.get(tunnel.provider)
        return provider.endpoint(tunnel)

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def resolve_route(self, route: Route) -> ResolvedRoute:
        listener = self.config.get("listener", route.listener)
        resolved = ResolvedRoute(route=route, listener=listener)
        if route.chain:
            resolved.hops = self.resolve_chain(self.config.get("chain", route.chain))
            # the chain's final backend hop is the effective target
            for hop in reversed(resolved.hops):
                if hop.kind == "backend":
                    resolved.endpoints = self.resolve_backend(hop.id)
                    break
                if hop.kind == "route":
                    resolved.endpoints = self.resolve_route(self.config.get("route", hop.id)).endpoints
                    break
        elif route.backend:
            resolved.endpoints = self.resolve_backend(route.backend)
        elif route.backend_inline is not None:
            resolved.endpoints = self._expand_backend(route.backend_inline, "primary", set())
        if not resolved.endpoints:
            raise ValidationError(
                f"route {route.id!r} resolves to no backend", route.id
            )
        return resolved

    # ------------------------------------------------------------------
    # chains
    # ------------------------------------------------------------------
    def resolve_chain(self, chain: Chain) -> list[ResolvedHop]:
        hops: list[ResolvedHop] = []
        seen: set[str] = set()
        for hop in chain.hops:
            ref = f"{hop.type}:{hop.id}"
            if ref in seen:
                raise ValidationError(f"chain {chain.id!r} loops at {ref}", chain.id)
            seen.add(ref)
            resolved = ResolvedHop(kind=hop.type, id=hop.id)
            if hop.type == "backend":
                resolved.endpoint = self.resolve_backend(hop.id)[0]
            elif hop.type == "tunnel":
                tunnel: Tunnel = self.config.get("tunnel", hop.id)
                endpoint = self.resolve_tunnel_endpoint(tunnel)
                resolved.endpoint = ResolvedEndpoint(
                    backend_id=tunnel.id,
                    host=endpoint.host,
                    port=endpoint.port,
                    socket=endpoint.socket,
                    tunnel_id=tunnel.id,
                )
            hops.append(resolved)
        return hops

    # ------------------------------------------------------------------
    # whole graph
    # ------------------------------------------------------------------
    def resolve_all(self) -> list[ResolvedRoute]:
        """Resolve every enabled route; raises on the first broken one."""
        resolved: list[ResolvedRoute] = []
        for route in self.config.routes:
            if not route.enabled:
                _log.info("Skipping disabled route %s", route.id)
                continue
            resolved.append(self.resolve_route(route))
        return resolved

    def adjacency(self) -> dict[str, list[str]]:
        """Node-level adjacency for status/graph output (route -> backend -> tunnel)."""
        graph: dict[str, list[str]] = {}
        for route in self.config.routes:
            edges: list[str] = []
            if route.backend:
                edges.append(f"backend:{route.backend}")
            if route.chain:
                edges.append(f"chain:{route.chain}")
            graph[f"route:{route.id}"] = edges
        for backend in self.config.backends:
            edges: list[str] = []
            if backend.type == "tunnel" and backend.tunnel:
                edges.append(f"tunnel:{backend.tunnel}")
            if backend.type == "failover":
                edges.extend(f"backend:{ref}" for ref in ([backend.primary] if backend.primary else []) + list(backend.backups) if ref)
            graph[f"backend:{backend.id}"] = edges
        for tunnel in self.config.tunnels:
            edges: list[str] = []
            if tunnel.remote_node:
                edges.append(f"node:{tunnel.remote_node}")
            graph[f"tunnel:{tunnel.id}"] = edges
        return graph


def resolve_config(config: RouterConfig) -> list[ResolvedRoute]:
    """Convenience wrapper used by the service layer."""
    return TopologyResolver(config).resolve_all()
