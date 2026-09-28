"""Tests for the guided configuration wizard."""

from __future__ import annotations

from pathlib import Path

import pytest

from pg_router.cli.wizard import Document, Wizard, WizardExit
from pg_router.config.loader import load_config, load_raw
from pg_router.config.validator import validate_config
from pg_router.utils.security import ValidationError


class Answers:
    """Canned prompt replies, one per call, so flows are reproducible."""

    def __init__(self, replies: list[str]):
        self._replies = iter(replies)
        self.questions: list[str] = []

    def __call__(self, question: str) -> str:
        self.questions.append(question)
        try:
            return next(self._replies)
        except StopIteration as exc:
            raise WizardExit from exc


def build(replies: list[str], path: Path, sections: list[str]) -> tuple[Wizard, Document]:
    """Run sections with canned answers.

    ``build_all`` asks the user to press Enter after each section, so every
    flow needs one trailing blank answer per section.
    """
    padded = list(replies) + [""] * len(sections)
    answers = Answers(padded)
    doc = Document.open(path)
    wizard = Wizard(prompt=answers, write=lambda *_: None)
    wizard.build_all(doc, sections)
    return wizard, doc


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


def test_document_creates_skeleton(tmp_path: Path):
    target = tmp_path / "new.yaml"
    doc = Document.open(target)
    assert doc.data["version"] == 1
    doc.append("backends", {"id": "b1"})
    doc.save()
    assert load_raw(target)["backends"] == [{"id": "b1"}]


def test_document_opens_existing_and_appends(tmp_path: Path):
    target = tmp_path / "existing.yaml"
    target.write_text("version: 1\nlisteners:\n  - {id: l1}\n", encoding="utf-8")
    doc = Document.open(target)
    assert doc.ids("listeners") == ["l1"]
    doc.append("listeners", {"id": "l2"})
    doc.save()
    assert doc.ids("listeners") == ["l1", "l2"]


def test_document_refuses_nonexistent_without_create(tmp_path: Path):
    with pytest.raises(ValidationError, match="does not exist"):
        Document.open(tmp_path / "nope.yaml", create=False)


def test_document_pick_offers_ids(tmp_path: Path, capsys):
    doc = Document.open(tmp_path / "c.yaml")
    doc.append("backends", {"id": "alpha"})
    doc.append("backends", {"id": "beta"})
    answers = Answers(["2"])
    wizard = Wizard(prompt=answers, write=print)
    picked = doc.pick(wizard, "backends", "Choose a backend")
    assert picked == "beta"
    assert "1) alpha" in capsys.readouterr().out


def test_document_pick_empty_returns_none(tmp_path: Path):
    doc = Document.open(tmp_path / "c.yaml")
    wizard = Wizard(prompt=Answers([]), write=lambda *_: None)
    assert doc.pick(wizard, "backends", "Choose") is None


# ---------------------------------------------------------------------------
# section builders
# ---------------------------------------------------------------------------


def test_build_node(tmp_path: Path):
    _, doc = build(["edge-99", "edge, relay"], tmp_path / "c.yaml", ["node"])
    assert doc.data["node"] == {"id": "edge-99", "roles": ["edge", "relay"]}


def test_build_certificate_existing(tmp_path: Path):
    _, doc = build(["cert-1", "1", "/c/full.pem", "/c/key.pem"], tmp_path / "c.yaml", ["certificate"])
    assert doc.data["certificates"] == [
        {
            "id": "cert-1",
            "provider": "existing",
            "chain": "/c/full.pem",
            "key": "/c/key.pem",
        }
    ]


def test_build_certificate_acme(tmp_path: Path):
    _, doc = build(
        ["cert-1", "2", "a.com, b.com", "me@a.com"],
        tmp_path / "c.yaml",
        ["certificate"],
    )
    assert doc.data["certificates"][0]["provider"] == "acme"
    assert doc.data["certificates"][0]["domains"] == ["a.com", "b.com"]


def test_build_listener_http_terminate(tmp_path: Path):
    _, doc = build(
        ["pub", "0.0.0.0", "1", "443", "1", ""],
        tmp_path / "c.yaml",
        ["listener"],
    )
    listener = doc.data["listeners"][0]
    assert listener["port"] == 443
    assert listener["mode"] == "http"
    assert listener["tls"] == {"mode": "terminate", "certificate": "primary"}


def test_build_listener_stream_passthrough(tmp_path: Path):
    _, doc = build(
        ["pub", "0.0.0.0", "2", "8443", "2", "1"],
        tmp_path / "c.yaml",
        ["listener"],
    )
    listener = doc.data["listeners"][0]
    assert listener["mode"] == "stream"
    assert listener["tls"] == {"mode": "passthrough"}
    assert listener["unknown_policy"] == "reject"


def test_build_tunnel_reverse(tmp_path: Path):
    _, doc = build(
        ["tun", "1", "custom", "127.0.0.1", "41001", "nl-node", "127.0.0.1", "62050"],
        tmp_path / "c.yaml",
        ["tunnel"],
    )
    tunnel = doc.data["tunnels"][0]
    assert tunnel["mode"] == "reverse"
    assert tunnel["listener"] == {"address": "127.0.0.1", "port": 41001}
    assert tunnel["remote"] == {"node": "nl-node"}
    assert tunnel["target"] == {"host": "127.0.0.1", "port": 62050}


def test_build_tunnel_direct(tmp_path: Path):
    _, doc = build(
        ["tun", "2", "custom", "10.0.0.20", "40001", "", "127.0.0.1", "62051"],
        tmp_path / "c.yaml",
        ["tunnel"],
    )
    tunnel = doc.data["tunnels"][0]
    assert tunnel["mode"] == "direct"
    assert tunnel["remote"] == {"host": "10.0.0.20", "port": 40001}
    assert "node" not in tunnel["remote"]


def test_build_backend_local(tmp_path: Path):
    _, doc = build(["b1", "1", "127.0.0.1", "62050"], tmp_path / "c.yaml", ["backend"])
    assert doc.data["backends"] == [{"id": "b1", "type": "local", "host": "127.0.0.1", "port": 62050}]


def test_build_backend_tunnel_requires_existing_tunnel(tmp_path: Path):
    doc = Document.open(tmp_path / "c.yaml")
    # Type "3" = tunnel, but no tunnel exists yet, so the wizard bails out.
    answers = Answers(["b1", "3"])
    wizard = Wizard(prompt=answers, write=lambda *_: None)
    wizard.build_backend(doc)
    assert doc.data.get("backends") is None  # nothing saved


def test_build_route_needs_listener_and_backend(tmp_path: Path):
    doc = Document.open(tmp_path / "c.yaml")
    answers = Answers(["r1"])
    wizard = Wizard(prompt=answers, write=lambda *_: None)
    wizard.build_route(doc)
    assert doc.data.get("routes") is None


def test_build_route_http_with_path_prefix(tmp_path: Path):
    doc = Document.open(tmp_path / "c.yaml")
    doc.append("listeners", {"id": "pub", "mode": "http"})
    doc.append("backends", {"id": "b1"})
    answers = Answers(["r1", "1", "1", "1", "/ws", "1", ""])
    wizard = Wizard(prompt=answers, write=lambda *_: None)
    wizard.build_route(doc)
    route = doc.data["routes"][0]
    assert route["listener"] == "pub"
    assert route["transport"] == {"type": "ws"}
    assert route["match"] == {"type": "path_prefix", "value": "/ws"}
    assert route["backend"] == "b1"


# ---------------------------------------------------------------------------
# full flows
# ---------------------------------------------------------------------------


def test_entry_preset_produces_valid_config(tmp_path: Path):
    target = tmp_path / "edge.yaml"
    replies = [
        # node
        "edge-01", "edge", "",
        # certificate (existing)
        "wildcard", "1", "/c/full.pem", "/c/key.pem", "",
        # listener http terminate
        "public-https", "0.0.0.0", "1", "443", "1", "1", "",
        # tunnel reverse
        "nl-reverse", "1", "custom", "127.0.0.1", "41001", "nl-node", "127.0.0.1", "62050", "",
        # backend tunnel (type tunnel, pick the tunnel we just made)
        "pg-nl", "2", "tunnel", "1", "",
        # route (pick listener 1, transport ws, match path_prefix /nl, backend 1)
        "nl-ws", "1", "1", "1", "/nl", "1", "",
    ]
    wizard, doc = Wizard(prompt=Answers(replies), write=lambda *_: None), Document.open(target)
    wizard.build_all(doc, ["node", "certificate", "listener", "tunnel", "backend", "route"])
    report = validate_config(load_config(target))
    assert report.valid, [str(error) for error in report.errors]
    summary = load_config(target).summary()
    assert summary["listeners"] == 1
    assert summary["backends"] == 1
    assert summary["tunnels"] == 1
    assert summary["routes"] == 1


def test_exit_preset_produces_valid_config(tmp_path: Path):
    target = tmp_path / "backend.yaml"
    replies = [
        # node
        "backend-01", "backend", "",
        # tunnel direct
        "out", "2", "custom", "10.0.0.20", "40001", "", "127.0.0.1", "62050", "",
        # backend local
        "pg-local", "1", "127.0.0.1", "62050", "",
    ]
    wizard, doc = Wizard(prompt=Answers(replies), write=lambda *_: None), Document.open(target)
    wizard.build_all(doc, ["node", "tunnel", "backend"])
    report = validate_config(load_config(target))
    assert report.valid, [str(error) for error in report.errors]


def test_wizard_appends_to_existing_config(tmp_path: Path):
    target = tmp_path / "grown.yaml"
    target.write_text("version: 1\nnode: {id: n1, roles: [edge]}\n", encoding="utf-8")
    build(["b1", "1", "127.0.0.1", "62050"], target, ["backend"])
    raw = load_raw(target)
    assert raw["node"] == {"id": "n1", "roles": ["edge"]}  # untouched
    assert raw["backends"] == [{"id": "b1", "type": "local", "host": "127.0.0.1", "port": 62050}]


def test_invalid_input_reports_and_continues(tmp_path: Path):
    target = tmp_path / "c.yaml"
    doc = Document.open(target)
    answers = Answers(["b1", "1", "127.0.0.1", "bad-port", "62050"])
    wizard = Wizard(prompt=answers, write=lambda *_: None)
    # A port that is not a number falls back to the default rather than aborting.
    wizard.build_backend(doc)
    assert doc.data["backends"][0]["port"] == 62050


def test_ctrl_c_between_sections_saves_nothing_after(tmp_path: Path):
    target = tmp_path / "c.yaml"
    replies = ["edge-01", "edge"]
    answers = Answers(replies)
    doc = Document.open(target)
    wizard = Wizard(prompt=answers, write=lambda *_: None)
    with pytest.raises(WizardExit):
        wizard.build_all(doc, ["node", "certificate"])
    assert load_raw(target)["node"] == {"id": "edge-01", "roles": ["edge"]}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_wizard_builds_and_validates(tmp_path: Path, monkeypatch):
    from pg_router.cli.app import main as cli_main

    target = tmp_path / "from-cli.yaml"
    replies = [
        # node
        "edge-cli", "edge", "",
        # tunnel direct
        "out", "2", "custom", "10.0.0.20", "40001", "", "127.0.0.1", "62050", "",
        # backend local
        "pg-local", "1", "127.0.0.1", "62050", "",
    ]
    answers = Answers(replies)
    monkeypatch.setattr("builtins.input", answers)
    code = cli_main(["wizard", "--path", str(target), "--preset", "exit"])
    assert code == 0
    assert target.exists()
    report = validate_config(load_config(target))
    assert report.valid, [str(error) for error in report.errors]
