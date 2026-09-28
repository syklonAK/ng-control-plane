"""Guided configuration builder.

Splits the configuration into the sections a user actually thinks about —
node, listener, backend, tunnel, route, certificate — and asks only for the
fields that matter, with validated defaults. The output is a normal
configuration file, so nothing here is a second format: ``validate`` and
``apply`` work on it unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config.loader import load_raw, write_config
from ..utils.logging import get_logger
from ..utils.security import ValidationError

_log = get_logger(__name__)


class WizardExit(Exception):
    """Raised to leave the wizard (EOF, Ctrl-C, or a user cancel)."""


@dataclass
class Document:
    """The configuration under construction, kept as plain data."""

    path: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def open(cls, path: str | Path, create: bool = True) -> "Document":
        path = Path(path)
        if path.exists():
            return cls(path, load_raw(path))
        if not create:
            raise ValidationError(f"Configuration file does not exist: {path}")
        return cls(path, {"version": 1, "node": {"id": "node-01", "roles": ["edge"]}})

    def save(self) -> None:
        write_config(self.path, self.data)

    # ------------------------------------------------------------------
    def append(self, section: str, item: dict) -> None:
        bucket = self.data.setdefault(section, [])
        if not isinstance(bucket, list):
            raise ValidationError(f"Section '{section}' is not a list in {self.path}")
        bucket.append(item)

    def replace(self, section: str, item: dict) -> None:
        self.data[section] = item

    def ids(self, section: str) -> list[str]:
        bucket = self.data.get(section) or []
        return [str(entry.get("id")) for entry in bucket if isinstance(entry, dict) and entry.get("id")]

    def pick(self, wizard: "Wizard", section: str, label: str, allow_none: bool = False) -> Optional[str]:
        """Ask the user to pick one existing id from a section."""
        options = self.ids(section)
        if not options:
            wizard.say(f"No {section} defined yet.")
            return None
        wizard.say(f"{label}:")
        for index, option in enumerate(options, start=1):
            wizard.say(f"  {index}) {option}")
        if allow_none:
            wizard.say("  0) none")
        choice = wizard.ask("Choice", "1")
        if allow_none and choice == "0":
            return None
        try:
            index = int(choice) - 1
        except ValueError:
            wizard.say(f"Not a number: {choice}")
            return None
        if not 0 <= index < len(options):
            wizard.say("Out of range.")
            return None
        return options[index]


class Wizard:
    """Prompt wrapper: every section builder hangs off this class."""

    def __init__(
        self,
        *,
        prompt: Callable[[str], str] = input,
        write: Callable[..., None] = print,
    ) -> None:
        self._prompt = prompt
        self._write = write

    # ------------------------------------------------------------------
    def say(self, *parts) -> None:
        self._write(*parts)

    def ask(self, label: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            text = self._prompt(f"{label}{hint}> ")
        except (EOFError, KeyboardInterrupt, StopIteration) as exc:
            raise WizardExit from exc
        return text.strip() or default

    def ask_choice(self, label: str, options: list[str], default: str) -> str:
        self.say(f"{label}:")
        for index, option in enumerate(options, start=1):
            marker = " (default)" if option == default else ""
            self.say(f"  {index}) {option}{marker}")
        choice = self.ask("Choice", str(options.index(default) + 1))
        try:
            index = int(choice) - 1
        except ValueError:
            self.say(f"Not a number: {choice}; using {default}")
            return default
        if not 0 <= index < len(options):
            self.say("Out of range; using default")
            return default
        return options[index]

    def ask_int(self, label: str, default: int) -> int:
        raw = self.ask(label, str(default))
        try:
            return int(raw)
        except ValueError:
            self.say(f"Not a number: {raw}; using {default}")
            return default

    def ask_bool(self, label: str, default: bool) -> bool:
        raw = self.ask(f"{label} (y/n)", "y" if default else "n").lower()
        return raw in ("y", "yes", "true", "1")

    def pause(self) -> None:
        self.ask("Press Enter to continue")

    # ------------------------------------------------------------------
    # section builders
    # ------------------------------------------------------------------

    def build_node(self, doc: Document) -> None:
        self.say("")
        self.say("Node identity — who this server is and what role it plays.")
        node_id = self.ask("Node id", doc.data.get("node", {}).get("id", "node-01"))
        roles = self.ask("Roles, comma separated (edge, relay, hybrid, backend, gateway)", "edge")
        doc.replace("node", {"id": node_id, "roles": [r.strip() for r in roles.split(",") if r.strip()]})
        self.say(f"Node saved: {node_id}")

    def build_certificate(self, doc: Document) -> None:
        self.say("")
        self.say("Certificate — TLS material for terminating HTTPS.")
        cert_id = self.ask("Certificate id", "primary")
        provider = self.ask_choice("Provider", ["existing", "acme", "custom"], "existing")
        item = {"id": cert_id, "provider": provider}
        if provider == "existing":
            item["chain"] = self.ask("Fullchain path", "/etc/letsencrypt/live/example.com/fullchain.pem")
            item["key"] = self.ask("Private key path", "/etc/letsencrypt/live/example.com/privkey.pem")
        else:
            domains = self.ask("Domains, comma separated", "example.com")
            item["domains"] = [d.strip() for d in domains.split(",") if d.strip()]
            item["email"] = self.ask("Account email", "admin@example.com")
        doc.append("certificates", item)
        self.say(f"Certificate saved: {cert_id}")

    def build_listener(self, doc: Document) -> None:
        self.say("")
        self.say("Listener — the address and port nginx binds, and how TLS is handled.")
        listener_id = self.ask("Listener id", "public-https")
        address = self.ask("Bind address", "0.0.0.0")
        mode = self.ask_choice("Mode", ["http", "stream"], "http")
        default_port = 443 if mode == "http" else 8443
        port = self.ask_int("Port", default_port)
        item = {"id": listener_id, "address": address, "port": port, "mode": mode}

        tls_mode = self.ask_choice(
            "TLS", ["terminate", "passthrough", "disabled"], "terminate"
        )
        if tls_mode == "terminate":
            certificate = doc.pick(self, "certificates", "Which certificate") or "primary"
            item["tls"] = {"mode": "terminate", "certificate": certificate}
        elif tls_mode == "passthrough":
            item["tls"] = {"mode": "passthrough"}

        if mode == "stream":
            item["unknown_policy"] = self.ask_choice(
                "Unknown SNI", ["reject", "default"], "reject"
            )
        doc.append("listeners", item)
        self.say(f"Listener saved: {listener_id} ({address}:{port} {mode})")

    def build_backend(self, doc: Document) -> None:
        self.say("")
        self.say("Backend — where matched traffic is sent.")
        backend_id = self.ask("Backend id", "backend-01")
        kind = self.ask_choice(
            "Type",
            ["local", "remote", "tunnel", "unix_socket", "failover"],
            "local",
        )
        item: dict = {"id": backend_id, "type": kind}
        if kind in ("local", "remote"):
            item["host"] = self.ask("Host", "127.0.0.1")
            item["port"] = self.ask_int("Port", 62050)
        elif kind == "tunnel":
            tunnel = doc.pick(self, "tunnels", "Which tunnel")
            if tunnel is None:
                self.say("Create a tunnel first; backend not saved.")
                return
            item["tunnel"] = tunnel
        elif kind == "unix_socket":
            item["socket"] = self.ask("Socket path", "/var/run/backend.sock")
        elif kind == "failover":
            primary = doc.pick(self, "backends", "Primary backend")
            if primary is None:
                self.say("Create a plain backend first; failover group not saved.")
                return
            item["primary"] = primary
            backups = doc.pick(self, "backends", "Backup backend", allow_none=True)
            if backups:
                item["backups"] = [backups]
        doc.append("backends", item)
        self.say(f"Backend saved: {backend_id} ({kind})")

    def build_tunnel(self, doc: Document) -> None:
        self.say("")
        self.say("Tunnel — how this node reaches (or is reached by) another node.")
        tunnel_id = self.ask("Tunnel id", "tunnel-01")
        mode = self.ask_choice("Mode", ["reverse", "direct"], "reverse")
        provider = self.ask_choice(
            "Provider",
            ["direct", "reverse", "gost", "ssh", "wireguard", "tcp-relay", "unix-socket", "custom"],
            "custom",
        )
        item: dict = {"id": tunnel_id, "mode": mode, "provider": provider}
        if mode == "reverse":
            # The remote node dials in; nginx reaches it through a local port.
            self.say("Reverse tunnel: the other node connects to THIS server.")
            item["listener"] = {
                "address": self.ask("Local listen address", "127.0.0.1"),
                "port": self.ask_int("Local listen port", 41001),
            }
            remote_node = self.ask("Remote node id (optional)", "")
            if remote_node:
                item["remote"] = {"node": remote_node}
        else:
            # This node reaches out to the remote endpoint directly.
            self.say("Direct tunnel: THIS server connects to the other node.")
            remote_host = self.ask("Remote host", "10.0.0.20")
            item["remote"] = {
                "host": remote_host,
                "port": self.ask_int("Remote port", 40001),
            }
            remote_node = self.ask("Remote node id (optional)", "")
            if remote_node:
                item["remote"]["node"] = remote_node
        target_host = self.ask("Target host (what the tunnel delivers to)", "127.0.0.1")
        item["target"] = {"host": target_host, "port": self.ask_int("Target port", 62050)}
        doc.append("tunnels", item)
        self.say(f"Tunnel saved: {tunnel_id} ({mode})")

    def build_route(self, doc: Document) -> None:
        self.say("")
        self.say("Route — match a request and send it to a backend.")
        route_id = self.ask("Route id", "route-01")
        listener = doc.pick(self, "listeners", "Which listener")
        if listener is None:
            self.say("Create a listener first; route not saved.")
            return
        listener_obj = next(
            (item for item in doc.data.get("listeners", []) if item.get("id") == listener),
            None,
        )
        http_mode = (listener_obj or {}).get("mode") == "http"
        transport = self.ask_choice(
            "Transport",
            ["ws", "http", "grpc", "tcp", "tls"] if http_mode else ["tcp", "tls"],
            "ws" if http_mode else "tcp",
        )
        item: dict = {"id": route_id, "listener": listener, "transport": {"type": transport}}

        match_type = self.ask_choice(
            "Match on",
            ["path_prefix", "host", "sni", "none"] if http_mode else ["sni", "host", "none"],
            "path_prefix" if http_mode else "sni",
        )
        if match_type != "none":
            default_value = "/nl" if match_type == "path_prefix" else "example.com"
            item["match"] = {"type": match_type, "value": self.ask(f"{match_type} value", default_value)}

        backend = doc.pick(self, "backends", "Which backend")
        if backend is None:
            self.say("Create a backend first; route not saved.")
            return
        item["backend"] = backend
        doc.append("routes", item)
        self.say(f"Route saved: {route_id} -> {backend}")

    # ------------------------------------------------------------------
    def build_all(self, doc: Document, sections: list[str]) -> None:
        builders = {
            "node": self.build_node,
            "certificate": self.build_certificate,
            "listener": self.build_listener,
            "tunnel": self.build_tunnel,
            "backend": self.build_backend,
            "route": self.build_route,
        }
        for section in sections:
            builder = builders.get(section)
            if builder is None:
                self.say(f"Unknown section: {section}")
                continue
            try:
                builder(doc)
            except ValidationError as exc:
                self.say(f"[ERROR] {exc}")
            except WizardExit:
                self.say("")
                self.say("Wizard cancelled; nothing after this point was saved.")
                raise
            doc.save()
            self.say(f"Written to {doc.path}")
            self.pause()


# ---------------------------------------------------------------------------
# preset flows: the two real-world server shapes
# ---------------------------------------------------------------------------

ENTRY_FLOW = ["node", "certificate", "listener", "tunnel", "backend", "route"]
EXIT_FLOW = ["node", "tunnel", "backend"]


def run_wizard(path: str | Path, sections: Optional[list[str]] = None, **kwargs) -> int:
    """Run one or more section builders against ``path``.

    Accepts a ``prompt`` callable so tests can drive the flow without stdin;
    the default is ``input``.
    """
    doc = Document.open(path)
    wizard = Wizard(**kwargs)
    sections = sections or ENTRY_FLOW
    try:
        wizard.build_all(doc, sections)
    except WizardExit:
        return 130
    return 0
