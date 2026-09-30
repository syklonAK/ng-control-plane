"""Configuration schema: the abstract object model of the routing engine.

Every object (Listener, Route, Matcher, Backend, Tunnel, ...) is a plain
dataclass parsed from YAML/JSON. Fields are validated on construction, so any
object that exists in memory is safe to interpolate into generated Nginx
directives. No hard-coded topology lives here: ports, domains, paths, SNIs,
transports and tunnel providers are all configuration values.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Optional

from ..utils.security import (
    ValidationError,
    validate_address,
    validate_bool,
    validate_choice,
    validate_filesystem_path,
    validate_hostname,
    validate_id,
    validate_path,
    validate_port,
    validate_wildcard_hostname,
)

# ---------------------------------------------------------------------------
# Enumerations (validated, not hard-coded behaviour)
# ---------------------------------------------------------------------------

LISTENER_MODES = ("http", "stream")
LISTENER_PROTOCOLS = ("tcp", "udp")
TLS_MODES = ("terminate", "passthrough", "disabled")
TRANSPORTS = (
    "ws",            # WebSocket
    "httpupgrade",   # HTTP Upgrade (non-WebSocket)
    "xhttp",         # XHTTP (split, streaming)
    "splithttp",     # legacy split HTTP
    "http",          # plain HTTP
    "http2",         # HTTP/2 cleartext
    "grpc",          # gRPC over HTTP/2
    "tcp",           # raw TCP
    "tls",           # opaque TLS (Reality-style)
)
HTTP_TRANSPORTS = ("ws", "httpupgrade", "xhttp", "splithttp", "http", "http2", "grpc")
STREAM_TRANSPORTS = ("tcp", "tls")

BACKEND_TYPES = ("local", "remote", "tcp", "tunnel", "unix_socket", "custom", "failover")
TUNNEL_MODES = ("reverse", "direct")
TUNNEL_PROVIDERS = ("direct", "reverse", "gost", "ssh", "wireguard", "tcp-relay", "unix-socket", "custom")
CERT_PROVIDERS = ("existing", "acme", "custom")
NODE_ROLES = ("edge", "relay", "hybrid", "backend", "gateway")
MATCHER_TYPES = (
    "path",
    "path_prefix",
    "path_regex",
    "host",
    "host_regex",
    "sni",
    "alpn",
    "port",
    "protocol",
    "transport",
    "source_ip",
    "source_cidr",
    "destination_port",
)
HEALTH_TYPES = ("tcp", "http", "none")
CHAIN_HOP_TYPES = ("listener", "route", "tunnel", "backend")


def transport_layer(transport_type: str) -> str:
    """Which Nginx mechanism a transport requires: ``http`` or ``stream``.

    This single mapping drives the whole generator selection (requirement 43):
    HTTP-aware transports get ``http/server/location`` blocks, raw transports
    get ``stream`` blocks with ssl_preread.
    """
    if transport_type in HTTP_TRANSPORTS:
        return "http"
    if transport_type in STREAM_TRANSPORTS:
        return "stream"
    raise ValidationError(f"Unknown transport type {transport_type!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(data: dict, key: str, obj_id: str) -> Any:
    if key not in data:
        raise ValidationError(f"Missing required field '{key}'", obj_id)
    return data[key]


def _optional(data: dict, key: str, default: Any = None) -> Any:
    return data.get(key, default)


def _as_dict(value: Any, obj_id: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"Expected a mapping, got {type(value).__name__}", obj_id)
    return value


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ident_list(values: Any, obj_id: str) -> list[str]:
    out: list[str] = []
    for item in _as_list(values):
        out.append(validate_id(str(item), obj_id))
    return out


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@dataclass
class HealthCheck:
    """Configurable probe for a backend, tunnel endpoint or inbound."""

    type: str = "tcp"
    enabled: bool = True
    interval: str = "10s"
    timeout: str = "3s"
    retries: int = 3
    rise: int = 2
    fall: int = 3
    path: str = "/"          # http only
    expect_code: int = 200   # http only
    port: Optional[int] = None  # override backend port when probing
    failover: bool = False   # health state may move traffic (off by default)

    @classmethod
    def from_dict(cls, data: Any, obj_id: str = "health_check") -> "HealthCheck":
        data = _as_dict(data, obj_id)
        return cls(
            type=validate_choice(_optional(data, "type", "tcp"), list(HEALTH_TYPES), obj_id),
            enabled=validate_bool(_optional(data, "enabled", True), obj_id),
            interval=str(_optional(data, "interval", "10s")),
            timeout=str(_optional(data, "timeout", "3s")),
            retries=int(_optional(data, "retries", 3)),
            rise=int(_optional(data, "rise", 2)),
            fall=int(_optional(data, "fall", 3)),
            path=str(_optional(data, "path", "/")),
            expect_code=int(_optional(data, "expect_code", 200)),
            port=validate_port(_optional(data, "port"), obj_id) if data.get("port") else None,
            failover=validate_bool(_optional(data, "failover", False), obj_id),
        )


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@dataclass
class Certificate:
    """A TLS certificate. Provider can be existing files, ACME or custom."""

    id: str
    provider: str = "existing"
    chain: Optional[str] = None       # fullchain path (existing/custom)
    key: Optional[str] = None         # privkey path (existing/custom)
    domains: list[str] = field(default_factory=list)  # acme
    email: Optional[str] = None       # acme
    acme_directory: Optional[str] = None  # acme (defaults to Let's Encrypt)
    webhook: Optional[str] = None     # custom provider command hook (never shell-interpolated)

    @classmethod
    def from_dict(cls, data: Any) -> "Certificate":
        data = _as_dict(data, "certificate")
        obj_id = validate_id(str(_require(data, "id", "certificate")), "certificate")
        provider = validate_choice(_optional(data, "provider", "existing"), list(CERT_PROVIDERS), obj_id)
        chain = _optional(data, "chain")
        key = _optional(data, "key")
        if provider == "existing":
            if not chain or not key:
                raise ValidationError(
                    "provider 'existing' requires both 'chain' and 'key' paths", obj_id
                )
            validate_filesystem_path(str(chain), object_id=obj_id)
            validate_filesystem_path(str(key), object_id=obj_id)
        domains = [str(d) for d in _as_list(_optional(data, "domains", []))]
        for domain in domains:
            validate_hostname(domain, obj_id)
        return cls(
            id=obj_id,
            provider=provider,
            chain=str(chain) if chain else None,
            key=str(key) if key else None,
            domains=domains,
            email=_optional(data, "email"),
            acme_directory=_optional(data, "acme_directory"),
            webhook=_optional(data, "webhook"),
        )


@dataclass
class TLSConfig:
    """Explicit TLS behaviour for a listener or route.

    ``passthrough`` means Nginx must not terminate TLS (stream + ssl_preread).
    ``terminate`` means Nginx presents a certificate.
    ``disabled`` means plaintext.
    """

    mode: str = "terminate"
    certificate: Optional[str] = None    # certificate id
    protocols: str = "TLSv1.2 TLSv1.3"
    verify_upstream: bool = False
    server_name: Optional[str] = None    # override SNI sent upstream

    @classmethod
    def from_dict(cls, data: Any, obj_id: str = "tls") -> "TLSConfig":
        if data is None:
            return cls()
        data = _as_dict(data, obj_id)
        return cls(
            mode=validate_choice(_optional(data, "mode", "terminate"), list(TLS_MODES), obj_id),
            certificate=validate_id(_optional(data, "certificate"), obj_id) if data.get("certificate") else None,
            protocols=str(_optional(data, "protocols", "TLSv1.2 TLSv1.3")),
            verify_upstream=validate_bool(_optional(data, "verify_upstream", False), obj_id),
            server_name=_optional(data, "server_name"),
        )


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


@dataclass
class Listener:
    """A network listener. Never assume 443 or a single one."""

    id: str
    address: str = "0.0.0.0"
    port: int = 443
    mode: str = "http"               # http | stream
    protocol: str = "tcp"            # tcp | udp
    tls: TLSConfig = field(default_factory=TLSConfig)
    default_server: bool = False
    unknown_policy: str = "reject"    # stream: reject | default (unknown SNI)
    extra_listens: list[tuple[str, int]] = field(default_factory=list)  # additional (address, port)

    @classmethod
    def from_dict(cls, data: Any) -> "Listener":
        data = _as_dict(data, "listener")
        obj_id = validate_id(str(_require(data, "id", "listener")), "listener")
        listener = cls(
            id=obj_id,
            address=validate_address(str(_optional(data, "address", "0.0.0.0")), obj_id),
            port=validate_port(int(_require(data, "port", obj_id)), obj_id),
            mode=validate_choice(_optional(data, "mode", "http"), list(LISTENER_MODES), obj_id),
            protocol=validate_choice(_optional(data, "protocol", "tcp"), list(LISTENER_PROTOCOLS), obj_id),
            tls=TLSConfig.from_dict(_optional(data, "tls"), obj_id),
            default_server=validate_bool(_optional(data, "default_server", False), obj_id),
            unknown_policy=validate_choice(
                _optional(data, "unknown_policy", "reject"), ("reject", "default"), obj_id
            ),
            extra_listens=[],
        )
        for extra in _as_list(_optional(data, "listen", [])):
            extra = _as_dict(extra, obj_id)
            listener.extra_listens.append(
                (
                    validate_address(str(_optional(extra, "address", "0.0.0.0")), obj_id),
                    validate_port(int(_require(extra, "port", obj_id)), obj_id),
                )
            )
        return listener

    def all_listens(self) -> list[tuple[str, int, str]]:
        """Return every (address, port, protocol) this listener binds."""
        result = [(self.address, self.port, self.protocol)]
        result.extend((addr, port, self.protocol) for addr, port in self.extra_listens)
        return result


# ---------------------------------------------------------------------------
# Matcher tree
# ---------------------------------------------------------------------------


@dataclass
class Matcher:
    """A single condition or a compound (all/any/not) of matchers.

    Examples in YAML::

        match:
          type: path_prefix
          value: /nl

        match:
          all:
            - {type: host, value: example.com}
            - {type: path_prefix, value: /nl}

        match:
          any:
            - {type: sni, value: nl.example.com}
            - {type: sni, value: nl2.example.com}

        match:
          not:
            type: source_cidr
            value: 10.0.0.0/8
    """

    type: Optional[str] = None
    value: Optional[str] = None
    values: list[str] = field(default_factory=list)
    all_: Optional[list["Matcher"]] = None
    any_: Optional[list["Matcher"]] = None
    not_: Optional["Matcher"] = None
    ignore_case: bool = False
    regex: bool = False
    normalize_path: bool = True

    @classmethod
    def from_dict(cls, data: Any, obj_id: str = "match") -> "Matcher":
        data = _as_dict(data, obj_id)
        unknown = set(data) - {
            "type", "value", "values", "all", "any", "not",
            "ignore_case", "regex", "normalize_path",
        }
        if unknown:
            raise ValidationError(f"Unknown matcher keys: {sorted(unknown)}", obj_id)

        compound_keys = [k for k in ("all", "any", "not") if k in data]
        if compound_keys and ("type" in data or "value" in data or "values" in data):
            raise ValidationError(
                "A compound matcher (all/any/not) cannot also define type/value", obj_id
            )
        if len(compound_keys) > 1:
            raise ValidationError(
                f"A matcher may use only one of all/any/not, got {sorted(compound_keys)}", obj_id
            )

        matcher = cls(
            ignore_case=validate_bool(_optional(data, "ignore_case", False), obj_id),
            regex=validate_bool(_optional(data, "regex", False), obj_id),
            normalize_path=validate_bool(_optional(data, "normalize_path", True), obj_id),
        )

        if "all" in data:
            items = _as_list(data["all"])
            if not items:
                raise ValidationError("'all' must contain at least one matcher", obj_id)
            matcher.all_ = [cls.from_dict(item, obj_id) for item in items]
        elif "any" in data:
            items = _as_list(data["any"])
            if not items:
                raise ValidationError("'any' must contain at least one matcher", obj_id)
            matcher.any_ = [cls.from_dict(item, obj_id) for item in items]
        elif "not" in data:
            matcher.not_ = cls.from_dict(data["not"], obj_id)
        else:
            matcher.type = validate_choice(
                str(_require(data, "type", obj_id)), list(MATCHER_TYPES), obj_id
            )
            raw_value = _optional(data, "value")
            raw_values = _as_list(_optional(data, "values", []))
            if raw_value is None and not raw_values:
                raise ValidationError(
                    f"Matcher type {matcher.type!r} requires 'value' or 'values'", obj_id
                )
            matcher.value = cls._coerce(matcher.type, raw_value, obj_id) if raw_value is not None else None
            matcher.values = [cls._coerce(matcher.type, v, obj_id) for v in raw_values]
        return matcher

    @staticmethod
    def _coerce(matcher_type: str, value: Any, obj_id: str) -> str:
        """Validate a matcher value according to its type."""
        text = str(value)
        if matcher_type in ("path", "path_prefix"):
            return validate_path(text, obj_id)
        if matcher_type == "path_regex":
            from ..utils.security import validate_regex

            return validate_regex(text, obj_id)
        if matcher_type in ("host", "sni"):
            return validate_wildcard_hostname(text, obj_id)
        if matcher_type == "host_regex":
            from ..utils.security import validate_regex

            return validate_regex(text, obj_id)
        if matcher_type in ("port", "destination_port"):
            return str(validate_port(int(text), obj_id))
        if matcher_type == "alpn":
            return validate_choice(
                text, ("h2", "http/1.1", "h3", "grpc", "spdy/3.1"), obj_id
            )
        if matcher_type == "protocol":
            return validate_choice(text, ("tcp", "udp", "tls"), obj_id)
        if matcher_type == "transport":
            return validate_choice(text, list(TRANSPORTS), obj_id)
        if matcher_type in ("source_ip",):
            from ..utils.security import validate_ip

            return validate_ip(text, obj_id)
        if matcher_type == "source_cidr":
            import ipaddress

            try:
                ipaddress.ip_network(text, strict=False)
            except ValueError as exc:
                raise ValidationError(f"Invalid CIDR {text!r}", obj_id) from exc
            return text
        return text

    @property
    def is_compound(self) -> bool:
        return self.all_ is not None or self.any_ is not None or self.not_ is not None

    def all_matchers(self) -> list["Matcher"]:
        """Flatten the tree (used for analysis and nginx generation)."""
        out: list[Matcher] = [self]
        if self.all_:
            for child in self.all_:
                out.extend(child.all_matchers())
        if self.any_:
            for child in self.any_:
                out.extend(child.all_matchers())
        if self.not_:
            out.extend(self.not_.all_matchers())
        return out


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@dataclass
class Transport:
    """How traffic is spoken on the wire. Determines the Nginx mechanism."""

    type: str = "tcp"
    path: Optional[str] = None        # for http-layer transports (e.g. /nl)
    service: Optional[str] = None     # grpc service name
    headers: dict[str, str] = field(default_factory=dict)
    buffer_enabled: Optional[bool] = None  # None -> inherit from defaults
    read_timeout: Optional[str] = None
    send_timeout: Optional[str] = None
    connect_timeout: Optional[str] = None
    keepalive: Optional[int] = None   # upstream keepalive connections
    upstream_tls: bool = False        # TLS to the upstream (grpc/https backends)
    upstream_sni: Optional[str] = None  # SNI presented to the upstream

    @classmethod
    def from_dict(cls, data: Any, obj_id: str = "transport") -> "Transport":
        if data is None:
            return cls()
        data = _as_dict(data, obj_id)
        transport_type = validate_choice(
            str(_optional(data, "type", "tcp")), list(TRANSPORTS), obj_id
        )
        path = _optional(data, "path")
        if path is not None:
            path = validate_path(str(path), obj_id)
        headers: dict[str, str] = {}
        for name, value in (_as_dict(_optional(data, "headers", {}), obj_id) or {}).items():
            from ..utils.security import validate_header_name, validate_header_value

            headers[validate_header_name(str(name), obj_id)] = validate_header_value(
                str(value), obj_id
            )
        return cls(
            type=transport_type,
            path=path,
            service=_optional(data, "service"),
            headers=headers,
            buffer_enabled=_buffer_default(transport_type, data),
            read_timeout=_optional(data, "read_timeout"),
            send_timeout=_optional(data, "send_timeout"),
            connect_timeout=_optional(data, "connect_timeout"),
            keepalive=int(_optional(data, "keepalive")) if data.get("keepalive") else None,
            upstream_tls=validate_bool(_optional(data, "upstream_tls", False), obj_id),
            upstream_sni=_optional(data, "upstream_sni"),
        )

    @property
    def layer(self) -> str:
        return transport_layer(self.type)

    @property
    def is_websocket(self) -> bool:
        return self.type == "ws"

    @property
    def is_grpc(self) -> bool:
        return self.type == "grpc"


# Streaming transports must not buffer by default; explicit config wins.
STREAMING_TRANSPORTS = ("xhttp", "splithttp")


def _buffer_default(transport_type: str, data: dict) -> Optional[bool]:
    if "buffer_enabled" in data:
        return validate_bool(data["buffer_enabled"], transport_type)
    if transport_type in STREAMING_TRANSPORTS:
        return False
    return None  # inherit from defaults


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


@dataclass
class Backend:
    """A traffic destination. PasarGuard is one kind of backend, not assumed."""

    id: str
    type: str                      # local | remote | tcp | tunnel | unix_socket | custom | failover
    host: Optional[str] = None
    port: Optional[int] = None
    tunnel: Optional[str] = None   # type=tunnel -> tunnel id
    socket: Optional[str] = None   # type=unix_socket
    primary: Optional[str] = None  # type=failover
    backups: list[str] = field(default_factory=list)  # type=failover
    weight: int = 1
    max_fails: int = 3
    fail_timeout: str = "10s"
    health_check: Optional[HealthCheck] = None
    down: bool = False             # administrative state, managed by health manager

    @classmethod
    def from_dict(cls, data: Any) -> "Backend":
        data = _as_dict(data, "backend")
        obj_id = validate_id(str(_require(data, "id", "backend")), "backend")
        backend_type = validate_choice(
            str(_optional(data, "type", "local")), list(BACKEND_TYPES), obj_id
        )
        host = _optional(data, "host")
        port = _optional(data, "port")
        tunnel = _optional(data, "tunnel")
        socket = _optional(data, "socket")

        if backend_type in ("local", "remote", "tcp", "custom"):
            if not host:
                raise ValidationError(f"backend type {backend_type!r} requires 'host'", obj_id)
            host = validate_hostname(str(host), obj_id)
            if port is None:
                raise ValidationError(f"backend type {backend_type!r} requires 'port'", obj_id)
            port = validate_port(int(port), obj_id)
        elif backend_type == "tunnel":
            if not tunnel:
                raise ValidationError("backend type 'tunnel' requires 'tunnel' id", obj_id)
            tunnel = validate_id(str(tunnel), obj_id)
        elif backend_type == "unix_socket":
            if not socket:
                raise ValidationError("backend type 'unix_socket' requires 'socket' path", obj_id)
            validate_filesystem_path(str(socket), object_id=obj_id)
        elif backend_type == "failover":
            if not (data.get("primary") or data.get("backends")):
                raise ValidationError(
                    "backend type 'failover' requires 'primary' (and optional 'backups')", obj_id
                )
            primary = _optional(data, "primary")
            if primary is not None:
                primary = validate_id(str(primary), obj_id)
            backups = _ident_list(_optional(data, "backups", []), obj_id)
            # alternative key name used by the spec
            if not backups and data.get("backends"):
                backups = _ident_list(data["backends"], obj_id)
            return cls(
                id=obj_id,
                type=backend_type,
                primary=primary,
                backups=backups,
                weight=int(_optional(data, "weight", 1)),
                max_fails=int(_optional(data, "max_fails", 3)),
                fail_timeout=str(_optional(data, "fail_timeout", "10s")),
                health_check=HealthCheck.from_dict(_optional(data, "health_check"), obj_id)
                if data.get("health_check")
                else None,
                down=validate_bool(_optional(data, "down", False), obj_id),
            )

        return cls(
            id=obj_id,
            type=backend_type,
            host=host,
            port=port,
            tunnel=tunnel,
            socket=socket,
            weight=int(_optional(data, "weight", 1)),
            max_fails=int(_optional(data, "max_fails", 3)),
            fail_timeout=str(_optional(data, "fail_timeout", "10s")),
            health_check=HealthCheck.from_dict(_optional(data, "health_check"), obj_id)
            if data.get("health_check")
            else None,
            down=validate_bool(_optional(data, "down", False), obj_id),
        )


# ---------------------------------------------------------------------------
# Tunnel
# ---------------------------------------------------------------------------


@dataclass
class TunnelEndpoint:
    """Where Nginx actually connects to reach the far side of a tunnel."""

    host: str
    port: int
    socket: Optional[str] = None


@dataclass
class Tunnel:
    """A tunnel between this node and a remote node.

    ``reverse``: the remote node dials in; Nginx reaches the tunnel through a
    local listener (e.g. 127.0.0.1:41001).
    ``direct``: this node reaches the remote endpoint directly.

    The implementation (GOST, SSH, WireGuard, PingTunnel, custom...) is hidden
    behind the ``provider`` label; the routing engine only ever sees the
    resolved endpoint.
    """

    id: str
    mode: str                        # reverse | direct
    provider: str = "custom"         # tunnel software family
    # reverse: local listener the remote node dials into
    listener_address: Optional[str] = None
    listener_port: Optional[int] = None
    # direct: remote endpoint this node connects to
    remote_host: Optional[str] = None
    remote_port: Optional[int] = None
    remote_node: Optional[str] = None  # logical node id on the far side
    # what the tunnel ultimately delivers to
    target_host: Optional[str] = None
    target_port: Optional[int] = None
    health_check: Optional[HealthCheck] = None
    options: dict[str, Any] = field(default_factory=dict)  # provider-specific, opaque to core

    @classmethod
    def from_dict(cls, data: Any) -> "Tunnel":
        data = _as_dict(data, "tunnel")
        obj_id = validate_id(str(_require(data, "id", "tunnel")), "tunnel")
        mode = validate_choice(str(_require(data, "mode", obj_id)), list(TUNNEL_MODES), obj_id)
        provider = validate_choice(
            str(_optional(data, "provider", "custom")), list(TUNNEL_PROVIDERS), obj_id
        )

        listener = _as_dict(_optional(data, "listener", {}), obj_id)
        remote = _as_dict(_optional(data, "remote", {}), obj_id)
        target = _as_dict(_optional(data, "target", {}), obj_id)

        tunnel = cls(
            id=obj_id,
            mode=mode,
            provider=provider,
            options=_as_dict(_optional(data, "options", {}), obj_id) or {},
            health_check=HealthCheck.from_dict(_optional(data, "health_check"), obj_id)
            if data.get("health_check")
            else None,
        )

        if listener:
            tunnel.listener_address = validate_address(
                str(_optional(listener, "address", "127.0.0.1")), obj_id
            )
            tunnel.listener_port = validate_port(int(_require(listener, "port", obj_id)), obj_id)
        if remote:
            if remote.get("node"):
                tunnel.remote_node = validate_id(str(remote["node"]), obj_id)
            if remote.get("host"):
                tunnel.remote_host = validate_hostname(str(remote["host"]), obj_id)
            if remote.get("port") is not None:
                tunnel.remote_port = validate_port(int(remote["port"]), obj_id)
        if target:
            tunnel.target_host = validate_hostname(str(_require(target, "host", obj_id)), obj_id)
            tunnel.target_port = validate_port(int(_require(target, "port", obj_id)), obj_id)

        if mode == "reverse" and (tunnel.listener_address is None or tunnel.listener_port is None):
            raise ValidationError(
                "reverse tunnel requires a 'listener' with address and port", obj_id
            )
        if mode == "direct" and (tunnel.remote_host is None or tunnel.remote_port is None):
            raise ValidationError(
                "direct tunnel requires a 'remote' with host and port", obj_id
            )
        return tunnel

    def endpoint(self) -> TunnelEndpoint:
        """Resolve the address Nginx must proxy to.

        This is the only place tunnel direction matters for routing: a reverse
        tunnel is reached through its local listener, a direct tunnel through
        its remote endpoint.
        """
        if self.mode == "reverse":
            assert self.listener_address and self.listener_port
            return TunnelEndpoint(self.listener_address, self.listener_port)
        assert self.remote_host and self.remote_port
        return TunnelEndpoint(self.remote_host, self.remote_port)


# ---------------------------------------------------------------------------
# PasarGuard inbounds
# ---------------------------------------------------------------------------


@dataclass
class PasarGuardInbound:
    """A PasarGuard inbound. Resolves to an ordinary backend target."""

    id: str
    host: str = "127.0.0.1"
    port: int = 62050
    protocol: str = "vless"
    tunnel: Optional[str] = None    # if the inbound is reached via a tunnel
    backend: Optional[str] = None   # if the inbound is reached via a named backend
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "PasarGuardInbound":
        data = _as_dict(data, "inbound")
        obj_id = validate_id(str(_require(data, "id", "inbound")), "inbound")
        return cls(
            id=obj_id,
            host=validate_hostname(str(_optional(data, "host", "127.0.0.1")), obj_id),
            port=validate_port(int(_optional(data, "port", 62050)), obj_id),
            protocol=str(_optional(data, "protocol", "vless")),
            tunnel=validate_id(str(data["tunnel"]), obj_id) if data.get("tunnel") else None,
            backend=validate_id(str(data["backend"]), obj_id) if data.get("backend") else None,
            tags=[str(tag) for tag in _as_list(_optional(data, "tags", []))],
        )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """This node's identity and logical roles. Roles are labels, not code paths."""

    id: str
    roles: list[str] = field(default_factory=list)
    address: Optional[str] = None
    region: Optional[str] = None
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Node":
        data = _as_dict(data, "node")
        obj_id = validate_id(str(_optional(data, "id", "node-01")), "node")
        roles = [str(role) for role in _as_list(_optional(data, "roles", ["edge"]))]
        for role in roles:
            validate_choice(role, list(NODE_ROLES), obj_id)
        return cls(
            id=obj_id,
            roles=roles,
            address=validate_hostname(str(data["address"]), obj_id) if data.get("address") else None,
            region=_optional(data, "region"),
            labels={str(k): str(v) for k, v in (_as_dict(_optional(data, "labels", {}), obj_id) or {}).items()},
        )


# ---------------------------------------------------------------------------
# Route + chains
# ---------------------------------------------------------------------------


@dataclass
class ChainHop:
    """One hop of a multi-hop route: a reference to listener/route/tunnel/backend."""

    type: str
    id: str

    @classmethod
    def from_dict(cls, data: Any) -> "ChainHop":
        data = _as_dict(data, "hop")
        hop_type = validate_choice(str(_require(data, "type", "hop")), list(CHAIN_HOP_TYPES), "hop")
        return cls(type=hop_type, id=validate_id(str(_require(data, "id", "hop")), "hop"))


@dataclass
class Chain:
    """An ordered list of hops forming a multi-hop topology."""

    id: str
    hops: list[ChainHop] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "Chain":
        data = _as_dict(data, "chain")
        obj_id = validate_id(str(_require(data, "id", "chain")), "chain")
        hops = [ChainHop.from_dict(item) for item in _as_list(_require(data, "chain", obj_id))]
        if not hops:
            raise ValidationError("chain must contain at least one hop", obj_id)
        return cls(id=obj_id, hops=hops)


@dataclass
class Route:
    """A route binds a listener, a matcher, a transport and a backend.

    A route may also be part of a chain (``chain`` field) for multi-hop
    topologies; the first hop is then the listener and subsequent hops are
    tunnels between nodes.
    """

    id: str
    listener: str
    transport: Transport = field(default_factory=Transport)
    match: Optional[Matcher] = None
    backend: Optional[str] = None        # backend id
    backend_inline: Optional[Backend] = None  # backend declared inside the route
    tls: Optional[TLSConfig] = None      # route-level TLS override
    chain: Optional[str] = None          # chain id (multi-hop)
    fallback: Optional[str] = None       # fallback route id
    health_check: Optional[HealthCheck] = None
    enabled: bool = True
    priority: int = 0
    # Simulation-only matchers (source_ip, source_cidr, port, ...) are refused
    # unless the route explicitly acknowledges that nginx will not enforce
    # them. See pg_router/model/capabilities.py.
    unenforced_matchers: str = "reject"   # reject | allow

    @classmethod
    def from_dict(cls, data: Any) -> "Route":
        data = _as_dict(data, "route")
        obj_id = validate_id(str(_require(data, "id", "route")), "route")
        listener = validate_id(str(_require(data, "listener", obj_id)), obj_id)
        transport = Transport.from_dict(_optional(data, "transport"), obj_id)

        backend_ref = _optional(data, "backend")
        backend_inline: Optional[Backend] = None
        backend_id: Optional[str] = None
        if isinstance(backend_ref, str):
            backend_id = validate_id(backend_ref, obj_id)
        elif backend_ref is not None:
            inline = _as_dict(backend_ref, obj_id)
            inline.setdefault("id", f"{obj_id}-backend")
            backend_inline = Backend.from_dict(inline)
            backend_id = backend_inline.id

        chain = validate_id(str(data["chain"]), obj_id) if data.get("chain") else None
        fallback = validate_id(str(data["fallback"]), obj_id) if data.get("fallback") else None

        return cls(
            id=obj_id,
            listener=listener,
            transport=transport,
            match=Matcher.from_dict(_require(data, "match", obj_id), obj_id) if data.get("match") else None,
            backend=backend_id,
            backend_inline=backend_inline,
            tls=TLSConfig.from_dict(_optional(data, "tls"), obj_id) if data.get("tls") else None,
            chain=chain,
            fallback=fallback,
            health_check=HealthCheck.from_dict(_optional(data, "health_check"), obj_id)
            if data.get("health_check")
            else None,
            enabled=validate_bool(_optional(data, "enabled", True), obj_id),
            priority=int(_optional(data, "priority", 0)),
            unenforced_matchers=validate_choice(
                str(_optional(data, "unenforced_matchers", "reject")),
                ("reject", "allow"),
                obj_id,
            ),
        )

    @property
    def layer(self) -> str:
        return self.transport.layer


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


@dataclass
class Defaults:
    """Global defaults merged into every object before use."""

    connect_timeout: str = "10s"
    read_timeout: str = "3600s"
    send_timeout: str = "3600s"
    buffer_enabled: bool = True
    client_max_body_size: str = "0"
    verify_upstream_tls: bool = False
    tls_protocols: str = "TLSv1.2 TLSv1.3"
    health_check: HealthCheck = field(default_factory=HealthCheck)
    proxy_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "Defaults":
        if data is None:
            return cls()
        data = _as_dict(data, "defaults")
        headers: dict[str, str] = {}
        for name, value in (_as_dict(_optional(data, "proxy_headers", {}), "defaults") or {}).items():
            from ..utils.security import validate_header_name, validate_header_value

            headers[validate_header_name(str(name), "defaults")] = validate_header_value(str(value), "defaults")
        return cls(
            connect_timeout=str(_optional(data, "connect_timeout", "10s")),
            read_timeout=str(_optional(data, "read_timeout", "3600s")),
            send_timeout=str(_optional(data, "send_timeout", "3600s")),
            buffer_enabled=validate_bool(_optional(data, "buffer_enabled", True), "defaults"),
            client_max_body_size=str(_optional(data, "client_max_body_size", "0")),
            verify_upstream_tls=validate_bool(_optional(data, "verify_upstream_tls", False), "defaults"),
            tls_protocols=str(_optional(data, "tls_protocols", "TLSv1.2 TLSv1.3")),
            health_check=HealthCheck.from_dict(_optional(data, "health_check"), "defaults")
            if data.get("health_check")
            else HealthCheck(),
            proxy_headers=headers,
        )


# ---------------------------------------------------------------------------
# Root configuration
# ---------------------------------------------------------------------------


@dataclass
class RouterConfig:
    """The fully parsed and validated configuration document."""

    version: int = 1
    node: Node = field(default_factory=lambda: Node(id="node-01", roles=["edge"]))
    nodes: list[Node] = field(default_factory=list)
    defaults: Defaults = field(default_factory=Defaults)
    listeners: list[Listener] = field(default_factory=list)
    backends: list[Backend] = field(default_factory=list)
    tunnels: list[Tunnel] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    chains: list[Chain] = field(default_factory=list)
    pasarguard: list[PasarGuardInbound] = field(default_factory=list)
    certificates: list[Certificate] = field(default_factory=list)

    # --- lookup indexes (built after parsing) --------------------------------
    _index: dict[str, Any] = field(default_factory=dict, repr=False)

    def build_index(self) -> None:
        """Build id -> object indexes for O(1) reference resolution."""
        self._index.clear()
        for collection, kind in (
            (self.listeners, "listener"),
            (self.backends, "backend"),
            (self.tunnels, "tunnel"),
            (self.routes, "route"),
            (self.chains, "chain"),
            (self.certificates, "certificate"),
        ):
            for obj in collection:
                key = f"{kind}:{obj.id}"
                if key in self._index:
                    raise ValidationError(
                        f"Duplicate {kind} id {obj.id!r}", obj.id
                    )
                self._index[key] = obj
        for inbound in self.pasarguard:
            key = f"inbound:{inbound.id}"
            if key in self._index:
                raise ValidationError(f"Duplicate inbound id {inbound.id!r}", inbound.id)
            self._index[key] = inbound
        known_nodes = {n.id for n in self.nodes} | {self.node.id}
        for node_id in known_nodes:
            self._index.setdefault(f"node:{node_id}", node_id)

    def get(self, kind: str, obj_id: str) -> Any:
        """Resolve a reference or raise a precise error."""
        obj = self._index.get(f"{kind}:{obj_id}")
        if obj is None:
            raise ValidationError(
                f"Route/backend/tunnel references unknown {kind} {obj_id!r}", obj_id
            )
        return obj

    def has(self, kind: str, obj_id: str) -> bool:
        return f"{kind}:{obj_id}" in self._index

    def objects(self) -> dict[str, Any]:
        return dict(self._index)

    def summary(self) -> dict[str, int]:
        return {
            "listeners": len(self.listeners),
            "backends": len(self.backends),
            "tunnels": len(self.tunnels),
            "routes": len(self.routes),
            "chains": len(self.chains),
            "inbounds": len(self.pasarguard),
            "certificates": len(self.certificates),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RouterConfig":
        data = _as_dict(data, "config")
        version = int(_optional(data, "version", 1))
        if version != 1:
            raise ValidationError(
                f"Unsupported config version {version}; only version 1 is supported", "config"
            )
        config = cls(
            version=version,
            node=Node.from_dict(_optional(data, "node", {})),
            nodes=[Node.from_dict(item) for item in _as_list(_optional(data, "nodes", []))],
            defaults=Defaults.from_dict(_optional(data, "defaults", {})),
            listeners=[Listener.from_dict(item) for item in _as_list(_optional(data, "listeners", []))],
            backends=[Backend.from_dict(item) for item in _as_list(_optional(data, "backends", []))],
            tunnels=[Tunnel.from_dict(item) for item in _as_list(_optional(data, "tunnels", []))],
            routes=[Route.from_dict(item) for item in _as_list(_optional(data, "routes", []))],
            chains=[Chain.from_dict(item) for item in _as_list(_optional(data, "chains", []))],
            pasarguard=[PasarGuardInbound.from_dict(item) for item in _as_list(_optional(data, "pasarguard", {}).get("inbounds", []))],
            certificates=[Certificate.from_dict(item) for item in _as_list(_optional(data, "certificates", []))],
        )
        # collect inline backends declared inside routes so they resolve too
        for route in config.routes:
            if route.backend_inline is not None:
                config.backends.append(route.backend_inline)
        config.build_index()
        return config

    def clone_fields(self) -> list[str]:
        return [f.name for f in fields(self)]
