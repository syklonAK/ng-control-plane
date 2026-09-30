"""Matcher capability contract.

This is the single source of truth for what a matcher type *means* across the
three places matchers are used:

* :mod:`pg_router.model.matcher` — offline simulation. Every type is evaluated
  there, exactly as written.
* :mod:`pg_router.config.validator` — decides whether a configuration may be
  deployed at all.
* :mod:`pg_router.nginx.generator` — decides what nginx config is emitted.

Before this table existed, the validator and the generator each re-derived
which matchers matter, independently and inconsistently: the generator
silently dropped ``source_cidr`` on an HTTP route (so an operator's CIDR
restriction vanished without a warning) while the simulator honours it. The
table makes that impossible — a matcher is either enforced in nginx for a given
listener mode, or it is simulation-only and must be acknowledged explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..utils.security import ValidationError

HTTP = "http"
STREAM = "stream"

_NON_NGINX = "not expressed in nginx; evaluated by 'pg-router routes test' only"


@dataclass(frozen=True)
class Capability:
    """What one matcher type can do.

    ``enforced_layers`` is the set of listener modes (``http`` / ``stream``)
    that can carry this matcher into the generated nginx config. An empty set
    means the matcher is simulation-only: the routing engine understands it,
    nginx cannot express it.
    """

    matcher_type: str
    enforced_layers: frozenset[str]
    nginx_mechanism: str
    description: str = ""

    @property
    def enforced(self) -> bool:
        return bool(self.enforced_layers)

    def enforced_in(self, listener_mode: str) -> bool:
        return listener_mode in self.enforced_layers


CAPABILITIES: dict[str, Capability] = {
    type_: Capability(type_, layers, mechanism, description)
    for type_, layers, mechanism, description in (
        (
            "path",
            frozenset({HTTP}),
            "location = <path>",
            "exact path match",
        ),
        (
            "path_prefix",
            frozenset({HTTP}),
            "location <prefix>",
            "component-wise path prefix",
        ),
        (
            "path_regex",
            frozenset({HTTP}),
            "location ~ <regex>",
            "PCRE path match",
        ),
        (
            "host",
            frozenset({HTTP}),
            "server_name",
            "request Host header (leading *. wildcard allowed)",
        ),
        (
            "host_regex",
            frozenset({HTTP}),
            "server_name ~ <regex>",
            "regex match on the Host header",
        ),
        (
            "sni",
            frozenset({STREAM}),
            "map $ssl_preread_server_name",
            "TLS SNI on a stream listener (requires ssl_preread)",
        ),
        (
            "alpn",
            frozenset({STREAM}),
            "map $ssl_preread_alpn_protocols",
            "TLS ALPN on a stream listener (requires ssl_preread)",
        ),
        (
            "port",
            frozenset(),
            _NON_NGINX,
            "the listener already fixes the port; per-port routing needs a "
            "separate listener",
        ),
        (
            "destination_port",
            frozenset(),
            _NON_NGINX,
            "the listener already fixes the port",
        ),
        (
            "protocol",
            frozenset(),
            _NON_NGINX,
            "the listener protocol is a bind property, not a routing key",
        ),
        (
            "transport",
            frozenset(),
            _NON_NGINX,
            "the route transport selects the nginx context, not a match key",
        ),
        (
            "source_ip",
            frozenset(),
            _NON_NGINX,
            "client address filtering is not compiled into generated config",
        ),
        (
            "source_cidr",
            frozenset(),
            _NON_NGINX,
            "client CIDR filtering is not compiled into generated config",
        ),
    )
}


def capability(matcher_type: str) -> Capability:
    """The capability record for a matcher type."""
    record = CAPABILITIES.get(matcher_type)
    if record is None:
        raise ValidationError(f"Unknown matcher type {matcher_type!r}")
    return record


def enforced_types() -> frozenset[str]:
    """Matcher types that reach the generated nginx config in some layer."""
    return frozenset(t for t, c in CAPABILITIES.items() if c.enforced)


def simulation_only_types() -> frozenset[str]:
    """Matcher types the routing engine evaluates but nginx never expresses."""
    return frozenset(t for t, c in CAPABILITIES.items() if not c.enforced)


def is_enforced(matcher_type: str, listener_mode: str) -> bool:
    return capability(matcher_type).enforced_in(listener_mode)


def describe_unenforced(matcher_types: list[str]) -> str:
    """Human-readable explanation of why matchers are not enforced."""
    lines = []
    for matcher_type in sorted(set(matcher_types)):
        record = capability(matcher_type)
        lines.append(
            f"{matcher_type} ({record.description}): {record.nginx_mechanism}"
        )
    return "; ".join(lines)
