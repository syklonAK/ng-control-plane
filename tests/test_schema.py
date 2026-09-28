"""Schema parsing and field validation tests."""

from __future__ import annotations

import pytest

from pg_router.config.loader import parse_config_string, substitute_env
from pg_router.config.schema import (
    Backend,
    Certificate,
    Listener,
    Matcher,
    Route,
    Tunnel,
    Transport,
    transport_layer,
)
from pg_router.utils.security import ValidationError


def test_transport_layer_maps_http_and_stream():
    assert transport_layer("ws") == "http"
    assert transport_layer("grpc") == "http"
    assert transport_layer("tcp") == "stream"
    with pytest.raises(ValidationError):
        transport_layer("carrier-pigeon")


def test_listener_rejects_bad_port_and_mode():
    with pytest.raises(ValidationError):
        Listener.from_dict({"id": "l1", "port": 70000})
    with pytest.raises(ValidationError):
        Listener.from_dict({"id": "l1", "port": 443, "mode": "quic"})
    with pytest.raises(ValidationError):
        Listener.from_dict({"id": "bad listener", "port": 443})


def test_listener_accepts_ipv6_and_extra_listens():
    listener = Listener.from_dict(
        {
            "id": "l1",
            "address": "::",
            "port": 8443,
            "mode": "stream",
            "listen": [{"address": "127.0.0.1", "port": 9443}],
        }
    )
    assert ("::", 8443, "tcp") in listener.all_listens()
    assert ("127.0.0.1", 9443, "tcp") in listener.all_listens()


def test_matcher_single_and_compound():
    simple = Matcher.from_dict({"type": "path_prefix", "value": "/nl"})
    assert simple.type == "path_prefix" and simple.value == "/nl"
    assert not simple.is_compound

    compound = Matcher.from_dict(
        {
            "all": [
                {"type": "host", "value": "example.com"},
                {"type": "path_prefix", "value": "/nl"},
            ]
        }
    )
    assert compound.is_compound
    assert len(compound.all_matchers()) == 3  # root + 2 children


def test_matcher_rejects_mixed_compound_and_type():
    with pytest.raises(ValidationError):
        Matcher.from_dict({"all": [{"type": "host", "value": "x"}], "type": "path_prefix"})


def test_matcher_rejects_two_compound_keys():
    with pytest.raises(ValidationError):
        Matcher.from_dict({"all": [{"type": "host", "value": "x"}], "any": [{"type": "host", "value": "y"}]})


def test_matcher_validates_sni_as_hostname():
    matcher = Matcher.from_dict({"type": "sni", "values": ["a.example.com", "b.example.com"]})
    assert matcher.values == ["a.example.com", "b.example.com"]
    with pytest.raises(ValidationError):
        Matcher.from_dict({"type": "sni", "value": "not a hostname!"})


def test_transport_validates_path_and_headers():
    transport = Transport.from_dict({"type": "ws", "path": "/nl", "headers": {"X-Tag": "abc"}})
    assert transport.path == "/nl"
    assert transport.headers == {"X-Tag": "abc"}
    assert transport.layer == "http"
    with pytest.raises(ValidationError):
        Transport.from_dict({"type": "ws", "path": "no-slash"})
    with pytest.raises(ValidationError):
        Transport.from_dict({"type": "ws", "headers": {"Bad Header": "x"}})


def test_transport_rejects_directive_injection_in_header_value():
    with pytest.raises(ValidationError):
        Transport.from_dict({"type": "ws", "headers": {"X-Tag": "a; server {"}})


def test_backend_types_require_their_fields():
    with pytest.raises(ValidationError):
        Backend.from_dict({"id": "b1", "type": "local", "host": "127.0.0.1"})
    with pytest.raises(ValidationError):
        Backend.from_dict({"id": "b1", "type": "tunnel"})
    assert Backend.from_dict(
        {"id": "b1", "type": "tunnel", "tunnel": "t1"}
    ).tunnel == "t1"
    assert Backend.from_dict(
        {"id": "b1", "type": "failover", "primary": "b2", "backups": ["b3"]}
    ).backups == ["b3"]


def test_tunnel_mode_requires_matching_fields():
    with pytest.raises(ValidationError):
        Tunnel.from_dict({"id": "t1", "mode": "reverse"})
    reverse = Tunnel.from_dict(
        {"id": "t1", "mode": "reverse", "listener": {"address": "127.0.0.1", "port": 41001}}
    )
    assert reverse.endpoint().host == "127.0.0.1"
    assert reverse.endpoint().port == 41001

    with pytest.raises(ValidationError):
        Tunnel.from_dict({"id": "t2", "mode": "direct"})
    direct = Tunnel.from_dict(
        {"id": "t2", "mode": "direct", "remote": {"host": "10.0.0.2", "port": 40001}}
    )
    assert direct.endpoint().host == "10.0.0.2"


def test_certificate_existing_requires_paths():
    with pytest.raises(ValidationError):
        Certificate.from_dict({"id": "c1", "provider": "existing"})
    cert = Certificate.from_dict(
        {"id": "c1", "provider": "existing", "chain": "/a/fullchain.pem", "key": "/a/privkey.pem"}
    )
    assert cert.chain == "/a/fullchain.pem"


def test_route_accepts_inline_backend():
    route = Route.from_dict(
        {
            "id": "r1",
            "listener": "l1",
            "transport": {"type": "ws"},
            "match": {"type": "path_prefix", "value": "/x"},
            "backend": {"host": "127.0.0.1", "port": 62050},
        }
    )
    assert route.backend_inline is not None
    assert route.backend_inline.host == "127.0.0.1"


def test_config_rejects_duplicate_ids():
    duplicate = """
    version: 1
    listeners:
      - {id: dup, port: 443, mode: http}
      - {id: dup, port: 8443, mode: stream}
    """
    with pytest.raises(ValidationError):
        parse_config_string(duplicate)


def test_config_rejects_unknown_version():
    with pytest.raises(ValidationError):
        parse_config_string("version: 2\n")


def test_env_substitution_with_default():
    os_env = {"PG_TEST_HOST": "relay.example.com"}
    import os

    old = os.environ.get("PG_TEST_HOST")
    os.environ.update(os_env)
    try:
        assert substitute_env("${PG_TEST_HOST}") == "relay.example.com"
        assert substitute_env("${PG_MISSING:-127.0.0.1}") == "127.0.0.1"
        assert substitute_env({"a": ["${PG_TEST_HOST}", 1]}) == {"a": ["relay.example.com", 1]}
    finally:
        if old is None:
            os.environ.pop("PG_TEST_HOST", None)
        else:
            os.environ["PG_TEST_HOST"] = old


def test_env_substitution_missing_required_variable():
    import os

    os.environ.pop("PG_DEFINITELY_MISSING", None)
    with pytest.raises(ValidationError):
        substitute_env("${PG_DEFINITELY_MISSING}")
