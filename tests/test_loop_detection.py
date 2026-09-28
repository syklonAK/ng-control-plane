"""Loop detection tests: chains, failover groups, tunnel wiring, fallbacks."""

from __future__ import annotations

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.validator import validate_config


def errors_of(text: str) -> list[str]:
    report = validate_config(parse_config_string(text))
    return [problem.message for problem in report.errors]


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
"""


def test_chain_with_repeated_hop_is_detected():
    text = BASE + """
chains:
  - id: loop
    chain:
      - {type: listener, id: https}
      - {type: tunnel, id: rev}
      - {type: listener, id: https}
"""
    assert any("loop" in error for error in errors_of(text))


def test_chain_with_unknown_hop_is_detected():
    text = BASE + """
chains:
  - id: bad
    chain:
      - {type: tunnel, id: nope}
"""
    assert any("unknown tunnel" in error for error in errors_of(text))


def test_failover_self_reference_is_detected():
    text = BASE + """
backends:
  - {id: group, type: failover, primary: group}
"""
    assert any("references itself" in error for error in errors_of(text))


def test_failover_cycle_is_detected():
    text = BASE + """
backends:
  - {id: a, type: failover, primary: b}
  - {id: b, type: failover, primary: c}
  - {id: c, type: failover, primary: a}
"""
    assert any("failover loop" in error for error in errors_of(text))


def test_tunnel_loop_is_detected():
    # Each tunnel's egress (target) lands on the other tunnel's ingress
    # (reverse listener), so traffic cycles between them forever.
    text = """
version: 1
listeners:
  - {id: stream, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: ta, type: tunnel, tunnel: a}
tunnels:
  - id: a
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
    target: {host: 127.0.0.1, port: 41002}
  - id: b
    mode: reverse
    listener: {address: 127.0.0.1, port: 41002}
    target: {host: 127.0.0.1, port: 41001}
"""
    assert any("tunnel topology contains a loop" in error for error in errors_of(text))


def test_tunnel_chain_without_loop_is_allowed():
    text = """
version: 1
listeners:
  - {id: stream, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: ta, type: tunnel, tunnel: a}
tunnels:
  - id: a
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
    target: {host: 127.0.0.1, port: 41002}
  - id: b
    mode: reverse
    listener: {address: 127.0.0.1, port: 41002}
    target: {host: 127.0.0.1, port: 62050}
"""
    assert validate_config(parse_config_string(text)).valid is True


def test_fallback_cycle_is_detected():
    text = BASE + """
routes:
  - id: r1
    listener: https
    transport: {type: ws}
    match: {type: path_prefix, value: /a}
    backend: local
    fallback: r2
  - id: r2
    listener: https
    transport: {type: ws}
    match: {type: path_prefix, value: /b}
    backend: local
    fallback: r1
"""
    assert any("fallback chain loops" in error for error in errors_of(text))


def test_backend_tunnel_self_loop_is_detected():
    # Tunnel's target is the backend that proxies into the tunnel itself.
    text = BASE + """
backends:
  - id: tun
    type: tunnel
    tunnel: rev
tunnels:
  - id: rev
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
    target: {host: 127.0.0.1, port: 62050}
"""
    assert validate_config(parse_config_string(text)).valid is True


def test_valid_chain_has_no_loop():
    text = BASE + """
chains:
  - id: ok
    chain:
      - {type: listener, id: https}
      - {type: tunnel, id: rev}
      - {type: backend, id: local}
"""
    assert validate_config(parse_config_string(text)).valid is True
