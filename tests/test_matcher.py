"""Matcher engine tests: single matchers, compound trees, normalisation."""

from __future__ import annotations

import pytest

from pg_router.config.schema import Matcher
from pg_router.model.matcher import MatcherEngine, RequestContext, normalize_path


@pytest.fixture
def engine() -> MatcherEngine:
    return MatcherEngine()


def ctx(**kwargs) -> RequestContext:
    return RequestContext(**kwargs)


def test_path_prefix_match(engine):
    matcher = Matcher.from_dict({"type": "path_prefix", "value": "/nl"})
    assert engine.evaluate(matcher, ctx(path="/nl")) is True
    assert engine.evaluate(matcher, ctx(path="/nl/10000")) is True
    assert engine.evaluate(matcher, ctx(path="/fr")) is False
    assert engine.evaluate(matcher, ctx(path="/")) is False


def test_path_prefix_does_not_match_sibling(engine):
    matcher = Matcher.from_dict({"type": "path_prefix", "value": "/nl"})
    assert engine.evaluate(matcher, ctx(path="/nlonline")) is False


def test_exact_path_match(engine):
    matcher = Matcher.from_dict({"type": "path", "value": "/nl"})
    assert engine.evaluate(matcher, ctx(path="/nl")) is True
    assert engine.evaluate(matcher, ctx(path="/nl/x")) is False


def test_path_regex_match(engine):
    matcher = Matcher.from_dict({"type": "path_regex", "value": "^/n[lr]/\\d+$"})
    assert engine.evaluate(matcher, ctx(path="/nl/10000")) is True
    assert engine.evaluate(matcher, ctx(path="/nl/abc")) is False


def test_path_normalisation_collapses_slashes(engine):
    matcher = Matcher.from_dict({"type": "path_prefix", "value": "/nl"})
    assert engine.evaluate(matcher, ctx(path="/nl//10000/")) is True


def test_normalize_path_handles_root_and_trailing_slash():
    assert normalize_path("/") == "/"
    assert normalize_path("//nl/") == "/nl"
    assert normalize_path("/nl//") == "/nl"


def test_host_match_and_wildcard(engine):
    matcher = Matcher.from_dict({"type": "host", "value": "*.example.com"})
    assert engine.evaluate(matcher, ctx(host="nl.example.com")) is True
    assert engine.evaluate(matcher, ctx(host="example.com")) is False
    assert engine.evaluate(matcher, ctx(host="x.nl.example.com")) is True


def test_host_ignore_case(engine):
    matcher = Matcher.from_dict({"type": "host", "value": "Example.com", "ignore_case": True})
    assert engine.evaluate(matcher, ctx(host="example.com")) is True


def test_sni_multiple_values(engine):
    matcher = Matcher.from_dict({"type": "sni", "values": ["a.com", "b.com"]})
    assert engine.evaluate(matcher, ctx(sni="a.com")) is True
    assert engine.evaluate(matcher, ctx(sni="b.com")) is True
    assert engine.evaluate(matcher, ctx(sni="c.com")) is False


def test_alpn_matcher(engine):
    matcher = Matcher.from_dict({"type": "alpn", "value": "h2"})
    assert engine.evaluate(matcher, ctx(alpn="h2")) is True
    assert engine.evaluate(matcher, ctx(alpn="http/1.1")) is False


def test_source_cidr_matcher(engine):
    matcher = Matcher.from_dict({"type": "source_cidr", "value": "10.0.0.0/8"})
    assert engine.evaluate(matcher, ctx(source_ip="10.4.5.6")) is True
    assert engine.evaluate(matcher, ctx(source_ip="11.0.0.1")) is False
    assert engine.evaluate(matcher, ctx()) is False


def test_compound_all(engine):
    matcher = Matcher.from_dict(
        {
            "all": [
                {"type": "host", "value": "example.com"},
                {"type": "path_prefix", "value": "/nl"},
            ]
        }
    )
    assert engine.evaluate(matcher, ctx(host="example.com", path="/nl")) is True
    assert engine.evaluate(matcher, ctx(host="example.com", path="/fr")) is False
    assert engine.evaluate(matcher, ctx(host="other.com", path="/nl")) is False


def test_compound_any(engine):
    matcher = Matcher.from_dict(
        {
            "any": [
                {"type": "sni", "value": "a.com"},
                {"type": "sni", "value": "b.com"},
            ]
        }
    )
    assert engine.evaluate(matcher, ctx(sni="a.com")) is True
    assert engine.evaluate(matcher, ctx(sni="b.com")) is True
    assert engine.evaluate(matcher, ctx(sni="c.com")) is False


def test_compound_not(engine):
    matcher = Matcher.from_dict({"not": {"type": "source_cidr", "value": "10.0.0.0/8"}})
    assert engine.evaluate(matcher, ctx(source_ip="8.8.8.8")) is True
    assert engine.evaluate(matcher, ctx(source_ip="10.1.2.3")) is False


def test_nested_compound(engine):
    matcher = Matcher.from_dict(
        {
            "all": [
                {"type": "host", "value": "example.com"},
                {
                    "any": [
                        {"type": "path_prefix", "value": "/nl"},
                        {"type": "path_prefix", "value": "/fr"},
                    ]
                },
                {"not": {"type": "source_cidr", "value": "10.0.0.0/8"}},
            ]
        }
    )
    assert engine.evaluate(matcher, ctx(host="example.com", path="/fr", source_ip="1.2.3.4")) is True
    assert engine.evaluate(matcher, ctx(host="example.com", path="/de", source_ip="1.2.3.4")) is False
    assert engine.evaluate(matcher, ctx(host="example.com", path="/fr", source_ip="10.0.0.1")) is False


def test_matcher_without_context_value_is_false(engine):
    matcher = Matcher.from_dict({"type": "sni", "value": "a.com"})
    assert engine.evaluate(matcher, ctx()) is False


def test_empty_matcher_matches_everything(engine):
    matcher = Matcher()
    assert engine.evaluate(matcher, ctx(path="/anything")) is True
