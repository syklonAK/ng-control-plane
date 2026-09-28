"""Nginx detection: version and compiled/dynamic modules.

Requirement 22: never assume a module exists just because Nginx is installed.
Detection combines three signals:

1. ``nginx -V`` configure arguments (static vs dynamic build)
2. module .so files in the distribution's module directory (dynamic)
3. a functional probe: a throwaway config exercising each directive, checked
   with ``nginx -t``. Only a passing probe marks a feature available.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
    "stream": "--with-stream_module",
    "stream_ssl": "--with-stream_ssl_module",
    "stream_ssl_preread": "--with-stream_ssl_preread_module",
}

DYNAMIC_MODULE_DIRS = (
    "/usr/lib/nginx/modules",
    "/usr/share/nginx/modules",
    "/usr/libexec/nginx/modules",
    "/etc/nginx/modules",
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
        }


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


def _find_dynamic_module(feature: str) -> Optional[str]:
    for filename in DYNAMIC_MODULE_FILES.get(feature, ()):
        for directory in DYNAMIC_MODULE_DIRS:
            path = Path(directory) / filename
            if path.exists():
                return str(path)
    return None


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

    args_text = " ".join(modules.configure_args)
    for feature in FEATURE_PROBES:
        static_flag = STATIC_FLAGS.get(feature, "")
        if static_flag and re.search(re.escape(static_flag) + r"(?!_)", args_text):
            modules.features[feature] = True
            continue
        if static_flag and f"{static_flag}=dynamic" in args_text:
            module_path = _find_dynamic_module(feature)
            if module_path:
                modules.load_modules.append(module_path)
            continue
        modules.features.setdefault(feature, False)

    # http is always present in a standard build; the probe confirms it.
    probe_result = probe_features(found, modules.load_modules)
    for feature, available in probe_result.items():
        if available:
            modules.features[feature] = True

    _log.info("Nginx detected: %s", modules.version or "unknown")
    for feature in FEATURE_PROBES:
        status = "available" if modules.supports(feature) else "missing"
        _log.info("%s: %s", feature, status)
    return modules


def probe_features(binary: str, load_modules: Optional[list[str]] = None) -> dict[str, bool]:
    """Functional probe: can this nginx actually parse each directive?

    Each probe config is written to a temporary prefix and tested with
    ``nginx -t``. Nothing is bound permanently and no live config is touched.
    """
    results: dict[str, bool] = {}
    load_lines = "\n".join(f"load_module {path};" for path in (load_modules or []))
    for feature, template in FEATURE_PROBES.items():
        config = template.replace("PROBEPORT", str(PROBE_PORT)).replace(
            "PROBEPORT2", str(PROBE_PORT_2)
        )
        results[feature] = _run_probe(binary, feature, config, load_lines)
    return results


def _run_probe(binary: str, feature: str, config: str, load_lines: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="pg-router-probe-") as tempdir:
        prefix = Path(tempdir)
        (prefix / "logs").mkdir()
        (prefix / "temp").mkdir()
        main_conf = prefix / "nginx.conf"
        main_conf.write_text(
            f"error_log {prefix / 'logs' / 'error.log'} warn;\n"
            f"pid {prefix / 'nginx.pid'};\n"
            f"temp_path {prefix / 'temp'};\n"
            f"{load_lines}\n"
            f"{config}\n",
            encoding="utf-8",
        )
        try:
            run([binary, "-t", "-p", str(prefix), "-c", str(main_conf)], check=True, timeout=30)
        except CommandError as exc:
            _log.debug("probe %s failed: %s", feature, exc)
            return False
        return True


def probe_single(binary: str, feature: str, load_modules: Optional[list[str]] = None) -> bool:
    """Probe exactly one feature (used by the CLI's nginx module commands)."""
    if feature not in FEATURE_PROBES:
        raise CommandError(f"Unknown feature {feature!r}; known: {sorted(FEATURE_PROBES)}")
    config = FEATURE_PROBES[feature].replace("PROBEPORT", str(PROBE_PORT)).replace(
        "PROBEPORT2", str(PROBE_PORT_2)
    )
    load_lines = "\n".join(f"load_module {path};" for path in (load_modules or []))
    return _run_probe(binary, feature, config, load_lines)
