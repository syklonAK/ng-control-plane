"""Tunnel abstraction and provider tests."""

from __future__ import annotations

import pytest

from pg_router.config.loader import parse_config_string
from pg_router.config.schema import RouterConfig, Tunnel
from pg_router.model.topology import TopologyResolver
from pg_router.plugins.providers import (
    CustomTunnelProvider,
    GostProvider,
    ReverseTunnelProvider,
    UnixSocketTunnelProvider,
    register_builtin_providers,
)
from pg_router.plugins.registry import ProviderRegistry, tunnels as tunnel_registry
from pg_router.utils.security import ValidationError


def test_reverse_tunnel_resolves_to_local_listener():
    tunnel = Tunnel.from_dict(
        {
            "id": "rev",
            "mode": "reverse",
            "listener": {"address": "127.0.0.1", "port": 41001},
        }
    )
    assert tunnel.endpoint().host == "127.0.0.1"
    assert tunnel.endpoint().port == 41001


def test_direct_tunnel_resolves_to_remote():
    tunnel = Tunnel.from_dict(
        {
            "id": "dir",
            "mode": "direct",
            "remote": {"host": "10.0.0.5", "port": 40001},
        }
    )
    assert tunnel.endpoint().host == "10.0.0.5"
    assert tunnel.endpoint().port == 40001


def test_builtin_providers_are_registered():
    register_builtin_providers(tunnel_registry, ProviderRegistry())
    registry = ProviderRegistry()
    register_builtin_providers(registry, ProviderRegistry())
    # registry must contain all documented provider names
    for name in ("direct", "reverse", "gost", "ssh", "wireguard", "tcp-relay", "unix-socket", "custom"):
        registry.register(_dummy(name))
    assert registry.names() == sorted(
        ("direct", "reverse", "gost", "ssh", "wireguard", "tcp-relay", "unix-socket", "custom")
    )


def _dummy(name):
    class _P:
        pass

    _P.name = name
    return _P()


def test_custom_provider_honours_options_endpoint():
    tunnel = Tunnel.from_dict(
        {
            "id": "c",
            "mode": "reverse",
            "provider": "custom",
            "listener": {"address": "127.0.0.1", "port": 41001},
            "options": {"endpoint_host": "169.254.1.1", "endpoint_port": 41010},
        }
    )
    endpoint = CustomTunnelProvider().endpoint(tunnel)
    assert (endpoint.host, endpoint.port) == ("169.254.1.1", 41010)


def test_custom_provider_falls_back_to_direction():
    tunnel = Tunnel.from_dict(
        {
            "id": "c",
            "mode": "direct",
            "provider": "custom",
            "remote": {"host": "10.0.0.9", "port": 40001},
        }
    )
    endpoint = CustomTunnelProvider().endpoint(tunnel)
    assert endpoint.host == "10.0.0.9"


def test_unix_socket_provider_requires_socket_option():
    tunnel = Tunnel.from_dict(
        {"id": "u", "mode": "direct", "provider": "unix-socket", "remote": {"host": "10.0.0.9", "port": 1}}
    )
    with pytest.raises(ValueError):
        UnixSocketTunnelProvider().endpoint(tunnel)

    with_socket = Tunnel.from_dict(
        {
            "id": "u2",
            "mode": "direct",
            "provider": "unix-socket",
            "remote": {"host": "10.0.0.9", "port": 1},
            "options": {"endpoint_socket": "/tmp/tunnel.sock"},
        }
    )
    assert UnixSocketTunnelProvider().endpoint(with_socket).socket == "/tmp/tunnel.sock"


def test_provider_describe_reports_metadata():
    tunnel = Tunnel.from_dict(
        {
            "id": "g",
            "mode": "direct",
            "provider": "gost",
            "remote": {"host": "10.0.0.9", "port": 40001},
            "target": {"host": "127.0.0.1", "port": 62050},
        }
    )
    describe = GostProvider().describe(tunnel)
    assert describe["provider"] == "gost"
    assert describe["endpoint"] == "10.0.0.9:40001"
    assert describe["target"] == "127.0.0.1:62050"


def test_backend_resolution_through_tunnel_and_failover():
    text = """
version: 1
listeners:
  - {id: stream, address: 0.0.0.0, port: 8443, mode: stream, tls: {mode: passthrough}}
backends:
  - {id: tun, type: tunnel, tunnel: rev}
  - {id: direct, type: remote, host: 10.0.0.20, port: 62050}
  - {id: group, type: failover, primary: tun, backups: [direct]}
tunnels:
  - id: rev
    mode: reverse
    listener: {address: 127.0.0.1, port: 41001}
"""
    config = parse_config_string(text)
    resolver = TopologyResolver(config)

    tunnel_endpoints = resolver.resolve_backend("tun")
    assert tunnel_endpoints[0].address == "127.0.0.1:41001"
    assert tunnel_endpoints[0].tunnel_id == "rev"

    group_endpoints = resolver.resolve_backend("group")
    assert group_endpoints[0].address == "127.0.0.1:41001"
    assert group_endpoints[0].role == "primary"
    assert group_endpoints[1].address == "10.0.0.20:62050"
    assert group_endpoints[1].role == "backup"


def test_unix_socket_backend_resolution():
    text = """
version: 1
listeners: []
backends:
  - {id: sock, type: unix_socket, socket: /tmp/pg.sock}
"""
    config = parse_config_string(text)
    resolver = TopologyResolver(config)
    endpoints = resolver.resolve_backend("sock")
    assert endpoints[0].address == "unix:/tmp/pg.sock"


def test_resolution_loop_is_rejected():
    text = """
version: 1
listeners: []
backends:
  - {id: a, type: failover, primary: b}
  - {id: b, type: failover, primary: a}
"""
    config = parse_config_string(text)
    resolver = TopologyResolver(config)
    with pytest.raises(ValidationError):
        resolver.resolve_backend("a")


def test_reverse_tunnel_provider_endpoint():
    tunnel = Tunnel.from_dict(
        {"id": "r", "mode": "reverse", "listener": {"address": "127.0.0.1", "port": 41001}}
    )
    assert ReverseTunnelProvider().endpoint(tunnel).port == 41001
    assert ReverseTunnelProvider().health_endpoint(tunnel) is not None
