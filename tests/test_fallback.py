"""Fallback route behaviour.

`fallback` used to be accepted by the schema, validated for reference and
loop correctness, and then ignored entirely — a route promised a fallback that
nginx never implemented. It is now expressed as `backup` members of the same
nginx upstream: nginx forwards to them only when every primary member has
failed, which is the semantics the field always advertised.
"""

from __future__ import annotations

from pg_router.config.loader import parse_config_string
from pg_router.config.validator import validate_config
from pg_router.model.topology import TopologyResolver
from pg_router.nginx.generator import ConfigGenerator

BASE = """
version: 1
listeners:
  - {id: web, address: 0.0.0.0, port: 443, mode: http, tls: {mode: disabled}}
backends:
  - {id: primary, type: local, host: 10.0.0.10, port: 62050}
  - {id: secondary, type: local, host: 10.0.0.20, port: 62051}
"""


def upstreams(text: str) -> str:
    config = parse_config_string(text)
    return ConfigGenerator(config, TopologyResolver(config)).generate().fragments["upstreams.conf"]


def endpoints(text: str):
    config = parse_config_string(text)
    return TopologyResolver(config).resolve_route(config.routes[0]).endpoints


def test_fallback_becomes_backup_upstream_members():
    text = BASE + """
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: primary
    fallback: spare
  - id: spare
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /spare}
    backend: secondary
"""
    fragment = upstreams(text)
    assert "server 10.0.0.10:62050" in fragment
    assert "server 10.0.0.20:62051 backup max_fails=3 fail_timeout=10s;" in fragment


def test_fallback_resolves_to_backup_role():
    text = BASE + """
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: primary
    fallback: spare
  - id: spare
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /spare}
    backend: secondary
"""
    roles = [endpoint.role for endpoint in endpoints(text)]
    assert roles == ["primary", "backup"]


def test_fallback_through_a_tunnel_backend():
    text = BASE + """
backends:
  - {id: tun, type: tunnel, tunnel: rev}
  - {id: secondary, type: local, host: 10.0.0.20, port: 62051}
tunnels:
  - id: rev
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: tun
    fallback: spare
  - id: spare
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /spare}
    backend: secondary
"""
    fragment = upstreams(text)
    assert "server 127.0.0.1:41001" in fragment
    assert "server 10.0.0.20:62051 backup max_fails=3 fail_timeout=10s;" in fragment


def test_validator_accepts_a_consistent_fallback():
    text = BASE + """
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: primary
    fallback: spare
  - id: spare
    listener: web
    transport: {type: ws}
    backend: secondary
"""
    report = validate_config(parse_config_string(text))
    assert report.valid, [str(p) for p in report.errors]


def test_disabled_fallback_is_rejected():
    text = BASE + """
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: primary
    fallback: spare
  - id: spare
    listener: web
    transport: {type: ws}
    backend: secondary
    enabled: false
"""
    report = validate_config(parse_config_string(text))
    assert not report.valid
    assert any("is disabled" in str(p) for p in report.errors)


def test_cross_layer_fallback_is_rejected():
    """A fallback shares one nginx upstream block, so it must speak the same
    transport layer as the route (an HTTP upstream cannot carry a raw TCP
    backend and vice versa)."""
    text = BASE + """
listeners:
  - {id: web, address: 0.0.0.0, port: 443, mode: http, tls: {mode: disabled}}
  - {id: raw, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: primary
    fallback: spare
  - id: spare
    listener: raw
    transport: {type: tcp}
    backend: secondary
"""
    report = validate_config(parse_config_string(text))
    assert not report.valid
    assert any("transport layer" in str(p) for p in report.errors)


def test_fallback_with_a_matcher_warns():
    """A backup target is reached by upstream selection, which ignores
    matchers entirely — so a matcher on the fallback route is misleading."""
    text = BASE + """
routes:
  - id: main
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /nl}
    backend: primary
    fallback: spare
  - id: spare
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /spare}
    backend: secondary
"""
    report = validate_config(parse_config_string(text))
    assert report.valid
    assert any("ignored while it serves as a backup target" in str(p)
               for p in report.warnings)


def test_fallback_chain_loops_are_still_rejected():
    text = BASE + """
routes:
  - id: a
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /a}
    backend: primary
    fallback: b
  - id: b
    listener: web
    transport: {type: ws}
    match: {type: path_prefix, value: /b}
    backend: secondary
    fallback: a
"""
    report = validate_config(parse_config_string(text))
    assert not report.valid
    assert any("fallback chain loops" in str(p) for p in report.errors)
