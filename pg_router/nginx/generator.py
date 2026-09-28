"""Nginx configuration generation.

Produces four managed fragments (maps.conf, upstreams.conf, http.conf,
stream.conf) from the resolved topology. Selection of the Nginx mechanism is
driven entirely by the transport layer of each route (requirement 43):

* HTTP-aware transports  -> http/server/location + proxy_pass
* raw TCP/TLS transports -> stream/server + ssl_preread + proxy_pass

Every emitted value originates from a validated schema object, so no raw
configuration string can be injected. Output is deterministic, which makes
deployment idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config.schema import (
    Certificate,
    Listener,
    RouterConfig,
    TLSConfig,
)
from ..model.topology import ResolvedEndpoint, ResolvedRoute, TopologyResolver
from ..plugins.registry import certificates as certificate_registry
from ..utils.logging import get_logger
from ..utils.security import ValidationError

_log = get_logger(__name__)

FRAGMENT_NAMES = ("maps.conf", "upstreams.conf", "http.conf", "stream.conf")
UPSTREAM_PREFIX = "pg_"
BLACKHOLE_UPSTREAM = "pg_blackhole"


@dataclass
class GenerationResult:
    """Generated fragments plus metadata for the deployer and CLI."""

    fragments: dict[str, str] = field(default_factory=dict)
    upstreams: dict[str, list[ResolvedEndpoint]] = field(default_factory=dict)
    route_upstream: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "fragments": sorted(self.fragments),
            "upstreams": {name: [endpoint.address for endpoint in endpoints]
                          for name, endpoints in self.upstreams.items()},
            "route_upstream": dict(self.route_upstream),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


class ConfigGenerator:
    """Turns a RouterConfig into Nginx fragments."""

    def __init__(self, config: RouterConfig, resolver: Optional[TopologyResolver] = None) -> None:
        self.config = config
        self.resolver = resolver or TopologyResolver(config)
        self.result = GenerationResult()
        self._upstream_cache: dict[tuple[tuple, ...], str] = {}
        self._certificate_cache: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    def generate(self) -> GenerationResult:
        routes = self.resolver.resolve_all()
        http_routes = [route for route in routes if route.route.transport.layer == "http"]
        stream_routes = [route for route in routes if route.route.transport.layer == "stream"]

        for route in routes:
            upstream = self._emit_upstream(route)
            self.result.route_upstream[route.route.id] = upstream

        self._stream_defaults = self._compute_stream_defaults(stream_routes)
        self.result.fragments["maps.conf"] = self._render_maps(http_routes, stream_routes)
        self.result.fragments["upstreams.conf"] = self._render_upstreams()
        if http_routes:
            self.result.fragments["http.conf"] = self._render_http(http_routes)
        else:
            self.result.fragments["http.conf"] = _header("http fragment: no http routes configured")
        if stream_routes:
            self.result.fragments["stream.conf"] = self._render_stream(stream_routes)
        else:
            self.result.fragments["stream.conf"] = _header("stream fragment: no stream routes configured")

        if self.result.warnings:
            for warning in self.result.warnings:
                _log.warning(warning)
        return self.result

    def _compute_stream_defaults(self, stream_routes: list[ResolvedRoute]) -> dict[str, str]:
        """Per stream listener: upstream of the route that has no matcher."""
        defaults: dict[str, str] = {}
        by_listener: dict[str, list[ResolvedRoute]] = {}
        for route in stream_routes:
            by_listener.setdefault(route.listener.id, []).append(route)
        for listener_id, routes in by_listener.items():
            plain = [route for route in routes if route.route.match is None]
            if plain:
                defaults[listener_id] = self.result.route_upstream.get(plain[0].route.id, BLACKHOLE_UPSTREAM)
        return defaults

    # ------------------------------------------------------------------
    # certificates
    # ------------------------------------------------------------------
    def _resolve_certificate(self, tls: TLSConfig, obj_id: str) -> tuple[str, str]:
        if not tls.certificate:
            raise ValidationError(
                f"{obj_id}: tls.mode 'terminate' requires a certificate reference", obj_id
            )
        if tls.certificate in self._certificate_cache:
            return self._certificate_cache[tls.certificate]
        certificate: Certificate = self.config.get("certificate", tls.certificate)
        provider = certificate_registry.get(certificate.provider)
        try:
            chain, key = provider.resolve(certificate)
        except FileNotFoundError as exc:
            # Generation can proceed; nginx -t will fail loudly at deploy time.
            self.result.warnings.append(f"{obj_id}: certificate material not available yet ({exc})")
            chain, key = certificate.chain or "", certificate.key or ""
        self._certificate_cache[tls.certificate] = (chain, key)
        return chain, key

    # ------------------------------------------------------------------
    # upstreams (deduplicated by endpoint set)
    # ------------------------------------------------------------------
    def _emit_upstream(self, route: ResolvedRoute) -> str:
        key = tuple(
            (endpoint.address, endpoint.role, endpoint.weight, endpoint.down)
            for endpoint in route.endpoints
        )
        cached = self._upstream_cache.get(key)
        if cached:
            return cached
        name = f"{UPSTREAM_PREFIX}{_safe_name(route.route.id)}"
        self.result.upstreams[name] = route.endpoints
        self._upstream_cache[key] = name
        return name

    def _render_upstreams(self) -> str:
        lines = [_header("upstreams: one per route; failover members are 'backup' servers")]
        for name, endpoints in self.result.upstreams.items():
            lines.append(f"upstream {name} {{")
            for endpoint in endpoints:
                directive = "    server"
                flags = []
                if endpoint.role == "backup":
                    flags.append("backup")
                if endpoint.down:
                    flags.append("down")
                flags.append(f"max_fails={endpoint.max_fails}")
                flags.append(f"fail_timeout={endpoint.fail_timeout}")
                if endpoint.weight != 1:
                    flags.append(f"weight={endpoint.weight}")
                lines.append(f"{directive} {endpoint.address} {' '.join(flags)};")
            lines.append("}")
            lines.append("")
        lines.append(f"upstream {BLACKHOLE_UPSTREAM} {{")
        lines.append("    server 127.0.0.1:9 down;")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # maps
    # ------------------------------------------------------------------
    def _render_maps(self, http_routes: list[ResolvedRoute], stream_routes: list[ResolvedRoute]) -> str:
        lines = [_header("maps: shared connection-upgrade map and stream SNI/ALPN tables")]
        if http_routes:
            # A SINGLE shared map: avoids the duplicate-map failure mode where
            # multiple site files each declare their own map block.
            lines.append("map $http_upgrade $connection_upgrade {")
            lines.append("    default upgrade;")
            lines.append("    ''      close;")
            lines.append("}")
            lines.append("")

        # Aggregate stream tables per listener: one map per variable, never
        # duplicated, so any number of SNI routes stay in a single block.
        sni_tables: dict[str, dict[str, str]] = {}
        alpn_tables: dict[str, dict[str, str]] = {}
        for route in stream_routes:
            listener_id = route.listener.id
            sni = self._sni_table(route)
            if sni:
                sni_tables.setdefault(listener_id, {}).update(sni)
            alpn = self._alpn_table(route)
            if alpn:
                alpn_tables.setdefault(listener_id, {}).update(alpn)

        for listener_id, table in sni_tables.items():
            listener: Listener = next(r.listener for r in stream_routes if r.listener.id == listener_id)
            lines.append(f"map $ssl_preread_server_name $pg_sni_{_safe_name(listener_id)} {{")
            lines.append(f"    default {self._stream_default_upstream(listener)};")
            for host, upstream in sorted(table.items()):
                lines.append(f"    {host} {upstream};")
            lines.append("}")
            lines.append("")
        for listener_id, table in alpn_tables.items():
            listener: Listener = next(r.listener for r in stream_routes if r.listener.id == listener_id)
            lines.append(f"map $ssl_preread_alpn_protocols $pg_alpn_{_safe_name(listener_id)} {{")
            lines.append(f"    default {self._stream_default_upstream(listener)};")
            for protocol, upstream in sorted(table.items()):
                lines.append(f"    ~{protocol} {upstream};")
            lines.append("}")
            lines.append("")
        if len(lines) == 1:
            return _header("maps: none required")
        return "\n".join(lines) + "\n"

    def _sni_table(self, route: ResolvedRoute) -> dict[str, str]:
        table: dict[str, str] = {}
        if route.route.match is None:
            return table
        for matcher in route.route.match.all_matchers():
            if matcher.type != "sni":
                continue
            upstream = self.result.route_upstream.get(route.route.id)
            if not upstream:
                continue
            values = matcher.values or ([matcher.value] if matcher.value else [])
            for value in values:
                table[value.lower()] = upstream
        return table

    def _alpn_table(self, route: ResolvedRoute) -> dict[str, str]:
        table: dict[str, str] = {}
        if route.route.match is None:
            return table
        for matcher in route.route.match.all_matchers():
            if matcher.type != "alpn":
                continue
            upstream = self.result.route_upstream.get(route.route.id)
            if not upstream:
                continue
            values = matcher.values or ([matcher.value] if matcher.value else [])
            for value in values:
                table[value] = upstream
        return table

    def _stream_default_upstream(self, listener: Listener) -> str:
        """Upstream used for SNIs that no route claims."""
        if listener.unknown_policy == "default":
            return self._stream_defaults.get(listener.id, BLACKHOLE_UPSTREAM)
        return BLACKHOLE_UPSTREAM

    # ------------------------------------------------------------------
    # http
    # ------------------------------------------------------------------
    def _render_http(self, routes: list[ResolvedRoute]) -> str:
        lines = [_header("http routes: one server block per host group, one location per route")]
        by_listener: dict[str, list[ResolvedRoute]] = {}
        for route in routes:
            by_listener.setdefault(route.listener.id, []).append(route)

        for listener_id, listener_routes in by_listener.items():
            listener: Listener = listener_routes[0].listener
            groups = self._group_by_host(listener_routes)
            for host_specs, group_routes in groups:
                lines.extend(self._render_http_server(listener, host_specs, group_routes))
                lines.append("")
        return "\n".join(lines) + "\n"

    def _group_by_host(self, routes: list[ResolvedRoute]) -> list[tuple[tuple, list[ResolvedRoute]]]:
        """Merge routes that share the same server_name set into one server block."""
        groups: dict[tuple, list[ResolvedRoute]] = {}
        for route in routes:
            specs = self._host_specs(route)
            groups.setdefault(specs, []).append(route)
        return list(groups.items())

    @staticmethod
    def _host_specs(route: ResolvedRoute) -> tuple:
        """Canonical server_name spec for a route: ((kind, value), ...)."""
        if route.route.match is None:
            return (("default", "_"),)
        specs: list[tuple[str, str]] = []
        has_host = False
        for matcher in route.route.match.all_matchers():
            if matcher.type == "host":
                has_host = True
                values = matcher.values or ([matcher.value] if matcher.value else [])
                for value in values:
                    specs.append(("exact", value.lower()))
            elif matcher.type == "host_regex":
                has_host = True
                specs.append(("regex", matcher.value or ""))
        if not has_host:
            return (("default", "_"),)
        return tuple(sorted(specs))

    def _render_http_server(
        self,
        listener: Listener,
        host_specs: tuple,
        routes: list[ResolvedRoute],
    ) -> list[str]:
        lines: list[str] = ["server {"]
        tls = listener.tls
        for address, port, protocol in listener.all_listens():
            listen_line = f"    listen {address}:{port}"
            if tls.mode == "terminate":
                listen_line += " ssl"
                if self._wants_http2(routes):
                    listen_line += " http2"
            lines.append(listen_line + ";")
        lines.append("    server_name " + self._server_name_directive(host_specs) + ";")
        if tls.mode == "terminate":
            chain, key = self._resolve_certificate(tls, listener.id)
            lines.append(f"    ssl_certificate {chain};")
            lines.append(f"    ssl_certificate_key {key};")
            lines.append(f"    ssl_protocols {tls.protocols};")
            lines.append("    server_tokens off;")
        elif tls.mode == "disabled":
            lines.append("    # tls.mode disabled: plaintext HTTP on this listener")
        lines.append(f"    client_max_body_size {self.config.defaults.client_max_body_size};")
        for route in sorted(routes, key=lambda item: (-len(item.route.match.all_matchers() or []), item.route.priority)):
            lines.extend(self._render_http_location(route))
        lines.append("}")
        return lines

    @staticmethod
    def _wants_http2(routes: list[ResolvedRoute]) -> bool:
        return any(route.route.transport.type in ("http2", "grpc") for route in routes)

    @staticmethod
    def _server_name_directive(host_specs: tuple) -> str:
        names = []
        for kind, value in host_specs:
            if kind == "default":   # catch-all: no host matcher on this route
                names.append("_")
            elif kind == "regex":
                names.append(f"~{value}")
            else:
                names.append(value)
        return " ".join(dict.fromkeys(names))

    def _render_http_location(self, route: ResolvedRoute) -> list[str]:
        transport = route.route.transport
        defaults = self.config.defaults
        buffer_enabled = (
            transport.buffer_enabled
            if transport.buffer_enabled is not None
            else defaults.buffer_enabled
        )
        read_timeout = transport.read_timeout or defaults.read_timeout
        send_timeout = transport.send_timeout or defaults.send_timeout
        connect_timeout = transport.connect_timeout or defaults.connect_timeout
        location = self._location_modifier(route)
        upstream = self.result.route_upstream[route.route.id]
        lines = [f"    {location} {{"]
        if transport.is_grpc:
            scheme = "grpcs://" if transport.upstream_tls else "grpc://"
            lines.append(f"        grpc_pass {scheme}{upstream};")
            lines.append("        grpc_set_header X-Real-IP $remote_addr;")
        else:
            lines.append(f"        proxy_pass http://{upstream};")
            lines.append("        proxy_http_version 1.1;")
            for name, value in self._proxy_headers(route).items():
                lines.append(f"        proxy_set_header {name} {value};")
            if transport.is_websocket or transport.type == "httpupgrade":
                lines.append("        proxy_set_header Upgrade $http_upgrade;")
                lines.append('        proxy_set_header Connection "upgrade";')
            elif transport.type in ("xhttp", "splithttp"):
                lines.append('        proxy_set_header Connection "";')
                lines.append("        chunked_transfer_encoding on;")
                lines.append("        client_body_timeout 1w;")
            lines.append(f"        proxy_read_timeout {read_timeout};")
            lines.append(f"        proxy_send_timeout {send_timeout};")
            lines.append(f"        proxy_connect_timeout {connect_timeout};")
            if not buffer_enabled:
                lines.append("        proxy_buffering off;")
                lines.append("        proxy_request_buffering off;")
        lines.append("    }")
        lines.append("")
        return lines

    def _proxy_headers(self, route: ResolvedRoute) -> dict[str, str]:
        headers: dict[str, str] = {
            "Host": "$host",
            "X-Real-IP": "$remote_addr",
            "X-Forwarded-For": "$proxy_add_x_forwarded_for",
        }
        tls = route.listener.tls
        if tls.mode == "terminate":
            headers["X-Forwarded-Proto"] = "https"
        else:
            headers["X-Forwarded-Proto"] = "$scheme"
        headers.update(self.config.defaults.proxy_headers)
        headers.update(route.route.transport.headers)
        return headers

    @staticmethod
    def _location_modifier(route: ResolvedRoute) -> str:
        """Return the nginx location line for a route's path matcher(s)."""
        match = route.route.match
        if match is None:
            return "location /"
        chosen: Optional[tuple[str, str]] = None
        for matcher in match.all_matchers():
            if matcher.type == "path":
                chosen = ("=", matcher.value or "/")
            elif matcher.type == "path_prefix":
                value = matcher.value or "/"
                if value == "/":
                    chosen = ("", "/")
                else:
                    chosen = ("", value)
            elif matcher.type == "path_regex":
                chosen = ("~", matcher.value or "/")
        if chosen is None:
            return "location /"
        modifier, value = chosen
        return f"location {modifier} {value}".replace("  ", " ").strip()

    # ------------------------------------------------------------------
    # stream
    # ------------------------------------------------------------------
    def _render_stream(self, routes: list[ResolvedRoute]) -> str:
        lines = [_header("stream routes: ssl_preread + SNI/ALPN maps")]
        by_listener: dict[str, list[ResolvedRoute]] = {}
        for route in routes:
            by_listener.setdefault(route.listener.id, []).append(route)

        for listener_id, listener_routes in by_listener.items():
            listener: Listener = listener_routes[0].listener
            self._validate_stream_listener(listener, listener_routes)
            sni_variable = f"pg_sni_{_safe_name(listener.id)}"
            alpn_variable = f"pg_alpn_{_safe_name(listener.id)}"
            default_upstream = self._stream_default_route_upstream(listener_routes)
            lines.append("server {")
            for address, port, protocol in listener.all_listens():
                lines.append(f"    listen {address}:{port};")
            if listener.tls.mode == "passthrough":
                lines.append("    ssl_preread on;")
            lines.append(f"    proxy_pass ${sni_variable};")
            lines.append("    proxy_connect_timeout 10s;")
            lines.append("    proxy_timeout 1w;")
            lines.append("}")
            lines.append("")
            if default_upstream:
                _log.info(
                    "stream listener %s default route: %s", listener.id, default_upstream
                )
        return "\n".join(lines) + "\n"

    def _stream_default_route_upstream(self, routes: list[ResolvedRoute]) -> Optional[str]:
        defaults = [route for route in routes if route.route.match is None]
        if not defaults:
            return None
        if len(defaults) > 1:
            self.result.errors.append(
                f"stream listener {routes[0].listener.id!r} has {len(defaults)} routes without "
                "a matcher; only one default route per stream listener is allowed"
            )
        return self.result.route_upstream.get(defaults[0].route.id)

    def _validate_stream_listener(self, listener: Listener, routes: list[ResolvedRoute]) -> None:
        if listener.tls.mode == "disabled":
            sni_routes = [
                route for route in routes
                if route.route.match and any(m.type in ("sni", "alpn") for m in route.route.match.all_matchers())
            ]
            if sni_routes:
                self.result.errors.append(
                    f"stream listener {listener.id!r} has SNI/ALPN matchers but tls.mode is "
                    "'disabled'; ssl_preread cannot read SNI from plaintext"
                )
        for route in routes:
            if route.route.match is None:
                continue
            matchers = route.route.match.all_matchers()
            types = {m.type for m in matchers if m.type}
            if "sni" in types and "alpn" in types:
                self.result.errors.append(
                    f"route {route.route.id!r}: combined SNI+ALPN matching is not supported "
                    "on a stream listener; use separate listeners"
                )
            unsupported = types - {"sni", "alpn", "source_ip", "source_cidr", "port", "destination_port", "protocol", "transport"}
            if unsupported:
                self.result.warnings.append(
                    f"route {route.route.id!r}: matcher(s) {sorted(unsupported)} have no effect "
                    "in a stream server block"
                )


def _header(text: str) -> str:
    border = "#" + "-" * 78
    return f"{border}\n# pg-router managed file - do not edit by hand\n# {text}\n{border}"


def _safe_name(value: str) -> str:
    """Reduce an id to a name safe for nginx variables and upstream groups.

    Nginx variable names only accept alphanumerics and underscores, so
    hyphens/dots from object ids are replaced.
    """
    return "".join(char if char.isalnum() else "_" for char in value)[:63]


def generate_config(config: RouterConfig) -> GenerationResult:
    """Convenience entry point used by the service layer."""
    return ConfigGenerator(config).generate()
