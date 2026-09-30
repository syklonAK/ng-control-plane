"""Configuration loading: YAML/JSON parsing with environment substitution.

Secrets and deployment-specific values never have to live in the YAML file:
``${VAR}`` and ``${VAR:-default}`` placeholders are expanded from the process
environment before any schema parsing happens.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from ..utils.logging import get_logger
from ..utils.security import ValidationError, validate_filesystem_path
from .schema import RouterConfig

_log = get_logger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATHS = (
    "pg-router.yaml",
    "pg-router.yml",
    "pg-router.json",
    "/etc/pg-router/config.yaml",
)

# Where the "active" configuration is remembered between invocations, so that
# `-c` is only needed once. PG_ROUTER_STATE_DIR overrides the location.
STATE_DIR = Path(
    os.environ.get("PG_ROUTER_STATE_DIR") or os.path.expanduser("~/.pg-router")
)
ACTIVE_CONFIG_FILE = STATE_DIR / "active_config"

# Databases are never written here; this is only used to back up a file before
# an interactive edit overwrites it.
EDIT_BACKUP_SUFFIX = ".bak"


def remembered_config_path() -> Optional[Path]:
    """The configuration remembered by ``config use``, if it still exists."""
    try:
        text = ACTIVE_CONFIG_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    path = Path(text)
    return path if path.is_file() else None


def remember_config(path: str | Path) -> Path:
    """Persist ``path`` as the active configuration."""
    target = Path(path).resolve()
    ACTIVE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_CONFIG_FILE.write_text(f"{target}\n", encoding="utf-8")
    return target


def forget_config() -> bool:
    """Stop remembering an active configuration. True if something was cleared."""
    try:
        ACTIVE_CONFIG_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def list_config_files(explicit: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Configuration files in the well-known locations, best match first.

    ``explicit`` is the path given through ``-c``/``PG_ROUTER_CONFIG``;
    ``active`` marks the file the next invocation without ``-c`` will load.
    """
    try:
        active = _resolve_path(explicit)
    except ValidationError:
        active = None
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    remembered = remembered_config_path()
    if remembered is not None:
        candidates.append(remembered)
    env_path = os.environ.get("PG_ROUTER_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(Path(candidate) for candidate in DEFAULT_CONFIG_PATHS)
    for pattern in ("*.yaml", "*.yml"):
        candidates.extend(sorted(Path("/etc/pg-router").glob(pattern)))

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.resolve() if candidate.exists() else candidate)
        if key in seen:
            continue
        seen.add(key)
        exists = candidate.is_file()
        entries.append(
            {
                "path": str(candidate),
                "exists": exists,
                "size": candidate.stat().st_size if exists else None,
                "active": active is not None and candidate.resolve() == active.resolve(),
            }
        )
    return entries


def substitute_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` placeholders."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_match, value)
    if isinstance(value, dict):
        return {k: substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_env(item) for item in value]
    return value


def _replace_match(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    resolved = os.environ.get(name)
    if resolved is None or resolved == "":
        if default is None:
            raise ValidationError(
                f"Environment variable {name} is required by the configuration but is not set"
            )
        return default
    return resolved


def load_raw(path: str | Path) -> dict[str, Any]:
    """Read and parse a YAML or JSON file into a plain dict."""
    path = Path(validate_filesystem_path(str(path)))
    if not path.exists():
        raise ValidationError(f"Configuration file does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid JSON in {path}: {exc}") from exc
    else:
        import yaml

        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValidationError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"Top-level configuration must be a mapping, got {type(parsed).__name__}")
    return parsed


def load_config(path: Optional[str | Path] = None) -> RouterConfig:
    """Load, env-substitute and validate a configuration document.

    Resolution order for the path: explicit argument, ``PG_ROUTER_CONFIG``
    environment variable, then a list of well-known defaults.
    """
    resolved = _resolve_path(path)
    _log.info("Loading configuration from %s", resolved)
    raw = substitute_env(load_raw(resolved))
    config = RouterConfig.from_dict(raw)
    counts = config.summary()
    _log.info(
        "Configuration loaded: listeners=%s backends=%s tunnels=%s routes=%s chains=%s inbounds=%s",
        counts["listeners"], counts["backends"], counts["tunnels"],
        counts["routes"], counts["chains"], counts["inbounds"],
    )
    return config


def _resolve_path(path: Optional[str | Path]) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("PG_ROUTER_CONFIG")
    if env_path:
        return Path(env_path)
    remembered = remembered_config_path()
    if remembered is not None:
        return remembered
    for candidate in DEFAULT_CONFIG_PATHS:
        if Path(candidate).exists():
            return Path(candidate)
    raise ValidationError(
        "No configuration file found. Pass a path, set PG_ROUTER_CONFIG, "
        f"or create one of: {', '.join(DEFAULT_CONFIG_PATHS)}"
    )


def resolve_path(path: Optional[str | Path] = None) -> Path:
    """The configuration file that would be loaded, without reading it.

    Raises :class:`ValidationError` when nothing resolves.
    """
    return _resolve_path(path)


def write_config(path: str | Path, data: dict[str, Any]) -> None:
    """Write a configuration document (YAML by suffix)."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        import yaml

        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def parse_config_string(text: str) -> RouterConfig:
    """Parse a configuration from an in-memory string (used by tests and API)."""
    import yaml

    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValidationError(f"Configuration must be a mapping, got {type(parsed).__name__}")
    return RouterConfig.from_dict(substitute_env(parsed))
