"""Nginx stream fragment and SNI/ALPN routing tests."""

from __future__ import annotations

from pg_router.config.loader import parse_config_string
from pg_router.config.schema import RouterConfig
from pg_router.model.topology import TopologyResolver
from pg_router.nginx.generator import ConfigGenerator

BASE = """
version: 1
listeners:
  - id: stream
    address: 0.0.0.0
    port: 8443
    mode: stream
    tls: {mode: passthrough}
backends:
  - {id: nl, type: local, host: 10.0.0.10, port: 62050}
  - {id: fr, type: local, host: 10.0.0.20, port: 62051}
"""


def generate(text: str):
    config = RouterConfig.from_dict(_load(text))
    result = ConfigGenerator(config, TopologyResolver(config)).generate()
    return result.fragments


def _load(text: str) -> dict:
    import yaml

    return yaml.safe_load(text)


def test_sni_routing_emits_preread_and_map():
    text = BASE + """
tunnels: []
routes:
  - id: nl-tcp
    listener: stream
    transport: {type: tcp}
    match: {type: sni, values: [nl.example.com, nl2.example.com]}
    backend: nl
  - id: fr-tcp
    listener: stream
    transport: {type: tcp}
    match: {type: sni, value: fr.example.com}
    backend: fr
"""
    fragments = generate(text)
    stream = fragments["stream.conf"]
    maps = fragments["maps.conf"]
    assert "ssl_preread on;" in stream
    assert "proxy_pass $pg_sni_stream;" in stream
    assert "nl.example.com pg_nl_tcp;" in maps
    assert "nl2.example.com pg_nl_tcp;" in maps
    assert "fr.example.com pg_fr_tcp;" in maps
    # Exactly one map per variable (never the duplicate-map failure mode).
    assert maps.count("map $ssl_preread_server_name $pg_sni_stream {") == 1


def test_unknown_sni_policy_reject_uses_blackhole():
    text = BASE + """
routes:
  - id: nl-tcp
    listener: stream
    transport: {type: tcp}
    match: {type: sni, value: nl.example.com}
    backend: nl
"""
    fragments = generate(text)
    assert "default pg_blackhole;" in fragments["maps.conf"]
    assert "server 127.0.0.1:9 down;" in fragments["upstreams.conf"]


def test_unknown_sni_policy_default_routes_to_default_backend():
    text = BASE.replace(
        "tls: {mode: passthrough}",
        "unknown_policy: default\ntls: {mode: passthrough}",
    ) + """
routes:
  - id: nl-tcp
    listener: stream
    transport: {type: tcp}
    match: {type: sni, value: nl.example.com}
    backend: nl
  - id: catchall
    listener: stream
    transport: {type: tcp}
    backend: fr
"""
    fragments = generate(text)
    assert "default pg_catchall;" in fragments["maps.conf"]


def test_alpn_routing_emits_alpn_map():
    text = BASE + """
routes:
  - id: h2-route
    listener: stream
    transport: {type: tcp}
    match: {type: alpn, value: h2}
    backend: nl
"""
    fragments = generate(text)
    assert "map $ssl_preread_alpn_protocols $pg_alpn_stream {" in fragments["maps.conf"]
    assert "~h2 pg_h2_route;" in fragments["maps.conf"]


def test_two_default_routes_on_one_stream_listener_is_an_error():
    text = BASE + """
routes:
  - id: a
    listener: stream
    transport: {type: tcp}
    backend: nl
  - id: b
    listener: stream
    transport: {type: tcp}
    backend: fr
"""
    config = RouterConfig.from_dict(_load(text))
    result = ConfigGenerator(config, TopologyResolver(config)).generate()
    assert any("only one default route per stream listener" in error for error in result.errors)


def test_sni_matcher_on_plaintext_listener_is_an_error():
    text = """
version: 1
listeners:
  - id: stream
    address: 0.0.0.0
    port: 8443
    mode: stream
    tls: {mode: disabled}
backends:
  - {id: nl, type: local, host: 10.0.0.10, port: 62050}
routes:
  - id: a
    listener: stream
    transport: {type: tcp}
    match: {type: sni, value: nl.example.com}
    backend: nl
"""
    config = RouterConfig.from_dict(_load(text))
    result = ConfigGenerator(config, TopologyResolver(config)).generate()
    assert any("ssl_preread cannot read SNI from plaintext" in error for error in result.errors)


def test_no_stream_routes_emits_placeholder_fragment():
    text = """
version: 1
listeners:
  - {id: https, address: 0.0.0.0, port: 443, mode: http, tls: {mode: disabled}}
backends:
  - {id: b, type: local, host: 127.0.0.1, port: 62050}
routes:
  - id: r
    listener: https
    transport: {type: ws}
    match: {type: path_prefix, value: /x}
    backend: b
"""
    config = RouterConfig.from_dict(_load(text))
    result = ConfigGenerator(config, TopologyResolver(config)).generate()
    assert "no stream routes configured" in result.fragments["stream.conf"]
    assert "no routes" not in result.fragments["http.conf"]


def test_stream_upstream_uses_tunnel_endpoint():
    text = """
version: 1
listeners:
  - {id: stream, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: tun, type: tunnel, tunnel: rev}
tunnels:
  - id: rev
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
routes:
  - id: r
    listener: stream
    transport: {type: tcp}
    match: {type: sni, value: x.example.com}
    backend: tun
"""
    config = RouterConfig.from_dict(_load(text))
    result = ConfigGenerator(config, TopologyResolver(config)).generate()
    assert "server 127.0.0.1:41001" in result.fragments["upstreams.conf"]
    assert "pg_sni_stream" in result.fragments["maps.conf"]
