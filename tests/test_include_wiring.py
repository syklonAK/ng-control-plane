"""Tests for nginx.conf include wiring (NginxManager.ensure_managed_include).

The live nginx.conf is the only file outside /etc/nginx/pg-router that the
tool modifies, so the wiring must be correct and idempotent. A stream-only
host broke in production because stream.conf proxies to upstreams that live
in upstreams.conf, but nginx.conf only ever included stream.conf — so nginx
resolved no upstreams at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pg_router.nginx.manager import NginxManager

MAIN_CONF = """\
load_module /usr/lib/nginx/modules/ndk.so;
events { worker_connections 128; }
http {
    include /etc/nginx/conf.d/*.conf;
}
stream {
    include /etc/nginx/stream.d/*.conf;
}
"""


@pytest.fixture
def manager(tmp_path: Path, monkeypatch) -> NginxManager:
    main_conf = tmp_path / "nginx.conf"
    main_conf.write_text(MAIN_CONF, encoding="utf-8")
    managed = tmp_path / "pg-router"
    managed.mkdir()
    # Stub the module list so no load_module lines are added.
    instance = NginxManager(binary="nginx")
    monkeypatch.setattr(
        type(instance.modules), "load_modules", (), raising=False
    )
    monkeypatch.setattr(
        NginxManager, "read_main_conf", lambda self: main_conf.read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        NginxManager,
        "test",
        lambda self, main_conf=None, prefix=None: (True, "ok"),
    )
    # The manager writes to DEFAULT_MAIN_CONF; redirect that to the temp file.
    import pg_router.nginx.manager as module

    monkeypatch.setattr(module, "DEFAULT_MAIN_CONF", str(main_conf))
    instance._managed_dir = str(managed)
    return instance


def test_stream_only_host_gets_upstreams_in_the_stream_block(manager):
    """stream.conf references upstreams defined in upstreams.conf; without
    including upstreams.conf in the stream block, nginx resolves nothing."""
    managed = manager._managed_dir
    changed = manager.ensure_managed_include(managed, ())
    assert changed

    text = manager.read_main_conf()
    http_block, stream_block = text.split("stream {", 1)
    assert "include " in http_block
    assert "upstreams.conf" in stream_block, (
        "the stream block must load upstreams.conf: stream.conf proxies to "
        "upstreams defined there, and nginx never loads the fragment otherwise"
    )
    assert "stream.conf" in stream_block


def test_http_block_loads_maps_and_upstreams(manager):
    """The http block needs its upgrade map (for ws/httpupgrade transports)
    and the upstreams its locations proxy to."""
    text = manager.ensure_managed_include(manager._managed_dir, ()) and manager.read_main_conf()
    http_block = text.split("http {", 1)[1].split("}", 1)[0]
    assert "maps.conf" in http_block
    assert "upstreams.conf" in http_block
    assert "http.conf" in http_block


def test_wiring_is_idempotent(manager):
    """Re-running must not duplicate include lines: a growing nginx.conf on
    every apply would eventually break the config."""
    managed = manager._managed_dir
    assert manager.ensure_managed_include(managed, ())
    again = manager.read_main_conf()
    assert manager.ensure_managed_include(managed, ()) is False
    assert manager.read_main_conf() == again


def test_http_only_config_still_wires_stream_block(manager):
    """Even with no stream routes, the stream include is wired once so a later
    stream-only apply does not need to touch nginx.conf again."""
    text = manager.ensure_managed_include(manager._managed_dir, ()) and manager.read_main_conf()
    assert "stream {" in text
    assert "stream.conf" in text
