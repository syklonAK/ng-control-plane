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


MAIN_CONF_NO_MODULES = """\
events { worker_connections 128; }
http {
    include /etc/nginx/conf.d/*.conf;
}
stream {
    include /etc/nginx/stream.d/*.conf;
}
"""


@pytest.fixture
def dynamic_module_manager(tmp_path: Path, monkeypatch) -> NginxManager:
    """A host whose nginx loads its modules dynamically via the distro's
    modules-enabled glob (Debian/Ubuntu), and whose nginx.conf does not yet
    include it."""
    from pg_router.nginx.modules import NginxModules

    main_conf = tmp_path / "nginx.conf"
    main_conf.write_text(MAIN_CONF_NO_MODULES, encoding="utf-8")
    managed = tmp_path / "pg-router"
    managed.mkdir()
    instance = NginxManager(binary="nginx")
    monkeypatch.setattr(
        NginxManager,
        "modules",
        property(
            lambda self: NginxModules(
                binary="nginx",
                version="1.24.0",
                module_include="/etc/nginx/modules-enabled/*.conf",
                load_modules=["/usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so"],
            )
        ),
    )
    monkeypatch.setattr(
        NginxManager, "read_main_conf", lambda self: main_conf.read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        NginxManager,
        "test",
        lambda self, main_conf=None, prefix=None: (True, "ok"),
    )
    import pg_router.nginx.manager as module

    monkeypatch.setattr(module, "DEFAULT_MAIN_CONF", str(main_conf))
    instance._managed_dir = str(managed)
    return instance


def test_module_include_is_wired_into_live_config(dynamic_module_manager):
    """The live nginx.conf must load the dynamic modules the fragments need.
    On a dynamic build without this, nginx parses the fragments against a
    module set that lacks ssl_preread and rejects every stream config."""
    assert dynamic_module_manager.ensure_managed_include(
        dynamic_module_manager._managed_dir, ()
    )
    text = dynamic_module_manager.read_main_conf()
    assert "include /etc/nginx/modules-enabled/*.conf;" in text


def test_module_include_is_added_before_the_http_block(dynamic_module_manager):
    """load_module (and its include) are main-context directives: they must
    precede the events/http/stream blocks whose directives they provide."""
    dynamic_module_manager.ensure_managed_include(dynamic_module_manager._managed_dir, ())
    text = dynamic_module_manager.read_main_conf()
    assert text.index("modules-enabled") < text.index("http {")


def test_module_include_is_not_duplicated(dynamic_module_manager):
    """A host that already ships the include (Ubuntu's default nginx.conf)
    must not get a second one: a duplicated module include makes nginx fail
    to load."""
    dynamic_module_manager.ensure_managed_include(dynamic_module_manager._managed_dir, ())
    once = dynamic_module_manager.read_main_conf()
    assert dynamic_module_manager.ensure_managed_include(
        dynamic_module_manager._managed_dir, ()
    ) is False
    assert dynamic_module_manager.read_main_conf() == once
    assert once.count("modules-enabled") == 1


def test_explicit_load_module_used_when_no_distro_include(manager, monkeypatch):
    """Hosts without a modules-enabled list still get per-module load_module
    directives for everything detection found."""
    from pg_router.nginx.modules import NginxModules

    monkeypatch.setattr(
        NginxManager,
        "modules",
        property(
            lambda self: NginxModules(
                binary="nginx",
                load_modules=["/usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so"],
            )
        ),
    )
    assert manager.ensure_managed_include(manager._managed_dir, ())
    text = manager.read_main_conf()
    assert (
        "load_module /usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so;" in text
    )


def test_existing_load_module_line_is_not_duplicated(manager, monkeypatch):
    """A load_module line already in nginx.conf must not be written again:
    nginx refuses to load a module twice and fails the whole config."""
    from pg_router.nginx.modules import NginxModules

    # MAIN_CONF already carries this exact load_module line.
    monkeypatch.setattr(
        NginxManager,
        "modules",
        property(
            lambda self: NginxModules(
                binary="nginx",
                load_modules=["/usr/lib/nginx/modules/ndk.so"],
            )
        ),
    )
    manager.ensure_managed_include(manager._managed_dir, ())
    text = manager.read_main_conf()
    assert text.count("load_module /usr/lib/nginx/modules/ndk.so;") == 1


def test_modules_the_include_does_not_cover_are_added(dynamic_module_manager, monkeypatch):
    """The distro include may not list every module the fragments need; the
    rest are appended as explicit load_module directives."""
    import pg_router.nginx.modules as module_detector

    # The include loads only the stream core, not ssl_preread.
    monkeypatch.setattr(
        module_detector,
        "_include_loads",
        lambda include: {"/usr/lib/nginx/modules/ngx_stream_module.so"},
    )
    dynamic_module_manager.ensure_managed_include(
        dynamic_module_manager._managed_dir, ()
    )
    text = dynamic_module_manager.read_main_conf()
    assert "include /etc/nginx/modules-enabled/*.conf;" in text
    assert (
        "load_module /usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so;" in text
    )
    assert "load_module /usr/lib/nginx/modules/ngx_stream_module.so;" not in text, (
        "the include already loads it; a second load_module would fail nginx -t"
    )
