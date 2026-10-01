"""Tests for nginx dynamic-module detection.

Regression: on a dynamic-module build (Debian/Ubuntu nginx) the configure args
read ``--with-stream=dynamic`` / ``--with-stream_ssl_preread_module=dynamic``.
The old detector tested the static flag *before* the ``=dynamic`` form with a
plain substring search, so ``--with-stream_ssl_preread_module=dynamic`` matched
the static test for ``--with-stream_ssl_preread_module`` and the module was
reported as statically built. Its ``load_module`` directive was then never
emitted, the deployer's staging config loaded no dynamic modules at all, and
``nginx -t`` died with ``unknown "ssl_preread_server_name" variable`` before any
fragment was ever swapped in.
"""

from __future__ import annotations

import pytest

from pg_router.nginx import modules as module_detector
from pg_router.utils.system import CommandResult

# Captured at import: _stub() replaces the scanner with a no-op, tests that
# need the real filesystem scan restore it from here.
_real_find_all_dynamic_modules = module_detector._find_all_dynamic_modules
# _stub() also neutralizes probe_features (no real nginx in CI); the probe
# invocation tests restore the real one from here.
_real_probe_features = module_detector.probe_features

UBUNTU_1_24_ARGS = (
    "--prefix=/usr/share/nginx "
    "--conf-path=/etc/nginx/nginx.conf "
    "--modules-path=/usr/lib/nginx/modules "
    "--with-compat "
    "--with-http_ssl_module=dynamic "
    "--with-http_v2_module=dynamic "
    "--with-http_grpc_module=dynamic "
    "--with-stream=dynamic "
    "--with-stream_ssl_module=dynamic "
    "--with-stream_ssl_preread_module=dynamic "
    "--with-mail=dynamic "
    "--with-mail_ssl_module=dynamic"
)

STATIC_BUILD_ARGS = (
    "--prefix=/usr/share/nginx "
    "--with-http_ssl_module "
    "--with-http_v2_module "
    "--with-stream "
    "--with-stream_ssl_module "
    "--with-stream_ssl_preread_module"
)


def _stub(monkeypatch, configure_args: str, module_include=None):
    """Wire detect() to a fake nginx -V and a fake module directory."""
    monkeypatch.setattr(
        module_detector, "find_nginx", lambda binary=None: "/usr/sbin/nginx"
    )
    monkeypatch.setattr(
        module_detector,
        "run",
        lambda args, **kwargs: CommandResult(
            returncode=0,
            stdout="",
            stderr=(
                "nginx version: nginx/1.24.0\n"
                "built by gcc 13.2.0 (Ubuntu 13.2.0-23ubuntu3)\n"
                f"configure arguments: {configure_args}\n"
            ),
        ),
    )
    # Pretend every dynamic module .so the build names is installed.
    monkeypatch.setattr(
        module_detector,
        "_find_dynamic_module",
        lambda feature: f"/usr/lib/nginx/modules/ngx_{feature}_module.so",
    )
    monkeypatch.setattr(
        module_detector, "_find_module_include", lambda: module_include
    )
    # No module directory exists on the test host: keep the per-feature list
    # the =dynamic branch built instead of scanning the (absent) filesystem.
    monkeypatch.setattr(module_detector, "_find_all_dynamic_modules", lambda: [])
    # The distro include files are not readable in the test environment; tests
    # that exercise the merge stub _include_loads explicitly.
    monkeypatch.setattr(module_detector, "_include_loads", lambda include, prefix="": set())
    # Probes would exec a real nginx; the parse of nginx -V is what is under
    # test here.
    monkeypatch.setattr(module_detector, "probe_features", lambda binary, modules=None: {})


def test_stream_core_flag_is_the_one_ngnix_actually_uses():
    """nginx spells the stream core flag ``--with-stream``; the previous
    ``--with-stream_module`` value matched nothing any nginx build ever emits,
    so stream was only ever found via the probe."""
    assert module_detector.STATIC_FLAGS["stream"] == "--with-stream"


def test_dynamic_flags_are_not_mistaken_for_static(monkeypatch):
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    detected = module_detector.detect()

    # None of the dynamic modules may be reported statically available: that
    # short-circuits the load_module bookkeeping that makes them usable.
    for feature in ("stream", "stream_ssl", "stream_ssl_preread", "http_ssl", "http_v2"):
        assert detected.features.get(feature) is not True, (
            f"{feature} is built =dynamic and must not read as statically built"
        )
    assert detected.supports("stream") is False


def test_dynamic_build_records_load_module_paths(monkeypatch):
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    detected = module_detector.detect()

    # The stream core module must be recorded: without loading it, the stream
    # block itself is an unknown directive.
    assert "/usr/lib/nginx/modules/ngx_stream_module.so" in detected.load_modules
    assert "/usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so" in detected.load_modules
    # http sub-modules are dynamic on this build too.
    assert "/usr/lib/nginx/modules/ngx_http_ssl_module.so" in detected.load_modules


def test_static_build_needs_no_load_module_directives(monkeypatch):
    _stub(monkeypatch, STATIC_BUILD_ARGS)
    detected = module_detector.detect()

    assert detected.load_modules == []
    for feature in ("stream", "stream_ssl", "stream_ssl_preread", "http_ssl", "http_v2"):
        assert detected.supports(feature), f"{feature} is compiled in statically"


def test_bare_stream_flag_is_not_matched_inside_stream_submodule_flags(monkeypatch):
    """``--with-stream`` must not match within ``--with-stream_ssl_module``;
    the lookahead has to reject the ``_`` that starts the sub-module suffix
    (and the ``=`` of ``=dynamic``)."""
    _stub(monkeypatch, "--with-stream_ssl_module=dynamic")
    detected = module_detector.detect()

    assert "/usr/lib/nginx/modules/ngx_stream_module.so" not in detected.load_modules, (
        "a build with only the stream *ssl* sub-module does not provide stream core"
    )


def test_module_include_is_preferred_for_load_lines(monkeypatch):
    _stub(monkeypatch, UBUNTU_1_24_ARGS, module_include="/etc/nginx/modules-enabled/*.conf")
    # The curated list covers every dynamic module detection found ...
    monkeypatch.setattr(
        module_detector,
        "_include_loads",
        lambda include, prefix="": {
            "/usr/lib/nginx/modules/ngx_http_ssl_module.so",
            "/usr/lib/nginx/modules/ngx_http_v2_module.so",
            "/usr/lib/nginx/modules/ngx_http_grpc_module.so",
            "/usr/lib/nginx/modules/ngx_stream_module.so",
            "/usr/lib/nginx/modules/ngx_stream_ssl_module.so",
            "/usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so",
            "/usr/lib/nginx/modules/ngx_mail_ssl_module.so",
        },
    )
    detected = module_detector.detect()

    # ... so the include is the only thing emitted: it is exactly what the live
    # nginx.conf loads, in the distro's dependency order.
    assert detected.module_include == "/etc/nginx/modules-enabled/*.conf"
    assert detected.load_lines() == "include /etc/nginx/modules-enabled/*.conf;"


def test_load_lines_fall_back_to_explicit_load_module(monkeypatch):
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    detected = module_detector.detect()

    assert detected.module_include is None
    lines = detected.load_lines().splitlines()
    assert "load_module /usr/lib/nginx/modules/ngx_stream_module.so;" in lines
    assert "load_module /usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so;" in lines
    assert len(lines) == len(detected.load_modules)


def test_module_include_is_not_duplicated_when_it_covers_a_module(monkeypatch):
    """nginx refuses to load a module twice, so a module the distro include
    already loads must not also be emitted as a load_module directive."""
    _stub(monkeypatch, UBUNTU_1_24_ARGS, module_include="/etc/nginx/modules-enabled/*.conf")
    monkeypatch.setattr(
        module_detector,
        "_include_loads",
        lambda include, prefix="": {
            "/usr/lib/nginx/modules/ngx_stream_module.so",
            "/usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so",
        },
    )
    detected = module_detector.detect()

    lines = detected.load_lines().splitlines()
    assert lines[0] == "include /etc/nginx/modules-enabled/*.conf;"
    assert "load_module /usr/lib/nginx/modules/ngx_stream_module.so;" not in lines
    assert "load_module /usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so;" not in lines


def test_uncovered_modules_are_added_alongside_the_include(monkeypatch):
    """Modules the curated list does not cover — the stream proxy/map modules
    the probes never test — still have to load, otherwise the staging config
    rejects their directives."""
    _stub(monkeypatch, UBUNTU_1_24_ARGS, module_include="/etc/nginx/modules-enabled/*.conf")
    monkeypatch.setattr(
        module_detector, "_include_loads", lambda include, prefix="": set()
    )
    detected = module_detector.detect()

    lines = detected.load_lines().splitlines()
    assert lines[0] == "include /etc/nginx/modules-enabled/*.conf;"
    assert "load_module /usr/lib/nginx/modules/ngx_stream_module.so;" in lines


def test_include_loads_parses_the_distro_confs(monkeypatch, tmp_path):
    """_include_loads reads what the distro's .conf files actually load, so
    deduplication reflects reality rather than guessing."""
    enabled = tmp_path / "modules-enabled"
    enabled.mkdir()
    (enabled / "50-mod-stream.conf").write_text(
        "load_module /usr/lib/nginx/modules/ngx_stream_module.so;\n", encoding="utf-8"
    )
    (enabled / "60-mod-stream-ssl.conf").write_text(
        "# ssl + preread\nload_module /usr/lib/nginx/modules/ngx_stream_ssl_module.so;\n"
        "load_module /usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so;\n",
        encoding="utf-8",
    )
    pattern = str(enabled / "*.conf")

    loaded = module_detector._include_loads(pattern)

    assert loaded == {
        "/usr/lib/nginx/modules/ngx_stream_module.so",
        "/usr/lib/nginx/modules/ngx_stream_ssl_module.so",
        "/usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so",
    }


def test_include_loads_resolves_relative_paths_against_the_prefix(monkeypatch, tmp_path):
    """Debian/Ubuntu writes *relative* load_module paths in modules-enabled
    ("modules/ngx_stream_module.so"), which nginx resolves against its build
    prefix. Deduplication must do the same, or the include's relative entry and
    detection's absolute path compare as different files and the module gets
    loaded twice — nginx then aborts with ``module "..." is already loaded``."""
    enabled = tmp_path / "modules-enabled"
    enabled.mkdir()
    (enabled / "50-mod-stream.conf").write_text(
        "load_module modules/ngx_stream_module.so;\n", encoding="utf-8"
    )
    pattern = str(enabled / "*.conf")

    loaded = module_detector._include_loads(pattern, prefix="/usr/share/nginx")

    assert loaded == {"/usr/share/nginx/modules/ngx_stream_module.so"}


def test_load_lines_dedupes_relative_include_paths(monkeypatch):
    """Regression: the server's staging test died with
    ``module "ngx_stream_module.so" is already loaded`` because the include
    loaded it via a relative path while load_lines added the absolute path.
    nginx identifies modules by .so filename, so that is what deduplication
    must compare — the same module can even live in two different directories
    across hosts."""
    _stub(monkeypatch, UBUNTU_1_24_ARGS, module_include="/etc/nginx/modules-enabled/*.conf")
    monkeypatch.setattr(
        module_detector,
        "_include_loads",
        lambda include, prefix="": {
            "/usr/share/nginx/modules/ngx_stream_module.so",
            "/usr/share/nginx/modules/ngx_stream_ssl_preread_module.so",
        },
    )
    detected = module_detector.detect()

    lines = detected.load_lines().splitlines()
    assert lines[0] == "include /etc/nginx/modules-enabled/*.conf;"
    assert "load_module /usr/lib/nginx/modules/ngx_stream_module.so;" not in lines
    assert (
        "load_module /usr/lib/nginx/modules/ngx_stream_ssl_preread_module.so;" not in lines
    )


def test_fallback_loads_the_whole_installed_module_set(monkeypatch, tmp_path):
    """Without a curated include, the generated fragments still use directives
    from modules the probes never test (stream proxy/map), so every installed
    .so must be loaded — core first, since the rest link against it."""
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    # Deliberately created out of order: the core module must still load first.
    for name in (
        "ngx_stream_proxy_module.so",
        "ngx_stream_ssl_preread_module.so",
        "ngx_stream_module.so",
        "ngx_stream_map_module.so",
    ):
        (module_dir / name).write_text("", encoding="utf-8")
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    # _stub() neutralizes the filesystem scan; restore it and point it at the
    # temp module directory.
    monkeypatch.setattr(module_detector, "DYNAMIC_MODULE_DIRS", (str(module_dir),))
    monkeypatch.setattr(
        module_detector,
        "_find_all_dynamic_modules",
        _real_find_all_dynamic_modules,
    )
    detected = module_detector.detect()

    assert detected.module_include is None
    lines = detected.load_lines().splitlines()
    # Core module first so dlopen resolves its symbols for the rest.
    assert lines[0].endswith("ngx_stream_module.so;"), lines[0]
    assert any(line.endswith("ngx_stream_map_module.so;") for line in lines)
    assert any(line.endswith("ngx_stream_proxy_module.so;") for line in lines)
    assert len(lines) == 4


def test_static_build_does_not_load_any_modules(monkeypatch):
    """A statically built nginx needs no load_module directives at all."""
    _stub(monkeypatch, STATIC_BUILD_ARGS)
    monkeypatch.setattr(
        module_detector,
        "_find_all_dynamic_modules",
        lambda: ["/usr/lib/nginx/modules/ngx_stream_module.so"],
    )
    detected = module_detector.detect()

    assert detected.load_modules == []


@pytest.mark.parametrize("module_include", [None, "/etc/nginx/modules-enabled/*.conf"])
def test_load_lines_are_always_safe_when_empty(monkeypatch, module_include):
    """A host with no dynamic modules must produce no load directives: an empty
    line is harmless in nginx -t, but a dangling load_module is a hard error."""
    _stub(monkeypatch, "--prefix=/usr/share/nginx")
    detected = module_detector.detect()
    assert detected.load_modules == []
    assert detected.load_lines() == ""


def test_ubuntu_dynamic_build_produces_a_working_module_include(monkeypatch):
    """End-to-end shape of the host that hit this bug: Ubuntu 24.04 nginx 1.24
    reports every stream module as =dynamic, so detection must hand the
    deployer a module include the staging config can load."""
    _stub(
        monkeypatch,
        UBUNTU_1_24_ARGS,
        module_include="/etc/nginx/modules-enabled/*.conf",
    )
    detected = module_detector.detect()

    assert detected.module_include == "/etc/nginx/modules-enabled/*.conf"
    assert detected.load_lines().startswith("include /etc/nginx/modules-enabled/")


def test_configure_prefix_is_read_from_nginx_v():
    """Debian's module .conf files use relative load_module paths that nginx
    resolves against --prefix, so detection has to hand that prefix to anything
    building a standalone config."""
    assert module_detector._configure_prefix(UBUNTU_1_24_ARGS.split()) == "/usr/share/nginx"
    assert module_detector._configure_prefix(["--with-stream"]) == ""
    assert module_detector._configure_prefix([]) == ""


def test_detect_records_the_nginx_prefix(monkeypatch):
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    detected = module_detector.detect()

    assert detected.prefix == "/usr/share/nginx"


def test_probe_tests_with_the_real_nginx_prefix(monkeypatch):
    """Regression: the probe ran ``nginx -t -p <tempdir>``, but Debian's
    modules-enabled include holds *relative* load_module paths. nginx resolves
    them against the prefix, so it searched ``<tempdir>/modules/ngx_stream_module.so``,
    failed the dlopen, and every dynamic module read as "missing" — which is
    why the operator saw ``stream: missing`` next to ``stream_ssl_preread:
    available`` on a host that definitely had stream."""
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    modules = module_detector.detect()
    assert modules.prefix == "/usr/share/nginx"

    # Only now replace run(): detect() needs the fake nginx -V above, the probe
    # invocations are what is under test here.
    invocations: list[list[str]] = []
    monkeypatch.setattr(
        module_detector,
        "run",
        lambda args, **kwargs: (
            invocations.append(args),
            CommandResult(returncode=0, stdout="", stderr=""),
        )[1],
    )
    monkeypatch.setattr(module_detector, "probe_features", _real_probe_features)
    module_detector.probe_features("/usr/sbin/nginx", modules)

    assert invocations, "probe_features must exec nginx"
    for args in invocations:
        assert args[:2] == ["/usr/sbin/nginx", "-t"]
        # The temp scratch dir must not be the prefix.
        assert "-p" in args
        assert args[args.index("-p") + 1] == "/usr/share/nginx"
        assert "pg-router-probe" not in args[args.index("-p") + 1]


def test_probe_falls_back_to_scratch_without_a_known_prefix(monkeypatch):
    """A build without --prefix= in its configure args keeps the old behaviour:
    the scratch directory is the prefix."""
    _stub(monkeypatch, "--with-stream")
    modules = module_detector.detect()
    assert modules.prefix == ""

    invocations: list[list[str]] = []
    monkeypatch.setattr(
        module_detector,
        "run",
        lambda args, **kwargs: (
            invocations.append(args),
            CommandResult(returncode=0, stdout="", stderr=""),
        )[1],
    )
    module_detector.probe_single("/usr/sbin/nginx", "stream", modules)

    (args,) = invocations
    assert "pg-router-probe" in args[args.index("-p") + 1], (
        "without a known --prefix the scratch directory remains the prefix"
    )


def test_probe_single_passes_the_prefix_too(monkeypatch):
    _stub(monkeypatch, UBUNTU_1_24_ARGS)
    modules = module_detector.detect()

    invocations: list[list[str]] = []
    monkeypatch.setattr(
        module_detector,
        "run",
        lambda args, **kwargs: (
            invocations.append(args),
            CommandResult(returncode=0, stdout="", stderr=""),
        )[1],
    )
    module_detector.probe_single("/usr/sbin/nginx", "stream", modules)

    (args,) = invocations
    assert args[args.index("-p") + 1] == "/usr/share/nginx"
