"""Service layer.

All business logic lives here. The CLI is a thin adapter over these services,
which is what allows a future REST API, Telegram bot or web UI to drive the
same operations without duplicating logic (requirement 30).
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ..config.loader import load_config, write_config
from ..config.schema import RouterConfig
from ..config.validator import ValidationReport, validate_config
from ..deploy.deployer import DeployResult, Deployer
from ..deploy.health import HealthManager
from ..model.matcher import MatcherEngine, RequestContext
from ..model.topology import TopologyResolver
from ..nginx.generator import GenerationResult
from ..nginx.installer import InstallReport, NginxInstaller
from ..nginx.manager import NginxManager
from ..plugins.registry import tunnels as tunnel_registry
from ..utils.logging import get_logger
from ..utils.security import ValidationError
from ..utils.system import CommandError

_log = get_logger(__name__)

DEFAULT_MANAGED_DIR = os.environ.get("PG_ROUTER_DATA_DIR", "/etc/nginx/pg-router")


class ConfigService:
    """Loads, validates and initialises configuration documents."""

    def __init__(self, config_path: Optional[str] = None, managed_dir: Optional[str] = None) -> None:
        self.config_path = config_path
        self.managed_dir = managed_dir or DEFAULT_MANAGED_DIR
        self._config: Optional[RouterConfig] = None

    def load(self, reload: bool = False) -> RouterConfig:
        if self._config is None or reload:
            self._config = load_config(self.config_path)
        return self._config

    def validate(self) -> ValidationReport:
        # The CLI renders the report itself, so logging here would duplicate it.
        return validate_config(self.load(), log=False)

    def summary(self) -> dict:
        config = self.load()
        summary = config.summary()
        summary["node"] = asdict(config.node)
        summary["version"] = config.version
        return summary

    def init_config(
        self,
        path: Optional[str] = None,
        interactive: bool = False,
        values: Optional[dict] = None,
    ) -> Path:
        """Write a starter configuration. Non-interactive when ``values`` given."""
        target = Path(path or self.config_path or "pg-router.yaml")
        if target.exists():
            raise ValidationError(f"Configuration already exists at {target}")
        defaults = {
            "node": {"id": "edge-01", "roles": ["edge"]},
            "certificates": [
                {
                    "id": "primary",
                    "provider": "existing",
                    "chain": "/etc/letsencrypt/live/example.com/fullchain.pem",
                    "key": "/etc/letsencrypt/live/example.com/privkey.pem",
                }
            ],
            "listeners": [
                {"id": "public-https", "address": "0.0.0.0", "port": 443, "mode": "http",
                 "tls": {"mode": "terminate", "certificate": "primary"}},
            ],
            "backends": [{"id": "local-pg", "type": "local", "host": "127.0.0.1", "port": 62050}],
            "tunnels": [],
            "routes": [
                {
                    "id": "local-ws",
                    "listener": "public-https",
                    "transport": {"type": "ws"},
                    "match": {"type": "path_prefix", "value": "/ws"},
                    "backend": "local-pg",
                }
            ],
        }
        provided = values or {}
        if interactive and not provided:
            provided = self._prompt_values()
        defaults.update(provided)
        defaults["version"] = 1
        write_config(target, defaults)
        _log.info("Initial configuration written to %s", target)
        return target

    @staticmethod
    def _prompt_values() -> dict:
        def ask(prompt: str, default: str = "") -> str:
            text = input(f"  {prompt} [{default}]: ").strip()
            return text or default

        node_id = ask("Node id", "edge-01")
        roles = ask("Roles (comma separated)", "edge")
        domain = ask("Public domain", "example.com")
        backend_port = ask("Local backend port", "62050")
        return {
            "node": {"id": node_id, "roles": [r.strip() for r in roles.split(",") if r.strip()]},
            "listeners": [
                {"id": "public-https", "address": "0.0.0.0", "port": 443, "mode": "http",
                 "tls": {"mode": "terminate"}},
            ],
            "backends": [
                {"id": "local-pg", "type": "local", "host": "127.0.0.1", "port": int(backend_port)},
            ],
            "tunnels": [],
            "routes": [
                {"id": "local-ws", "listener": "public-https",
                 "transport": {"type": "ws"},
                 "match": {"type": "path_prefix", "value": f"/{node_id}"},
                 "backend": "local-pg"},
            ],
            "certificates": [
                {"id": "primary", "provider": "existing",
                 "chain": f"/etc/letsencrypt/live/{domain}/fullchain.pem",
                 "key": f"/etc/letsencrypt/live/{domain}/privkey.pem"},
            ],
        }


class RouterService:
    """Route introspection and offline route simulation."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self.engine = MatcherEngine()
        self.resolver = TopologyResolver(config)

    def list_routes(self) -> list[dict]:
        out: list[dict] = []
        for route in self.config.routes:
            listener = self.config.get("listener", route.listener) if self.config.has("listener", route.listener) else None
            entry = {
                "id": route.id,
                "enabled": route.enabled,
                "listener": route.listener,
                "listener_mode": listener.mode if listener else None,
                "transport": route.transport.type,
                "layer": route.transport.layer,
                "backend": route.backend,
                "chain": route.chain,
                "fallback": route.fallback,
                "priority": route.priority,
                "matcher": self._describe_matcher(route.match) if route.match else None,
            }
            if self.config.has("backend", route.backend or ""):
                try:
                    endpoints = self.resolver.resolve_backend(route.backend)
                    entry["endpoints"] = [endpoint.address for endpoint in endpoints]
                except ValidationError:
                    entry["endpoints"] = []
            out.append(entry)
        return out

    @staticmethod
    def _describe_matcher(matcher) -> Optional[dict]:
        if matcher is None:
            return None
        if matcher.all_ is not None:
            return {"all": [RouterService._describe_matcher(child) for child in matcher.all_]}
        if matcher.any_ is not None:
            return {"any": [RouterService._describe_matcher(child) for child in matcher.any_]}
        if matcher.not_ is not None:
            return {"not": RouterService._describe_matcher(matcher.not_)}
        return {"type": matcher.type, "value": matcher.value, "values": matcher.values}

    def list_backends(self) -> list[dict]:
        out: list[dict] = []
        for backend in self.config.backends:
            entry = {
                "id": backend.id,
                "type": backend.type,
                "host": backend.host,
                "port": backend.port,
                "tunnel": backend.tunnel,
                "socket": backend.socket,
                "primary": backend.primary,
                "backups": backend.backups,
                "down": backend.down,
                "weight": backend.weight,
            }
            try:
                endpoints = self.resolver.resolve_backend(backend.id)
                entry["resolved"] = [
                    {"address": endpoint.address, "role": endpoint.role, "tunnel": endpoint.tunnel_id}
                    for endpoint in endpoints
                ]
            except ValidationError as exc:
                entry["resolved"] = []
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def list_chains(self) -> list[dict]:
        return [
            {
                "id": chain.id,
                "hops": [{"type": hop.type, "id": hop.id} for hop in chain.hops],
            }
            for chain in self.config.chains
        ]

    def test_request(self, request: dict) -> list[dict]:
        """Simulate a request: which routes would match, in priority order."""
        context = RequestContext.from_dict(request)
        matches: list[dict] = []
        for route in self.config.routes:
            if not route.enabled or route.match is None:
                continue
            if not self.engine.evaluate(route.match, context):
                continue
            listener = self.config.get("listener", route.listener)
            entry = {
                "route": route.id,
                "listener": route.listener,
                "listener_mode": listener.mode if listener else None,
                "transport": route.transport.type,
                "backend": route.backend,
            }
            if route.backend:
                try:
                    endpoints = self.resolver.resolve_backend(route.backend)
                    entry["endpoints"] = [endpoint.address for endpoint in endpoints]
                except ValidationError as exc:
                    entry["error"] = str(exc)
            matches.append(entry)
        return matches


class TunnelService:
    """Tunnel introspection through the provider registry."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config

    def list_tunnels(self) -> list[dict]:
        out: list[dict] = []
        for tunnel in self.config.tunnels:
            entry = {
                "id": tunnel.id,
                "mode": tunnel.mode,
                "provider": tunnel.provider,
                "remote_node": tunnel.remote_node,
                "listener": (
                    f"{tunnel.listener_address}:{tunnel.listener_port}"
                    if tunnel.listener_address else None
                ),
                "remote": (
                    f"{tunnel.remote_host}:{tunnel.remote_port}"
                    if tunnel.remote_host else None
                ),
                "target": (
                    f"{tunnel.target_host}:{tunnel.target_port}"
                    if tunnel.target_host else None
                ),
            }
            try:
                provider = tunnel_registry.get(tunnel.provider)
                entry["endpoint"] = provider.endpoint(tunnel).address if hasattr(
                    provider.endpoint(tunnel), "address"
                ) else str(provider.endpoint(tunnel))
                entry["describe"] = provider.describe(tunnel)
            except (KeyError, ValueError) as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return list(out)

    def status(self) -> list[dict]:
        manager = HealthManager(self.config)
        results = manager.check_once()
        out: list[dict] = []
        for tunnel in self.config.tunnels:
            result = results.get(f"tunnel:{tunnel.id}")
            out.append(
                {
                    "id": tunnel.id,
                    "mode": tunnel.mode,
                    "provider": tunnel.provider,
                    "up": result.up if result else None,
                    "latency_ms": result.latency_ms if result else None,
                    "error": result.error if result else None,
                }
            )
        return out


class NginxService:
    """Nginx installation, detection and service operations."""

    def __init__(self, managed_dir: Optional[str] = None, manager: Optional[NginxManager] = None) -> None:
        self.managed_dir = managed_dir or DEFAULT_MANAGED_DIR
        self.manager = manager or NginxManager()

    def install(self, features: Optional[list[str]] = None) -> InstallReport:
        installer = NginxInstaller(self.managed_dir)
        report = installer.install(features)
        if report.created_directories or report.already_installed:
            self.manager.ensure_managed_include(self.managed_dir, (), dry_run=False)
        return report

    def detect(self) -> dict:
        return self.manager.modules.as_dict()

    def status(self) -> dict:
        return self.manager.status().as_dict()

    def test(self) -> tuple[bool, str]:
        return self.manager.test()

    def reload(self) -> None:
        self.manager.reload()

    def restart(self) -> None:
        self.manager.restart()

    def wire_includes(self, dry_run: bool = False) -> bool:
        return self.manager.ensure_managed_include(self.managed_dir, (), dry_run=dry_run)


class HealthService:
    """Health checks and failover state."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self.manager = HealthManager(config)

    def check(self) -> dict:
        return self.manager.summary()

    def failover(self) -> dict:
        changed = self.manager.apply_failover_state()
        return {"changed": changed, "summary": self.manager.summary()}


class DeployService:
    """Atomic generation, deployment and rollback."""

    def __init__(
        self,
        config: RouterConfig,
        managed_dir: Optional[str] = None,
        manager: Optional[NginxManager] = None,
    ) -> None:
        self.config = config
        self.managed_dir = managed_dir or DEFAULT_MANAGED_DIR
        self.manager = manager or NginxManager()
        self.resolver = TopologyResolver(config)

    def generate(self, dry_run: bool = False) -> GenerationResult:
        """Generate and syntax-check fragments without touching live Nginx."""
        deployer = Deployer(self.config, self.managed_dir, self.manager, self.resolver)
        result = deployer.plan()
        if dry_run:
            try:
                staged_ok, output = deployer._test_in_staging(dict(result.fragments))
                if not staged_ok:
                    result.errors.append(output)
            except CommandError as exc:
                # Dry run must stay usable on hosts without nginx installed
                # (e.g. inspecting fragments from a workstation).
                result.warnings.append(
                    f"nginx unavailable, fragment syntax not verified: {exc}"
                )
        return result

    def apply(self, dry_run: bool = False, check_health: bool = False) -> DeployResult:
        if check_health:
            health = HealthManager(self.config, self.resolver)
            health.check_once()
            health.apply_failover_state()
        deployer = Deployer(self.config, self.managed_dir, self.manager, self.resolver)
        return deployer.apply(dry_run=dry_run)

    def rollback(self, name: Optional[str] = None) -> DeployResult:
        deployer = Deployer(self.config, self.managed_dir, self.manager, self.resolver)
        return deployer.rollback(name)

    def history(self) -> list[dict]:
        deployer = Deployer(self.config, self.managed_dir, self.manager, self.resolver)
        return [asdict(snapshot) for snapshot in deployer.history()]

    def status(self) -> dict:
        return {
            "nginx": self.manager.status().as_dict(),
            "objects": self.config.summary(),
        }


__all__ = [
    "ConfigService",
    "DeployService",
    "HealthService",
    "NginxService",
    "RouterService",
    "TunnelService",
]
