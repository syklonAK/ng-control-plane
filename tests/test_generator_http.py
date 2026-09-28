"""Nginx HTTP fragment generation tests."""

from __future__ import annotations

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.schema import RouterConfig
from pg_router.model.topology import TopologyResolver
from pg_router.nginx.generator import ConfigGenerator

CERT = """
certificates:
  - id: c1
    provider: existing
    chain: /certs/fullchain.pem
    key: /certs/privkey.pem
"""


def generate(text: str) -> str:
    config = RouterConfig.from_dict(_load(text))
    return ConfigGenerator(config, TopologyResolver(config)).generate().fragments.get("http.conf", "")


def _load(text: str) -> dict:
    import yaml

    return yaml.safe_load(text)


def test_websocket_location_headers(single_node_config):
    http = ConfigGenerator(single_node_config).generate().fragments["http.conf"]
    assert "location /" in http
    assert "proxy_http_version 1.1;" in http
    assert 'proxy_set_header Connection "upgrade";' in http
    assert "proxy_set_header Upgrade $http_upgrade;" in http
    assert "proxy_read_timeout 1w;" in http


def test_tls_termination_directives(single_node_config):
    http = ConfigGenerator(single_node_config).generate().fragments["http.conf"]
    assert "ssl_certificate /etc/pg-router/certs/fullchain.pem;" in http
    assert "ssl_certificate_key /etc/pg-router/certs/privkey.pem;" in http
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in http


def test_plain_listener_has_no_ssl_directives():
    text = f"""
version: 1
{CERT}
listeners:
  - id: plain
    address: 0.0.0.0
    port: 8080
    mode: http
    tls: {{mode: disabled}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: r1
    listener: plain
    transport: {{type: http}}
    match: {{type: path_prefix, value: /x}}
    backend: b1
"""
    http = generate(text)
    assert "ssl_certificate" not in http
    assert "listen 0.0.0.0:8080;" in http


def test_exact_and_regex_locations():
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: exact
    listener: https
    transport: {{type: ws}}
    match: {{type: path, value: /exact}}
    backend: b1
  - id: regex
    listener: https
    transport: {{type: ws}}
    match: {{type: path_regex, value: ^/dynamic/}}
    backend: b1
"""
    http = generate(text)
    assert "location = /exact {" in http
    assert "location ~ ^/dynamic/ {" in http


def test_xhttp_transport_disables_buffering():
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: xh
    listener: https
    transport: {{type: xhttp, path: /x}}
    match: {{type: path_prefix, value: /x}}
    backend: b1
"""
    http = generate(text)
    assert 'proxy_set_header Connection "";' in http
    assert "chunked_transfer_encoding on;" in http
    assert "proxy_buffering off;" in http


def test_grpc_transport_uses_grpc_pass():
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: g
    listener: https
    transport: {{type: grpc, service: my.Service}}
    match: {{type: path_prefix, value: /g}}
    backend: b1
"""
    http = generate(text)
    assert "grpc_pass grpc://pg_g;" in http
    assert "http2" in http


def test_multiple_paths_on_same_host_share_server_block():
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: a
    listener: https
    transport: {{type: ws}}
    match:
      all:
        - {{type: host, value: example.com}}
        - {{type: path_prefix, value: /nl}}
    backend: b1
  - id: b
    listener: https
    transport: {{type: ws}}
    match:
      all:
        - {{type: host, value: example.com}}
        - {{type: path_prefix, value: /fr}}
    backend: b1
"""
    http = generate(text)
    assert http.count("server {") == 1
    assert "server_name example.com;" in http
    assert "location /nl" in http and "location /fr" in http


def test_host_route_and_default_route_get_separate_servers():
    """A route bound to a host must not be merged into the catch-all server."""
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: a
    listener: https
    transport: {{type: ws}}
    match: {{type: host, value: a.example.com}}
    backend: b1
  - id: b
    listener: https
    transport: {{type: ws}}
    match: {{type: path_prefix, value: /nl}}
    backend: b1
"""
    http = generate(text)
    assert http.count("server {") == 2
    assert "server_name a.example.com;" in http
    assert "server_name _;" in http


def test_different_hosts_get_separate_server_blocks():
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: a
    listener: https
    transport: {{type: ws}}
    match: {{type: host, value: a.example.com}}
    backend: b1
  - id: b
    listener: https
    transport: {{type: ws}}
    match: {{type: host, value: b.example.com}}
    backend: b1
"""
    http = generate(text)
    assert http.count("server {") == 2


def test_shared_connection_upgrade_map_is_single():
    """The duplicate-map failure mode must never be reproduced."""
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
  - {{id: b2, type: local, host: 127.0.0.1, port: 62051}}
routes:
  - id: a
    listener: https
    transport: {{type: ws}}
    match: {{type: path_prefix, value: /a}}
    backend: b1
  - id: b
    listener: https
    transport: {{type: ws}}
    match: {{type: path_prefix, value: /b}}
    backend: b2
"""
    result = ConfigGenerator(parse_config_string(text)).generate()
    assert result.fragments["maps.conf"].count("map $http_upgrade $connection_upgrade {") == 1


def test_custom_proxy_headers_are_emitted():
    text = f"""
version: 1
{CERT}
listeners:
  - id: https
    address: 0.0.0.0
    port: 443
    mode: http
    tls: {{mode: terminate, certificate: c1}}
defaults:
  proxy_headers:
    X-Pool: edge-01
backends:
  - {{id: b1, type: local, host: 127.0.0.1, port: 62050}}
routes:
  - id: a
    listener: https
    transport: {{type: ws, headers: {{X-Tag: nl}}}}
    match: {{type: path_prefix, value: /a}}
    backend: b1
"""
    http = generate(text)
    assert "proxy_set_header X-Pool edge-01;" in http
    assert "proxy_set_header X-Tag nl;" in http
