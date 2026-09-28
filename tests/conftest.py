"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.schema import RouterConfig


@pytest.fixture
def tmp_managed(tmp_path: Path) -> str:
    """A temporary managed-fragment directory."""
    directory = tmp_path / "pg-router"
    directory.mkdir()
    return str(directory)


@pytest.fixture
def single_node_config() -> RouterConfig:
    return parse_config_string(SINGLE_NODE)


@pytest.fixture
def edge_mixed_config() -> RouterConfig:
    return parse_config_string(EDGE_MIXED)


SINGLE_NODE = """
version: 1
node:
  id: single-01
  roles: [edge, backend]
defaults:
  read_timeout: 1w
  send_timeout: 1w
certificates:
  - id: self
    provider: existing
    chain: /etc/pg-router/certs/fullchain.pem
    key: /etc/pg-router/certs/privkey.pem
listeners:
  - id: public-https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {mode: terminate, certificate: self}
backends:
  - id: pg-local
    type: local
    host: 127.0.0.1
    port: 62050
routes:
  - id: ws-route
    listener: public-https
    transport: {type: ws}
    match: {type: path_prefix, value: /}
    backend: pg-local
"""

EDGE_MIXED = """
version: 1
node:
  id: edge-01
  roles: [edge, relay]
certificates:
  - id: wildcard
    provider: existing
    chain: /etc/letsencrypt/live/example.com/fullchain.pem
    key: /etc/letsencrypt/live/example.com/privkey.pem
listeners:
  - id: public-https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {mode: terminate, certificate: wildcard}
  - id: public-stream
    address: 0.0.0.0
    port: 8443
    mode: stream
    unknown_policy: reject
    tls: {mode: passthrough}
backends:
  - id: pg-nl
    type: tunnel
    tunnel: nl-reverse
  - id: pg-fr
    type: tunnel
    tunnel: fr-direct
  - id: pg-local
    type: local
    host: 127.0.0.1
    port: 62052
tunnels:
  - id: nl-reverse
    mode: reverse
    provider: custom
    listener: {address: 127.0.0.1, port: 41001}
    remote: {node: nl-node}
    target: {host: 127.0.0.1, port: 62050}
  - id: fr-direct
    mode: direct
    provider: custom
    remote: {host: 10.0.0.20, port: 40001}
    target: {host: 127.0.0.1, port: 62051}
routes:
  - id: nl-ws
    listener: public-https
    transport: {type: ws}
    match:
      all:
        - {type: host, value: example.com}
        - {type: path_prefix, value: /nl}
    backend: pg-nl
  - id: fr-tcp
    listener: public-stream
    transport: {type: tcp}
    match: {type: sni, value: fr.example.com}
    backend: pg-fr
  - id: local-default
    listener: public-stream
    transport: {type: tcp}
    backend: pg-local
"""


def load_example(name: str) -> RouterConfig:
    """Load one of the shipped example configurations."""
    root = Path(__file__).resolve().parent.parent
    return parse_config_string((root / "examples" / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def examples_dir() -> Path:
    root = Path(__file__).resolve().parent.parent / "examples"
    return root
