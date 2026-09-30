"""Capability contract and matcher-compilation regression tests.

These cover the class of bug where the simulator, the validator and the nginx
generator disagreed about what a matcher means: ALPN routing that never took
effect, `any:` paths that silently collapsed to the last one, negated matchers
that inverted stream routing, and simulation-only matchers (source_ip,
source_cidr, port, ...) that vanished from generated config without a word.
"""

from __future__ import annotations

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.validator import validate_config
from pg_router.model.capabilities import (
    HTTP,
    STREAM,
    capability,
    enforced_types,
    simulation_only_types,
)
from pg_router.model.selection import (
    MatcherCompileError,
    compile_route_match,
)
from pg_router.model.topology import TopologyResolver
from pg_router.nginx.generator import ConfigGenerator

BASE_HTTP = """
version: 1
listeners:
  - {id: web, address: 0.0.0.0, port: 443, mode: http, tls: {mode: disabled}}
backends:
  - {id: b, type: local, host: 127.0.0.1, port: 62050}
"""

BASE_STREAM = """
version: 1
listeners:
  - {id: s, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: b, type: local, host: 10.0.0.10, port: 62050}
"""


def http_route(matcher, **extra) -> str:
    return _route_yaml(BASE_HTTP, "web", "ws", matcher, extra)


def stream_route(matcher, **extra) -> str:
    return _route_yaml(BASE_STREAM, "s", "tcp", matcher, extra)


def _route_yaml(base: str, listener: str, transport: str, matcher, extra: dict) -> str:
    import yaml

    if isinstance(matcher, str):
        matcher = yaml.safe_load(matcher)
    route = {"id": "r", "listener": listener, "transport": {"type": transport},
             "match": matcher, "backend": "b"}
    route.update(extra)
    return base + "routes:\n" + yaml.safe_dump([route], sort_keys=False)


def compile_first(text: str, mode: str = HTTP, **kwargs):
    config = parse_config_string(text)
    route = config.routes[0]
    kwargs.setdefault("allow_unenforced", route.unenforced_matchers == "allow")
    return compile_route_match(route, mode, **kwargs)


def gen(text: str):
    config = parse_config_string(text)
    return ConfigGenerator(config, TopologyResolver(config)).generate()


# ---------------------------------------------------------------------------
# capability table
# ---------------------------------------------------------------------------


def test_capability_table_separates_enforced_from_simulation_only():
    enforced = enforced_types()
    simulated = simulation_only_types()
    assert enforced == {"path", "path_prefix", "path_regex", "host", "host_regex", "sni", "alpn"}
    assert simulated == {
        "port", "destination_port", "protocol", "transport", "source_ip", "source_cidr",
    }
    assert not (enforced & simulated)


def test_enforced_matchers_are_bound_to_their_layer():
    assert capability("path").enforced_in(HTTP)
    assert not capability("path").enforced_in(STREAM)
    assert capability("sni").enforced_in(STREAM)
    assert not capability("sni").enforced_in(HTTP)
    assert not capability("source_cidr").enforced  # never enforced anywhere


# ---------------------------------------------------------------------------
# compound matcher compilation
# ---------------------------------------------------------------------------


def test_all_of_host_and_path_compiles():
    selection = compile_first(http_route(
        "all: [{type: host, value: example.com}, {type: path_prefix, value: /nl}]"
    ))
    http = selection.http
    assert http.hosts and http.hosts[0].value == "example.com"
    assert [loc.value for loc in http.locations] == ["/nl"]


def test_any_of_paths_compiles_to_multiple_locations():
    """The bug: `any:` kept only the last path; the others were unreachable."""
    selection = compile_first(http_route(
        "any: [{type: path, value: /x}, {type: path, value: /y}]"
    ))
    assert sorted(loc.value for loc in selection.http.locations) == ["/x", "/y"]


def test_any_of_paths_reaches_nginx_as_two_locations():
    fragments = gen(http_route(
        "any: [{type: path, value: /x}, {type: path, value: /y}]"
    )).fragments["http.conf"]
    assert "location = /x {" in fragments
    assert "location = /y {" in fragments


def test_any_of_stream_values_compiles():
    selection = compile_first(stream_route(
        "any: [{type: sni, value: a.example.com}, {type: sni, value: b.example.com}]"
    ), mode=STREAM)
    assert sorted(selection.stream.values) == ["a.example.com", "b.example.com"]


def test_any_across_different_types_is_refused():
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(http_route(
            "any: [{type: host, value: a.com}, {type: path_prefix, value: /x}]"
        ))
    assert "different matcher types" in str(excinfo.value)


def test_nested_compound_is_refused():
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(http_route(
            "all: [{type: host, value: a.com}, {any: [{type: path, value: /x}]}]"
        ))
    assert "nested inside" in str(excinfo.value)


def test_not_matcher_is_refused_everywhere():
    """The bug: `not:` was flattened into a *positive* match, so a host an
    operator excluded was routed to that very route."""
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(stream_route("not: {type: sni, value: bad.example.com}"), mode=STREAM)
    assert "negated matchers" in str(excinfo.value)


def test_repeated_type_under_all_is_refused():
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(http_route(
            "all: [{type: host, value: a.com}, {type: host, value: b.com}]"
        ))
    assert "repeats matcher type" in str(excinfo.value)


def test_combining_two_path_matchers_is_refused():
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(http_route(
            "all: [{type: path, value: /x}, {type: path_prefix, value: /y}]"
        ))
    assert "only one path matcher" in str(excinfo.value)


def test_combined_sni_and_alpn_on_one_route_is_refused():
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(stream_route(
            "all: [{type: sni, value: a.com}, {type: alpn, value: h2}]"
        ), mode=STREAM)
    assert "cannot combine SNI and ALPN" in str(excinfo.value)


# ---------------------------------------------------------------------------
# simulation-only matchers must be acknowledged
# ---------------------------------------------------------------------------


def test_source_cidr_on_http_is_refused_by_default():
    """The security bug: the CIDR restriction silently vanished from the
    generated config, leaving the route open to everyone, with no warning."""
    with pytest.raises(MatcherCompileError) as excinfo:
        compile_first(http_route("{type: source_cidr, value: 10.0.0.0/8}"))
    message = str(excinfo.value)
    assert "not enforced in generated nginx config" in message
    assert "source_cidr" in message
    assert "unenforced_matchers" in message


def test_source_cidr_allowed_for_simulation_only():
    selection = compile_first(
        http_route("{type: source_cidr, value: 10.0.0.0/8}", unenforced_matchers="allow")
    )
    assert selection.http.unenforced == ("source_cidr",)
    assert not selection.http.locations  # no nginx location from it


def test_unenforced_matcher_rejected_by_validator():
    config = parse_config_string(http_route("{type: source_ip, value: 10.0.0.1}"))
    report = validate_config(config)
    assert not report.valid
    assert any("not enforced in generated nginx" in str(p) for p in report.errors)


def test_unenforced_matcher_allowed_by_validator_when_acknowledged():
    config = parse_config_string(
        http_route("{type: source_ip, value: 10.0.0.1}", unenforced_matchers="allow")
    )
    report = validate_config(config)
    assert report.valid, [str(p) for p in report.errors]


def test_simulator_still_evaluates_unenforced_matchers():
    """Simulation keeps full tree semantics even for matchers nginx cannot
    express: that is the whole point of the capability split."""
    from pg_router.model.matcher import MatcherEngine, RequestContext

    config = parse_config_string(
        http_route("{type: source_cidr, value: 10.0.0.0/8}", unenforced_matchers="allow")
    )
    engine = MatcherEngine()
    matcher = config.routes[0].match
    assert engine.evaluate(matcher, RequestContext(source_ip="10.1.2.3")) is True
    assert engine.evaluate(matcher, RequestContext(source_ip="8.8.8.8")) is False


# ---------------------------------------------------------------------------
# listener-level constraints
# ---------------------------------------------------------------------------


def test_stream_listener_mixing_sni_and_alpn_routes_is_rejected():
    text = BASE_STREAM + """
routes:
  - {id: a, listener: s, transport: {type: tcp}, match: {type: sni, value: a.com}, backend: b}
  - {id: c, listener: s, transport: {type: tcp}, match: {type: alpn, value: h2}, backend: b}
"""
    report = validate_config(parse_config_string(text))
    assert not report.valid
    assert any("mix" in str(p) for p in report.errors)
