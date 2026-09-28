"""Validator tests: references, compatibility, TLS and uniqueness."""

from __future__ import annotations

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.validator import validate_config


def report_for(text: str):
    return validate_config(parse_config_string(text))


def problems_of(text: str) -> list[str]:
    report = report_for(text)
    return [f"{problem.object_id}: {problem.message}" for problem in report.problems]


BASE = """
version: 1
listeners:
  - {id: https, address: 0.0.0.0, port: 443, mode: http}
  - {id: stream, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: local, type: local, host: 127.0.0.1, port: 62050}
tunnels:
  - id: rev
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
routes: []
"""


def test_valid_base_configuration_is_clean():
    assert problems_of(BASE) == []


def test_unknown_listener_backend_and_tunnel_references():
    text = BASE + """
routes:
  - id: bad
    listener: nope
    transport: {type: ws}
    match: {type: path_prefix, value: /x}
    backend: missing
backends:
  - {id: tun, type: tunnel, tunnel: no-such-tunnel}
"""
    problems = problems_of(text)
    assert any("unknown listener" in p for p in problems)
    assert any("unknown backend" in p for p in problems)
    assert any("unknown tunnel" in p for p in problems)
    assert report_for(text).valid is False


def test_stream_transport_must_use_stream_listener():
    text = BASE + """
routes:
  - id: r1
    listener: https
    transport: {type: tcp}
    match: {type: sni, value: a.example.com}
    backend: local
"""
    problems = problems_of(text)
    assert any("needs a stream listener" in p for p in problems)


def test_http_transport_must_use_http_listener():
    text = BASE + """
routes:
  - id: r1
    listener: stream
    transport: {type: ws}
    match: {type: path_prefix, value: /x}
    backend: local
"""
    problems = problems_of(text)
    assert any("needs a http listener" in p for p in problems)


def test_sni_matcher_rejected_on_http_listener():
    text = BASE + """
routes:
  - id: r1
    listener: https
    transport: {type: ws}
    match: {type: sni, value: a.example.com}
    backend: local
"""
    problems = problems_of(text)
    assert any("need ssl_preread" in p for p in problems)


def test_path_matcher_rejected_on_stream_listener():
    text = BASE + """
routes:
  - id: r1
    listener: stream
    transport: {type: tcp}
    match: {type: path_prefix, value: /x}
    backend: local
"""
    problems = problems_of(text)
    assert any("cannot be used on a stream listener" in p for p in problems)


def test_tls_passthrough_rejected_on_http_listener():
    text = """
version: 1
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {mode: passthrough}
"""
    assert any("'passthrough' requires a stream listener" in p for p in problems_of(text))


def test_tls_terminate_rejected_on_stream_listener():
    text = """
version: 1
listeners:
  - id: stream
    address: 0.0.0.0
    port: 8443
    mode: stream
    tls: {mode: terminate}
"""
    assert any("not generated" in p for p in problems_of(text))


def test_duplicate_http_locations_are_rejected():
    text = BASE + """
routes:
  - {id: a, listener: https, transport: {type: ws}, match: {type: path_prefix, value: /nl}, backend: local}
  - {id: b, listener: https, transport: {type: ws}, match: {type: path_prefix, value: /nl}, backend: local}
"""
    problems = problems_of(text)
    assert any("ambiguous location" in p for p in problems)


def test_duplicate_sni_values_are_rejected():
    text = BASE + """
routes:
  - {id: a, listener: stream, transport: {type: tcp}, match: {type: sni, value: nl.example.com}, backend: local}
  - {id: b, listener: stream, transport: {type: tcp}, match: {type: sni, value: nl.example.com}, backend: local}
"""
    problems = problems_of(text)
    assert any("duplicate SNI" in p for p in problems)


def test_distinct_prefixes_are_allowed():
    text = BASE + """
routes:
  - {id: a, listener: https, transport: {type: ws}, match: {type: path_prefix, value: /nl}, backend: local}
  - {id: b, listener: https, transport: {type: ws}, match: {type: path_prefix, value: /fr}, backend: local}
"""
    assert report_for(text).valid is True


def test_listener_bind_conflict_is_detected():
    text = """
version: 1
listeners:
  - {id: a, address: 0.0.0.0, port: 443, mode: http}
  - {id: b, address: 0.0.0.0, port: 443, mode: http}
"""
    assert any("already used by listener" in p for p in problems_of(text))


def test_unknown_certificate_reference():
    text = BASE.replace(
        "tls: {mode: passthrough}", "tls: {mode: passthrough}"
    ) + """
routes:
  - id: r1
    listener: https
    transport: {type: ws}
    match: {type: path_prefix, value: /x}
    backend: local
    tls: {mode: terminate, certificate: no-cert}
"""
    assert any("unknown certificate" in p for p in problems_of(text))


def test_fallback_reference_to_unknown_route():
    text = BASE + """
routes:
  - id: r1
    listener: https
    transport: {type: ws}
    match: {type: path_prefix, value: /x}
    backend: local
    fallback: ghost
"""
    assert any("unknown fallback route" in p for p in problems_of(text))
