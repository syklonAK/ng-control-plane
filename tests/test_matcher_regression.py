"""End-to-end regression tests for the matcher-compilation fixes.

Each test below corresponds to a bug that was actually present: the config was
accepted, and the generated nginx silently behaved differently from what the
simulator (and the documentation) promised.
"""

from __future__ import annotations

from pg_router.config.loader import parse_config_string
from pg_router.config.validator import validate_config
from pg_router.model.matcher import MatcherEngine, RequestContext
from pg_router.model.topology import TopologyResolver
from pg_router.nginx.generator import ConfigGenerator

STREAM_BASE = """
version: 1
listeners:
  - {id: s, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: a, type: local, host: 10.0.0.10, port: 62050}
  - {id: b, type: local, host: 10.0.0.20, port: 62051}
"""

HTTP_BASE = """
version: 1
listeners:
  - {id: web, address: 0.0.0.0, port: 443, mode: http, tls: {mode: disabled}}
backends:
  - {id: a, type: local, host: 10.0.0.10, port: 62050}
  - {id: b, type: local, host: 10.0.0.20, port: 62051}
"""


def generate(text: str):
    config = parse_config_string(text)
    return ConfigGenerator(config, TopologyResolver(config)).generate()


def test_alpn_route_takes_effect_end_to_end():
    """Before the fix the ALPN map was generated but the stream `server` still
    proxied through the SNI variable, so every ALPN route was dead config."""
    text = STREAM_BASE + """
routes:
  - {id: h2, listener: s, transport: {type: tcp}, match: {type: alpn, value: h2}, backend: a}
  - {id: d, listener: s, transport: {type: tcp}, backend: b}
"""
    result = generate(text)
    stream = result.fragments["stream.conf"]
    maps = result.fragments["maps.conf"]
    assert "proxy_pass $pg_alpn_s;" in stream
    assert "$pg_sni_s" not in stream
    assert "map $ssl_preread_alpn_protocols $pg_alpn_s {" in maps
    assert result.ok, result.errors


def test_stream_listener_without_sni_or_alpn_does_not_reference_a_missing_map():
    """A matcher-less default route has no map, so the server must proxy to the
    upstream directly. Referencing an undefined `$pg_sni_...` variable made
    `nginx -t` fail for the simplest possible stream topology."""
    text = STREAM_BASE + """
routes:
  - {id: d, listener: s, transport: {type: tcp}, backend: a}
"""
    result = generate(text)
    assert result.ok, result.errors
    stream = result.fragments["stream.conf"]
    assert "proxy_pass pg_d;" in stream
    assert "pg_sni" not in stream and "pg_alpn" not in stream


def test_any_paths_reach_nginx_and_stay_simulatable():
    text = HTTP_BASE + """
routes:
  - id: r
    listener: web
    transport: {type: ws}
    match:
      any:
        - {type: path, value: /x}
        - {type: path, value: /y}
    backend: a
"""
    result = generate(text)
    assert result.ok, result.errors
    http = result.fragments["http.conf"]
    assert "location = /x {" in http and "location = /y {" in http

    # The simulator agrees: both paths match, nothing else does.
    config = parse_config_string(text)
    engine = MatcherEngine()
    matcher = config.routes[0].match
    assert engine.evaluate(matcher, RequestContext(path="/x"))
    assert engine.evaluate(matcher, RequestContext(path="/y"))
    assert not engine.evaluate(matcher, RequestContext(path="/z"))


def test_not_matcher_is_refused_before_it_can_invert_routing():
    """A negated SNI matcher used to be flattened into a positive entry, so a
    host the operator excluded was routed *to* that route."""
    text = STREAM_BASE + """
routes:
  - {id: r, listener: s, transport: {type: tcp}, match: {not: {type: sni, value: bad.example.com}}, backend: a}
  - {id: d, listener: s, transport: {type: tcp}, backend: b}
"""
    report = validate_config(parse_config_string(text))
    assert not report.valid
    assert any("negated matchers" in str(problem) for problem in report.errors)


def test_source_cidr_restriction_is_never_silently_dropped():
    """The operator believes the route is restricted to a CIDR. Before the fix
    the generated nginx had no restriction at all and said nothing about it."""
    text = HTTP_BASE + """
routes:
  - id: r
    listener: web
    transport: {type: ws}
    match: {type: source_cidr, value: 10.0.0.0/8}
    backend: a
"""
    report = validate_config(parse_config_string(text))
    assert not report.valid
    assert any("not enforced in generated nginx" in str(problem) for problem in report.errors)


def test_simulation_only_matcher_still_works_for_route_testing():
    """Refusing the matcher at deploy time must not weaken `pg-router routes
    test`, which is where simulation-only matchers are useful."""
    text = HTTP_BASE + """
routes:
  - id: r
    listener: web
    transport: {type: ws}
    match:
      all:
        - {type: path_prefix, value: /nl}
        - {type: source_cidr, value: 10.0.0.0/8}
    backend: a
    unenforced_matchers: allow
"""
    config = parse_config_string(text)
    report = validate_config(config)
    assert report.valid, [str(p) for p in report.errors]

    engine = MatcherEngine()
    matcher = config.routes[0].match
    assert engine.evaluate(matcher, RequestContext(path="/nl/1", source_ip="10.1.1.1"))
    assert not engine.evaluate(matcher, RequestContext(path="/nl/1", source_ip="8.8.8.8"))
    assert not engine.evaluate(matcher, RequestContext(path="/fr/1", source_ip="10.1.1.1"))

    # And nginx gets exactly the enforceable part.
    result = generate(text)
    assert result.ok, result.errors
    assert "location /nl" in result.fragments["http.conf"]
