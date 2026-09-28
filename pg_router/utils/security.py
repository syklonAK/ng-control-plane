"""Input validation and sanitization.

Every value that travels from configuration into generated Nginx directives is
validated here. The generator itself only emits values that passed these
checks, so no raw user string can reach Nginx unvalidated.
"""

from __future__ import annotations

import ipaddress
import re
import string

# Safe identifiers: used for object ids, nginx upstream names, file names.
# Letters, digits, dash, underscore, dot only. Never a path separator.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)

# Header names per RFC 7230 token rules.
_HEADER_RE = re.compile(r"^[!#$%&'*+\-.^_`|~A-Za-z0-9]+$")

# Path prefix for HTTP routes: must start with /, no NUL, no backslash escapes.
_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./:%]*$")

MIN_PORT = 1
MAX_PORT = 65535

ValidationProblem = tuple[str, str]  # (object_id, reason)


class ValidationError(ValueError):
    """Raised when an input fails validation."""

    def __init__(self, message: str, object_id: str = "") -> None:
        super().__init__(message)
        self.object_id = object_id


def validate_id(value: str, object_id: str = "id") -> str:
    """Validate a safe identifier usable as a filename and nginx name."""
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValidationError(
            f"Invalid id '{value!r}': only letters, digits, '.', '-' and '_' allowed "
            f"(1-64 chars, must start alphanumeric)",
            object_id,
        )
    if value.startswith(".") or ".." in value:
        raise ValidationError(f"Invalid id '{value}': must not start with '.' or contain '..'", object_id)
    return value


def sanitize_filename(value: str) -> str:
    """Reduce an arbitrary string to a safe filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", str(value))
    cleaned = cleaned.strip(".-") or "unknown"
    return cleaned[:64]


def validate_port(value: int, object_id: str = "port") -> int:
    """Validate a TCP/UDP port number."""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid port {value!r}: not an integer", object_id) from exc
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValidationError(
            f"Invalid port {port}: must be between {MIN_PORT} and {MAX_PORT}", object_id
        )
    return port


def validate_address(value: str, object_id: str = "address") -> str:
    """Validate an IPv4/IPv6 literal or the wildcard strings."""
    if value in ("0.0.0.0", "::", "*"):
        return value
    return validate_ip(value, object_id)


def validate_ip(value: str, object_id: str = "ip") -> str:
    """Validate an IP literal (v4 or v6)."""
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid IP address {value!r}", object_id) from exc
    return value


def validate_hostname(value: str, object_id: str = "host") -> str:
    """Validate a DNS hostname or IP literal."""
    if not isinstance(value, str) or not value:
        raise ValidationError("Hostname must be a non-empty string", object_id)
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(value):
        raise ValidationError(f"Invalid hostname {value!r}", object_id)
    if len(value) > 253:
        raise ValidationError(f"Hostname too long: {value!r}", object_id)
    return value.lower()


def validate_wildcard_hostname(value: str, object_id: str = "host") -> str:
    """Validate a hostname that may carry a leading ``*.`` wildcard.

    Used by host/sni matchers, where ``*.example.com`` is a legitimate
    matching expression. Only a single leading wildcard label is permitted.
    """
    if not isinstance(value, str) or not value:
        raise ValidationError("Hostname must be a non-empty string", object_id)
    if value.startswith("*."):
        inner = value[2:]
        if not inner or "*" in inner:
            raise ValidationError(f"Invalid wildcard hostname {value!r}", object_id)
        validate_hostname(inner, object_id)
        return value.lower()
    return validate_hostname(value, object_id)


def validate_path(value: str, object_id: str = "path") -> str:
    """Validate an HTTP path used for path matching (must start with /)."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValidationError(f"Invalid path {value!r}: must start with '/'", object_id)
    if "\x00" in value or "\\" in value:
        raise ValidationError(f"Invalid path {value!r}: forbidden characters", object_id)
    if not _PATH_RE.match(value):
        raise ValidationError(f"Invalid path {value!r}: only URL-safe characters allowed", object_id)
    return value


def validate_filesystem_path(value: str, must_exist: bool = False, object_id: str = "path") -> str:
    """Validate a filesystem path; reject parent traversal beyond safe roots."""
    if not isinstance(value, str) or not value:
        raise ValidationError("Path must be a non-empty string", object_id)
    if "\x00" in value:
        raise ValidationError(f"Invalid path {value!r}: NUL byte", object_id)
    if ".." in value.split("/"):
        raise ValidationError(f"Unsafe path {value!r}: '..' components rejected", object_id)
    if must_exist and not _path_exists(value):
        raise ValidationError(f"Path does not exist: {value!r}", object_id)
    return value


def _path_exists(value: str) -> bool:
    import os

    return os.path.exists(value)


def validate_header_name(value: str, object_id: str = "header") -> str:
    """Validate an HTTP header name (proxy_set_header target)."""
    if not isinstance(value, str) or not _HEADER_RE.match(value):
        raise ValidationError(f"Invalid header name {value!r}", object_id)
    return value


# Header values that must never be attacker-controlled verbatim; we allow
# arbitrary printable ASCII minus NUL/newline/quotes to avoid directive
# injection when interpolated into nginx directives.
_SAFE_VALUE_RE = re.compile(r"^[\x20-\x7e]*$")


def validate_header_value(value: str, object_id: str = "header") -> str:
    """Validate a proxy header value; block directive-injection characters."""
    if not isinstance(value, str):
        raise ValidationError(f"Invalid header value {value!r}: not a string", object_id)
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValidationError(f"Invalid header value: control characters rejected", object_id)
    if not _SAFE_VALUE_RE.match(value):
        raise ValidationError(f"Invalid header value {value!r}: non-ASCII rejected", object_id)
    if ";" in value or "{" in value or "}" in value:
        raise ValidationError(
            f"Invalid header value {value!r}: ';', '{{', '}}' are forbidden in nginx values",
            object_id,
        )
    return value


def validate_regex(value: str, object_id: str = "regex") -> str:
    """Validate that a string is a compilable regex."""
    try:
        re.compile(value)
    except re.error as exc:
        raise ValidationError(f"Invalid regex {value!r}: {exc}", object_id) from exc
    return value


def validate_duration(value, object_id: str = "duration") -> str:
    """Validate/normalize a duration like '60s', '1w', '3600'. Returns nginx-style string."""
    if isinstance(value, (int, float)):
        return f"{int(value)}s"
    if not isinstance(value, str):
        raise ValidationError(f"Invalid duration {value!r}", object_id)
    unit = value.strip()[-1:]
    if unit in string.digits:
        return f"{int(value.strip())}s"
    if unit in ("s", "m", "h", "d", "w"):
        number = value.strip()[:-1]
        try:
            int(number)
        except ValueError as exc:
            raise ValidationError(f"Invalid duration {value!r}", object_id) from exc
        return value.strip()
    raise ValidationError(f"Invalid duration {value!r}: unknown unit {unit!r}", object_id)


def duration_seconds(value) -> float:
    """Convert a duration spec to seconds (numeric form)."""
    text = validate_duration(value)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    unit = text[-1]
    return int(text[:-1]) * units[unit]


def validate_choice(value, allowed: list[str], object_id: str = "choice"):
    """Validate value is one of the allowed enum values."""
    if value not in allowed:
        raise ValidationError(
            f"Invalid value {value!r} for {object_id}: allowed {allowed}", object_id
        )
    return value


def validate_bool(value, object_id: str = "flag") -> bool:
    """Accept true/false in several spellings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "yes", "on", "1"):
            return True
        if value.lower() in ("false", "no", "off", "0", ""):
            return False
    raise ValidationError(f"Invalid boolean {value!r}", object_id)


def is_secret_key(name: str) -> bool:
    """Heuristic for secret-looking keys that must never be logged."""
    lowered = name.lower()
    return any(s in lowered for s in ("key", "secret", "password", "token", "credential"))
