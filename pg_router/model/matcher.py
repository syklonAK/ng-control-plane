"""Generic matcher engine.

Matchers are evaluated against a ``RequestContext``. The engine is used by:

* ``pg-router routes test`` (offline route simulation)
* the health manager (to know what a route expects)
* future API/UI code that needs to answer "where would this request go?"

The engine never inspects live traffic: it is a pure function over the
configuration, keeping the control plane separate from the data plane.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Optional

from ..config.schema import Matcher
from ..utils.security import ValidationError


@dataclass
class RequestContext:
    """A hypothetical incoming request, for offline route simulation."""

    host: Optional[str] = None
    path: Optional[str] = None
    sni: Optional[str] = None
    alpn: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None          # tcp | udp | tls
    transport: Optional[str] = None         # ws | grpc | tcp | ...
    source_ip: Optional[str] = None
    destination_port: Optional[int] = None
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "RequestContext":
        kwargs: dict = {}
        for key in (
            "host", "path", "sni", "alpn", "protocol", "transport", "source_ip",
        ):
            if data.get(key) is not None:
                kwargs[key] = str(data[key])
        for key in ("port", "destination_port"):
            if data.get(key) is not None:
                try:
                    kwargs[key] = int(data[key])
                except ValueError as exc:
                    raise ValidationError(f"{key} must be an integer") from exc
        return cls(**kwargs)


def normalize_path(path: str) -> str:
    """Collapse a request path the way Nginx normalisation would."""
    if not path:
        return "/"
    normalized = re.sub(r"/{2,}", "/", path)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/") or "/"
    return normalized


class MatcherEngine:
    """Evaluates a matcher tree against a request context."""

    def evaluate(self, matcher: Matcher, context: RequestContext) -> bool:
        """True if the context satisfies the matcher."""
        if matcher.all_ is not None:
            return all(self.evaluate(child, context) for child in matcher.all_)
        if matcher.any_ is not None:
            return any(self.evaluate(child, context) for child in matcher.any_)
        if matcher.not_ is not None:
            return not self.evaluate(matcher.not_, context)
        if matcher.type is None:
            return True
        candidates = matcher.values if matcher.values else ([matcher.value] if matcher.value else [])
        if not candidates:
            raise ValidationError("matcher has neither value nor values")
        if matcher.type == "path":
            request_path = context.path or "/"
            if matcher.normalize_path:
                request_path = normalize_path(request_path)
            for candidate in candidates:
                if matcher.ignore_case:
                    if request_path.lower() == candidate.lower():
                        return True
                elif request_path == candidate:
                    return True
            return False
        if matcher.type == "path_prefix":
            request_path = context.path or "/"
            if matcher.normalize_path:
                request_path = normalize_path(request_path)
            for candidate in candidates:
                # Component-wise prefix: "/nl" matches "/nl" and "/nl/10000"
                # but never "/nlonline".
                if matcher.ignore_case:
                    if request_path.lower() == candidate.lower():
                        return True
                    if request_path.lower().startswith(candidate.lower() + "/"):
                        return True
                else:
                    if request_path == candidate:
                        return True
                    if request_path.startswith(candidate + "/"):
                        return True
            return False
        if matcher.type == "path_regex":
            request_path = context.path or "/"
            flags = re.IGNORECASE if matcher.ignore_case else 0
            return any(re.search(candidate, request_path, flags) for candidate in candidates)
        if matcher.type in ("host", "sni"):
            actual = (context.host if matcher.type == "host" else context.sni) or ""
            for candidate in candidates:
                if self._host_matches(actual, candidate, matcher.ignore_case):
                    return True
            return False
        if matcher.type == "host_regex":
            actual = context.host or ""
            flags = re.IGNORECASE if matcher.ignore_case else 0
            return any(re.search(candidate, actual, flags) for candidate in candidates)
        if matcher.type == "alpn":
            return context.alpn in candidates
        if matcher.type in ("port", "destination_port"):
            value = context.port if matcher.type == "port" else context.destination_port
            return value is not None and any(value == int(candidate) for candidate in candidates)
        if matcher.type == "protocol":
            return context.protocol in candidates
        if matcher.type == "transport":
            return context.transport in candidates
        if matcher.type == "source_ip":
            return context.source_ip is not None and any(
                context.source_ip == candidate for candidate in candidates
            )
        if matcher.type == "source_cidr":
            if not context.source_ip:
                return False
            try:
                address = ipaddress.ip_address(context.source_ip)
            except ValueError:
                return False
            for candidate in candidates:
                try:
                    if address in ipaddress.ip_network(candidate, strict=False):
                        return True
                except ValueError:
                    continue
            return False
        raise ValidationError(f"Unknown matcher type {matcher.type!r}")

    @staticmethod
    def _host_matches(actual: str, expected: str, ignore_case: bool) -> bool:
        if ignore_case:
            actual, expected = actual.lower(), expected.lower()
        if actual == expected:
            return True
        # leading wildcard: *.example.com matches sub.example.com
        if expected.startswith("*."):
            suffix = expected[1:]  # .example.com
            return actual.endswith(suffix) and len(actual) > len(suffix)
        return False
