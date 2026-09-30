"""Compilation of matcher trees into nginx-expressible selections.

The simulation engine (:mod:`pg_router.model.matcher`) evaluates the full
matcher tree exactly as written — including ``all``/``any``/``not`` and the
simulation-only types. Nginx cannot express most of that. This module is the
honest translation layer: it turns a matcher tree into the small set of nginx
constructs a route can actually occupy, and **refuses to compile** anything it
cannot represent faithfully.

Refusal is the whole point. Previously the generator flattened the tree with
``Matcher.all_matchers()`` and kept whatever looked useful, which silently:

* dropped every ``path`` value but the last under ``any:`` (nginx got one
  ``location`` where the operator asked for several);
* treated a negated matcher (``not: {sni: ...}``) as a *positive* match, so a
  host an operator excluded got routed to that very route;
* dropped client-address matchers on HTTP routes without a word, so a route
  believed restricted to a CIDR was open to everyone.

Compilation rules (each derived from what a single nginx ``server``/``location``
or stream ``server`` can hold):

* ``all:`` of matchers with *distinct* types -> the intersection, e.g.
  ``host`` + ``path_prefix`` (server_name + location). Repeating a type is
  unsatisfiable for the same request and is rejected.
* ``any:`` of matchers of the *same* type -> the union of their values, e.g.
  two ``path`` values become two ``location`` blocks sharing one upstream.
  ``any`` across different types has no nginx representation and is rejected.
* ``not:`` has no negative counterpart in nginx location/server_name matching
  and is always rejected.
* Nesting a compound inside a compound is rejected (no faithful translation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config.schema import Matcher, Route
from ..utils.security import ValidationError
from .capabilities import HTTP, STREAM, capability, describe_unenforced

PATH_TYPES = ("path", "path_prefix", "path_regex")
HOST_TYPES = ("host", "host_regex")
STREAM_MATCH_TYPES = ("sni", "alpn")


class MatcherCompileError(ValidationError):
    """A matcher tree cannot be represented in nginx configuration."""


@dataclass(frozen=True)
class HostSpec:
    """One ``server_name`` entry."""

    kind: str           # exact | regex
    value: str


@dataclass(frozen=True)
class LocationSpec:
    """One nginx ``location`` occupied by the route."""

    modifier: str       # "" | "=" | "~"
    value: str
    matcher_type: str   # path | path_prefix | path_regex


@dataclass
class HttpSelection:
    """What an HTTP route occupies in nginx: a host set and locations."""

    hosts: tuple[HostSpec, ...] = ()
    locations: tuple[LocationSpec, ...] = ()
    unenforced: tuple[str, ...] = ()

    @property
    def is_default_server(self) -> bool:
        """No host matcher: the route lands in the catch-all server block."""
        return not self.hosts

    def location_keys(self) -> list[str]:
        """Canonical nginx location keys, for ambiguity detection."""
        if not self.locations:
            return ["=/"]
        keys = []
        for location in self.locations:
            if location.modifier == "=":
                keys.append(f"={location.value}")
            elif location.modifier == "~":
                keys.append(f"~{location.value}")
            else:
                keys.append(f"^{location.value}")
        return keys


@dataclass
class StreamSelection:
    """What a stream route occupies in nginx: one preread selector."""

    kind: str = "sni"               # sni | alpn
    values: tuple[str, ...] = ()
    unenforced: tuple[str, ...] = ()


@dataclass
class RouteSelection:
    """A compiled matcher tree for one route, in the route's listener mode."""

    route_id: str
    mode: str
    http: Optional[HttpSelection] = None
    stream: Optional[StreamSelection] = None

    @property
    def unenforced(self) -> tuple[str, ...]:
        if self.http is not None:
            return self.http.unenforced
        if self.stream is not None:
            return self.stream.unenforced
        return ()


def compile_route_match(
    route: Route,
    listener_mode: str,
    allow_unenforced: bool = False,
) -> RouteSelection:
    """Compile a route's matcher for the given listener mode.

    Raises :class:`MatcherCompileError` when the tree cannot be represented in
    nginx, and when the route uses simulation-only matchers it does not
    acknowledge (``unenforced_matchers: allow``).
    """
    groups = _collect(route.match, route.id)
    if listener_mode == HTTP:
        selection = RouteSelection(route.id, HTTP, http=_build_http(groups, route.id))
    elif listener_mode == STREAM:
        selection = RouteSelection(route.id, STREAM, stream=_build_stream(groups, route.id))
    else:  # pragma: no cover - schema restricts modes to these two
        raise MatcherCompileError(
            f"unknown listener mode {listener_mode!r}", route.id
        )

    unenforced = selection.unenforced
    if unenforced and not allow_unenforced:
        raise MatcherCompileError(
            f"matcher(s) {sorted(unenforced)} are not enforced in generated nginx "
            f"config ({describe_unenforced(list(unenforced))}); they are only "
            "evaluated by 'pg-router routes test'. Remove them, or set "
            "'unenforced_matchers: allow' on the route to keep them for "
            "simulation only",
            route.id,
        )
    return selection


# ---------------------------------------------------------------------------
# tree reduction
# ---------------------------------------------------------------------------


def _collect(matcher: Optional[Matcher], obj_id: str) -> dict[str, list[str]]:
    """Reduce the matcher tree to ``{matcher_type: [values]}`` or refuse.

    The structure of the tree is what makes it expressible or not, so the
    decision is made here, while values are still grouped by type.
    """
    if matcher is None:
        return {}
    if matcher.all_ is not None:
        groups: dict[str, list[str]] = {}
        for child in matcher.all_:
            if child.is_compound:
                raise MatcherCompileError(
                    "a matcher nested inside 'all' cannot be expressed in nginx",
                    obj_id,
                )
            for matcher_type, values in _leaf_values(child, obj_id).items():
                if matcher_type in groups:
                    raise MatcherCompileError(
                        f"'all' repeats matcher type {matcher_type!r}; a request "
                        "cannot satisfy two different values of it at once",
                        obj_id,
                    )
                groups[matcher_type] = list(values)
        return groups
    if matcher.any_ is not None:
        groups = {}
        for child in matcher.any_:
            if child.is_compound:
                raise MatcherCompileError(
                    "a matcher nested inside 'any' cannot be expressed in nginx",
                    obj_id,
                )
            for matcher_type, values in _leaf_values(child, obj_id).items():
                groups.setdefault(matcher_type, []).extend(values)
        if len(groups) > 1:
            raise MatcherCompileError(
                "'any' combines different matcher types "
                f"({sorted(groups)}); nginx cannot express a union of them",
                obj_id,
            )
        return groups
    if matcher.not_ is not None:
        raise MatcherCompileError(
            "negated matchers ('not:') have no counterpart in nginx location or "
            "server_name matching and are refused; express the positive set "
            "instead (for example route the complement on another route)",
            obj_id,
        )
    return _leaf_values(matcher, obj_id)


def _leaf_values(matcher: Matcher, obj_id: str) -> dict[str, list[str]]:
    if matcher.type is None:
        raise MatcherCompileError("matcher without a type", obj_id)
    values = matcher.values if matcher.values else (
        [matcher.value] if matcher.value is not None else []
    )
    if not values:
        raise MatcherCompileError(
            f"matcher type {matcher.type!r} needs a value or values", obj_id
        )
    return {matcher.type: [str(value) for value in values]}


# ---------------------------------------------------------------------------
# selection builders
# ---------------------------------------------------------------------------


def _build_http(groups: dict[str, list[str]], obj_id: str) -> HttpSelection:
    hosts: list[HostSpec] = []
    locations: list[LocationSpec] = []
    unenforced: list[str] = []

    for matcher_type in HOST_TYPES:
        for value in groups.get(matcher_type, []):
            hosts.append(
                HostSpec("regex" if matcher_type == "host_regex" else "exact",
                         value.lower() if matcher_type == "host" else value)
            )

    present_path_types = [t for t in PATH_TYPES if groups.get(t)]
    if len(present_path_types) > 1:
        raise MatcherCompileError(
            f"matchers {present_path_types} cannot be combined: a request has one "
            "path, so only one path matcher per route is meaningful",
            obj_id,
        )
    for matcher_type in present_path_types:
        for value in groups[matcher_type]:
            locations.append(_location_spec(matcher_type, value))

    for matcher_type, values in groups.items():
        if not capability(matcher_type).enforced_in(HTTP):
            unenforced.extend([matcher_type] * len(values))

    return HttpSelection(
        hosts=tuple(hosts),
        locations=tuple(locations),
        unenforced=tuple(unenforced),
    )


def _location_spec(matcher_type: str, value: str) -> LocationSpec:
    if matcher_type == "path":
        return LocationSpec("=", value, matcher_type)
    if matcher_type == "path_regex":
        return LocationSpec("~", value, matcher_type)
    # A prefix of "/" is the whole namespace; nginx spells it "location /".
    return LocationSpec("", value, matcher_type)


def _build_stream(groups: dict[str, list[str]], obj_id: str) -> StreamSelection:
    present = [t for t in STREAM_MATCH_TYPES if groups.get(t)]
    if len(present) > 1:
        raise MatcherCompileError(
            "a stream route cannot combine SNI and ALPN matching: one nginx "
            "`server` proxies to a single selector variable. Use separate "
            "listeners or separate routes",
            obj_id,
        )

    unenforced: list[str] = []
    for matcher_type, values in groups.items():
        if not capability(matcher_type).enforced_in(STREAM):
            unenforced.extend([matcher_type] * len(values))

    if not present:
        # Matcher present but only simulation-only types: no preread selector.
        return StreamSelection(kind="sni", values=(), unenforced=tuple(unenforced))

    kind = present[0]
    values = groups[kind]
    return StreamSelection(
        kind=kind,
        values=tuple(value.lower() if kind == "sni" else value for value in values),
        unenforced=tuple(unenforced),
    )
