"""Nginx detection: version and compiled/dynamic modules.

Requirement 22: never assume a module exists just because Nginx is installed.
Detection combines three signals:

1. ``nginx -V`` configure arguments (static vs dynamic build)
2. module .so files in the distribution's module directory (dynamic)
3. a functional probe: a throwaway config exercising each directive, checked
   with ``nginx -t``. Only a passing probe marks a feature available.
"""

from __future__ import annotations

import glob
import os
import posixpath
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ..utils.logging import get_logger
from ..utils.system import CommandError, run, which

_log = get_logger(__name__)

# Features the generator cares about, mapped to their probe snippets.
FEATURE_PROBES: dict[str, str] = {
    "http": """
events {}
http {
    server { listen 127.0.0.1:PROBEPORT; location / { return 200 ok; } }
}
""",
    "http_ssl": """
events {}
http {
    server { listen 127.0.0.1:PROBEPORT ssl; ssl_reject_handshake on; }
}
""",
    "http_v2": """
events {}
http {
    server { listen 127.0.0.1:PROBEPORT ssl http2; ssl_reject_handshake on; }
}
""",
    "http_grpc": """
events {}
http {
    upstream g { server 127.0.0.1:PROBEPORT2; }
    server { listen 127.0.0.1:PROBEPORT; location / { grpc_pass http://g; } }
}
""",
    "stream": """
events {}
stream {
    server { listen 127.0.0.1:PROBEPORT; proxy_pass 127.0.0.1:PROBEPORT2; }
}
""",
    "stream_ssl": """
events {}
stream {
    server { listen 127.0.0.1:PROBEPORT ssl; ssl_reject_handshake on; }
}
""",
    "stream_ssl_preread": """
events {}
stream {
    server { listen 127.0.0.1:PROBEPORT; ssl_preread on; proxy_pass 127.0.0.1:PROBEPORT2; }
}
""",
    "http_websocket": """
events {}
http {
    map $http_upgrade $c { default upgrade; '' close; }
    server { listen 127.0.0.1:PROBEPORT; location / { proxy_pass http://127.0.0.1:PROBEPORT2; }
             proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $c; }
}
""",
}

STATIC_FLAGS: dict[str, str] = {
    "http_ssl": "--with-http_ssl_module",
    "http_v2": "--with-http_v2_module",
    "http_grpc": "--with-http_grpc_module",
    # nginx spells the stream *core* flag "--with-stream", never
    # "--with-stream_module" (that form does not exist), with or without
    # "=dynamic". The sub-modules do use the "_module" suffix.
    "stream": "--with-stream",
    "stream_ssl": "--with-stream_ssl_module",
    "stream_ssl_preread": "--with-stream_ssl_preread_module",
}

DYNAMIC_MODULE_DIRS = (
    "/usr/lib/nginx/modules",
    "/usr/share/nginx/modules",
    "/usr/libexec/nginx/modules",
    "/etc/nginx/modules",
)

# Distro-curated load_module lists. Debian/Ubuntu ship one .conf per module
# under modules-enabled (symlinks into the module directory), each holding a
# single load_module directive, ordered so a module loads before anything that
# depends on it. The live nginx.conf includes this glob, so reusing it makes a
# staging/probe config exercise exactly the module set production uses instead
# of us guessing .so filenames and dependency order.
DYNAMIC_MODULE_INCLUDES = (
    "/etc/nginx/modules-enabled/*.conf",
    "/usr/share/nginx/modules/*.conf",
    "/etc/nginx/modules/*.conf",
)

DYNAMIC_MODULE_FILES: dict[str, tuple[str, ...]] = {
    "http_ssl": ("ngx_http_ssl_module.so",),
    "http_v2": ("ngx_http_v2_module.so",),
    "http_grpc": ("ngx_http_grpc_module.so",),
    "stream": ("ngx_stream_module.so",),
    "stream_ssl": ("ngx_stream_ssl_module.so",),
    "stream_ssl_preread": ("ngx_stream_ssl_preread_module.so",),
}

PROBE_PORT = 18443
PROBE_PORT_2 = 18444


@dataclass
class NginxModules:
    """Detected Nginx capabilities."""

    binary: str = ""
    version: str = ""
    configure_args: list[str] = field(default_factory=list)
    features: dict[str, bool] = field(default_factory=dict)
    load_modules: list[str] = field(default_factory=list)
    # Glob of distro .conf files that load the dynamic modules (see
    # DYNAMIC_MODULE_INCLUDES). Preferred over ``load_modules`` when present.
    module_include: Optional[str] = None
    # The ``--prefix`` nginx was configured with. Debian/Ubuntu ship module
    # .conf files whose ``load_module`` paths are *relative* ("modules/
    # ngx_stream_module.so"), and nginx resolves them against this prefix —
    # never against the config file's directory. A staging/probe config must
    # therefore be tested with the real prefix, or nginx goes looking for the
    # .so inside the throwaway temp dir and every dlopen fails.
    prefix: str = ""

    def supports(self, feature: str) -> bool:
        return bool(self.features.get(feature))

    def require(self, *features: str) -> None:
        """Raise if any required feature is unavailable."""
        missing = [feature for feature in features if not self.supports(feature)]
        if missing:
            raise CommandError(
                f"nginx {self.version or '(unknown version)'} lacks required modules: "
                f"{missing}. Install the module package (e.g. libnginx-mod-stream) or "
                "rebuild nginx with the corresponding --with-* flag."
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "binary": self.binary,
            "version": self.version,
            "features": dict(self.features),
            "load_modules": list(self.load_modules),
            "module_include": self.module_include,
            "prefix": self.prefix,
        }

    def load_lines(self) -> str:
        """Directives that make a *standalone* config (a module probe or the
        deployer's staging test) load the same modules the live nginx.conf
        loads.

        A config built without them parses against a different module set than
        production: on a dynamic-module build (Debian/Ubuntu) the stream
        fragments need load_module directives that the live config supplies,
        so nginx fails with ``unknown "ssl_preread_server_name" variable``
        before a fragment is ever swapped in.

        The distro's curated include is used when available, and any module it
        does not load is added explicitly — the generated fragments also use
        directives from modules the feature probes never cover (the stream
        proxy and map modules).
        """
        lines: list[str] = []
        covered: set[str] = set()
        if self.module_include:
            lines.append(f"include {self.module_include};")
            covered = _module_names(_include_loads(self.module_include, self.prefix))
        for path in self.load_modules:
            if _module_name_of(path) not in covered:
                lines.append(f"load_module {path};")
        return "\n".join(lines)


def find_nginx(binary: Optional[str] = None) -> Optional[str]:
    """Locate the nginx binary."""
    if binary:
        return binary if os.path.exists(binary) else None
    for candidate in ("nginx", "/usr/sbin/nginx", "/usr/bin/nginx", "/usr/local/nginx/sbin/nginx"):
        found = which(candidate)
        if found:
            return found
    return None


def _parse_version_output(stderr: str) -> tuple[str, list[str]]:
    version = ""
    args: list[str] = []
    for line in stderr.splitlines():
        match = re.search(r"nginx version:\s*\S+/([\w.+-]+)", line)
        if match and not version:
            version = match.group(1)
        if "configure arguments:" in line:
            raw = line.split("configure arguments:", 1)[1].strip()
            args = [arg.strip() for arg in raw.split() if arg.strip()]
    return version, args


def _configure_prefix(configure_args: list[str]) -> str:
    """The ``--prefix`` nginx was built with.

    Debian/Ubuntu's ``modules-enabled/*.conf`` hold *relative* load_module
    paths ("modules/ngx_stream_module.so"), and nginx resolves them against
    this prefix — not against the config file's own directory. A standalone
    config that includes them must therefore be tested with ``-p <prefix>``,
    or nginx hunts for the .so files in whatever scratch directory the config
    happens to live in.
    """
    for arg in configure_args:
        if arg.startswith("--prefix="):
            return arg.split("=", 1)[1]
    return ""


def _find_dynamic_module(feature: str) -> Optional[str]:
    for filename in DYNAMIC_MODULE_FILES.get(feature, ()):
        for directory in DYNAMIC_MODULE_DIRS:
            path = Path(directory) / filename
            if path.exists():
                return str(path)
    return None


def _find_module_include() -> Optional[str]:
    """The distro's module load list, if it ships one."""
    for pattern in DYNAMIC_MODULE_INCLUDES:
        if glob.glob(pattern):
            return pattern
    return None


def _include_loads(module_include: Optional[str], prefix: str = "") -> set[str]:
    """Module .so paths a distro include glob loads.

    Needed so an explicit ``load_module`` is only added for modules the
    curated list does not already cover: nginx refuses to load a module
    twice and fails the whole config.

    Debian/Ubuntu spells the paths *relatively* ("modules/ngx_stream_module.so"),
    resolved by nginx against its build prefix, while our own
    ``load_modules`` are absolute. Both sides are therefore resolved against
    ``prefix`` and normalized before comparing, or the same file would be
    loaded twice and nginx would abort with ``module "..." is already loaded``.
    """
    if not module_include:
        return set()
    loaded: set[str] = set()
    for conf in glob.glob(module_include):
        try:
            content = Path(conf).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in content.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "load_module":
                loaded.add(_normalize_module_path(parts[1].rstrip(";"), prefix))
    return loaded


def _normalize_module_path(path: str, prefix: str = "") -> str:
    """Resolve a load_module path the way nginx does and canonicalize it.

    nginx resolves relative module paths against the ``-p`` prefix (Debian's
    modules-enabled uses "modules/ngx_stream_module.so"). Canonicalizing lets
    a relative include entry and an absolute detection result be recognized
    as the same file.

    Module paths are always POSIX ones, so separators are canonicalized to
    "/" before normalizing — otherwise the Windows test runner turns
    "/usr/lib/nginx/modules/x.so" into a completely different path and the
    deduplication this exists for silently stops working off-Linux.
    """
    if prefix and not os.path.isabs(path):
        path = os.path.join(prefix, path)
    return posixpath.normpath(path.replace("\\", "/"))


def _module_name_of(path: str) -> str:
    """The .so filename nginx identifies a loaded module by."""
    return posixpath.basename(path.replace("\\", "/"))


def _module_names(paths: Iterable[str]) -> set[str]:
    return {_module_name_of(path) for path in paths}


# Core modules other dynamic modules link against; loaded first so dlopen can
# resolve their symbols. Everything else loads in name order, which happens to
# put e.g. ngx_stream_ssl_module.so before ngx_stream_ssl_preread_module.so.
_CORE_MODULE_FILES = (
    "ngx_stream_module.so",
    "ngx_mail_module.so",
)


def _find_all_dynamic_modules() -> list[str]:
    """Every installed module .so, core modules first.

    Used only when the host ships no curated load list. The generated fragments
    use directives from modules the feature probes do not cover (the stream
    proxy and map modules), so the whole installed set has to be loaded — which
    is exactly what a hand-written nginx.conf on such a host does.
    """
    for directory in DYNAMIC_MODULE_DIRS:
        directory_path = Path(directory)
        if not directory_path.is_dir():
            continue
        found = sorted(
            directory_path.glob("*.so"),
            key=lambda path: (0 if path.name in _CORE_MODULE_FILES else 1, str(path)),
        )
        if found:
            return [str(path) for path in found]
    return []


def detect(binary: Optional[str] = None) -> NginxModules:
    """Full detection: static flags, dynamic module files, functional probes."""
    found = find_nginx(binary)
    if not found:
        return NginxModules()
    modules = NginxModules(binary=found)

    try:
        result = run([found, "-V"], check=False, timeout=30)
    except CommandError:
        return modules
    modules.version, modules.configure_args = _parse_version_output(result.stderr)
    modules.prefix = _configure_prefix(modules.configure_args)

    args_text = " ".join(modules.configure_args)
    for feature in FEATURE_PROBES:
        static_flag = STATIC_FLAGS.get(feature, "")
        if not static_flag:
            modules.features.setdefault(feature, False)
            continue
        # A dynamic build spells the flag "--with-<name>=dynamic": the module
        # .so then needs an explicit load_module directive, so only record the
        # file and let the functional probe decide availability. This must run
        # before the static test: "--with-stream_ssl_preread_module=dynamic"
        # also matches a plain search for "--with-stream_ssl_preread_module",
        # which marked dynamic modules as statically built, never emitted their
        # load_module directive, and then shipped configs nginx could not parse
        # ("unknown \"ssl_preread_server_name\" variable").
        if f"{static_flag}=dynamic" in args_text:
            module_path = _find_dynamic_module(feature)
            if module_path:
                modules.load_modules.append(module_path)
            continue
        if re.search(re.escape(static_flag) + r"(?![\w=])", args_text):
            modules.features[feature] = True
            continue
        modules.features.setdefault(feature, False)

    modules.module_include = _find_module_include()
    if not modules.module_include and modules.load_modules:
        # No curated list available: load the whole installed set instead of
        # only the probed features, so directives from modules the probes never
        # test (stream proxy/map) resolve in the staging config too.
        all_modules = _find_all_dynamic_modules()
        if all_modules:
            modules.load_modules = all_modules

    # http is always present in a standard build; the probe confirms it.
    probe_result = probe_features(found, modules)
    for feature, available in probe_result.items():
        if available:
            modules.features[feature] = True

    _log.info("Nginx detected: %s", modules.version or "unknown")
    for feature in FEATURE_PROBES:
        status = "available" if modules.supports(feature) else "missing"
        _log.info("%s: %s", feature, status)
    return modules


def probe_features(binary: str, modules: Optional[NginxModules] = None) -> dict[str, bool]:
    """Functional probe: can this nginx actually parse each directive?

    Each probe config is written to a temporary directory and tested with
    ``nginx -t``. Nothing is bound permanently and no live config is touched.
    """
    results: dict[str, bool] = {}
    load_lines = modules.load_lines() if modules else ""
    prefix = modules.prefix if modules else ""
    for feature, template in FEATURE_PROBES.items():
        config = template.replace("PROBEPORT", str(PROBE_PORT)).replace(
            "PROBEPORT2", str(PROBE_PORT_2)
        )
        results[feature] = _run_probe(binary, feature, config, load_lines, prefix)
    return results


def _run_probe(
    binary: str,
    feature: str,
    config: str,
    load_lines: str,
    nginx_prefix: str = "",
) -> bool:
    with tempfile.TemporaryDirectory(prefix="pg-router-probe-") as tempdir:
        scratch = Path(tempdir)
        (scratch / "logs").mkdir()
        main_conf = scratch / "nginx.conf"
        # Only directives nginx actually has in the main context. There is no
        # bare "temp_path" directive (nginx uses client_body_temp_path,
        # proxy_temp_path, ...); "nginx -t" never writes temp files anyway, so
        # none of them belong here.
        main_conf.write_text(
            f"error_log {scratch / 'logs' / 'error.log'} warn;\n"
            f"pid {scratch / 'nginx.pid'};\n"
            f"{load_lines}\n"
            f"{config}\n",
            encoding="utf-8",
        )
        # ``-p`` must be nginx's real prefix, not the scratch directory: the
        # distro's module .conf files use relative load_module paths, which
        # nginx resolves against the prefix. With the scratch dir as prefix,
        # nginx looked for "<scratch>/modules/ngx_stream_module.so", found
        # nothing, and reported every dynamic module as missing — which then
        # made detection claim the host lacks stream entirely.
        prefix = nginx_prefix or str(scratch)
        try:
            run([binary, "-t", "-p", prefix, "-c", str(main_conf)], check=True, timeout=30)
        except CommandError as exc:
            _log.debug("probe %s failed: %s", feature, exc)
            return False
        return True


def probe_single(
    binary: str,
    feature: str,
    modules: Optional[NginxModules] = None,
) -> bool:
    """Probe exactly one feature (used by the CLI's nginx module commands)."""
    if feature not in FEATURE_PROBES:
        raise CommandError(f"Unknown feature {feature!r}; known: {sorted(FEATURE_PROBES)}")
    config = FEATURE_PROBES[feature].replace("PROBEPORT", str(PROBE_PORT)).replace(
        "PROBEPORT2", str(PROBE_PORT_2)
    )
    load_lines = modules.load_lines() if modules else ""
    prefix = modules.prefix if modules else ""
    return _run_probe(binary, feature, config, load_lines, prefix)
