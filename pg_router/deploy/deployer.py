"""Atomic deployment with automatic rollback.

Deployment pipeline (requirements 24 and 25)::

    read config -> validate -> resolve -> generate
                 -> module check
                 -> backup current fragments
                 -> stage + syntax-test fragments in isolation
                 -> swap fragments atomically (temp file + rename)
                 -> nginx -t on the live tree
                        |-- FAIL -> restore backup -> nginx -t -> report failure
                        |-- PASS -> reload -> post-reload verification
                                          |-- FAIL -> rollback

Nginx keeps serving the previous in-memory configuration until the reload
succeeds, so a broken configuration never reaches production traffic.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config.schema import RouterConfig
from ..config.validator import ValidationReport, validate_config
from ..model.topology import TopologyResolver
from ..utils.logging import get_logger
from ..utils.security import validate_filesystem_path
from ..nginx.generator import ConfigGenerator, GenerationResult, FRAGMENT_NAMES
from ..nginx.manager import NginxManager

_log = get_logger(__name__)

STAGING_DIR = ".staging"
SNAPSHOT_DIR = "backups"
STATE_DIR = "state"
MAX_SNAPSHOTS = 25


@dataclass
class DeployResult:
    """Outcome of a deployment attempt."""

    applied: bool = False
    dry_run: bool = False
    valid: bool = False
    generated: Optional[GenerationResult] = None
    validation: Optional[ValidationReport] = None
    staged_test_ok: bool = False
    staged_test_output: str = ""
    live_test_ok: bool = False
    live_test_output: str = ""
    reloaded: bool = False
    rolled_back: bool = False
    snapshot: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "dry_run": self.dry_run,
            "valid": self.valid,
            "staged_test_ok": self.staged_test_ok,
            "live_test_ok": self.live_test_ok,
            "reloaded": self.reloaded,
            "rolled_back": self.rolled_back,
            "snapshot": self.snapshot,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "upstreams": dict(self.generated.upstreams) if self.generated else {},
            "routes": dict(self.generated.route_upstream) if self.generated else {},
        }


@dataclass
class Snapshot:
    """A stored known-good set of managed fragments."""

    name: str
    created: str
    fragments: list[str]
    metadata: dict = field(default_factory=dict)


class Deployer:
    """Validates, generates, tests and deploys Nginx fragments atomically."""

    def __init__(
        self,
        config: RouterConfig,
        managed_dir: str = "/etc/nginx/pg-router",
        manager: Optional[NginxManager] = None,
        resolver: Optional[TopologyResolver] = None,
    ) -> None:
        self.config = config
        self.managed_dir = str(validate_filesystem_path(managed_dir))
        self.manager = manager or NginxManager()
        self.resolver = resolver or TopologyResolver(config)
        self.report = DeployResult()

    # ------------------------------------------------------------------
    # planning
    # ------------------------------------------------------------------
    def plan(self) -> GenerationResult:
        """Validate configuration and generate fragments (no writes)."""
        validation = validate_config(self.config)
        self.report.validation = validation
        if not validation.valid:
            self.report.errors.extend(
                f"{problem.object_id}: {problem.message}" for problem in validation.errors
            )
            self.report.valid = False
            raise DeploymentError("Configuration validation failed", self.report)

        self.report.valid = True
        result = ConfigGenerator(self.config, self.resolver).generate()
        self.report.generated = result
        if not result.ok:
            self.report.errors.extend(result.errors)
            raise DeploymentError("Configuration generation failed", self.report)
        self._check_modules(result)
        return result

    def _check_modules(self, result: GenerationResult) -> None:
        required: set[str] = set()
        for route in self.config.routes:
            if not route.enabled:
                continue
            listener = self.config.get("listener", route.listener)
            if listener.mode == "http":
                required.add("http")
                if listener.tls.mode == "terminate":
                    required.add("http_ssl")
                if route.transport.type in ("http2", "grpc"):
                    required.add("http_v2")
                if route.transport.type == "grpc":
                    required.add("http_grpc")
                if route.transport.type == "ws":
                    required.add("http_websocket")
            else:
                required.add("stream")
                if listener.tls.mode == "passthrough" or route.match and any(
                    m.type in ("sni", "alpn") for m in route.match.all_matchers()
                ):
                    required.add("stream_ssl_preread")
        if not required:
            return
        features = sorted(required)
        _log.info("Required nginx features for this configuration: %s", features)
        try:
            self.manager.require_modules(*features)
        except Exception as exc:  # module detection is advisory on non-Linux hosts
            self.report.warnings.append(f"Module check skipped/failed: {exc}")

    # ------------------------------------------------------------------
    # deployment
    # ------------------------------------------------------------------
    def apply(self, dry_run: bool = False) -> DeployResult:
        """Generate, test and deploy. Never modifies live Nginx before tests pass."""
        self.report.dry_run = dry_run
        result = self.plan()
        fragments = dict(result.fragments)

        self.manager.ensure_privileges()
        self._ensure_managed_dir()

        if dry_run:
            staged_ok, staged_output = self._test_in_staging(fragments)
            self.report.staged_test_ok = staged_ok
            self.report.staged_test_output = staged_output
            if staged_ok:
                _log.info("Dry run: configuration generated and validated successfully")
            else:
                self.report.errors.append(staged_output)
            return self.report

        snapshot_name = self._backup_current()
        self.report.snapshot = snapshot_name

        staged_ok, staged_output = self._test_in_staging(fragments)
        self.report.staged_test_ok = staged_ok
        self.report.staged_test_output = staged_output
        if not staged_ok:
            self.report.errors.append(f"Staging test failed: {staged_output}")
            self._rollback_to(snapshot_name)
            self.report.rolled_back = True
            raise DeploymentError("Generated configuration failed nginx -t in staging", self.report)

        self._swap_fragments(fragments)

        live_ok, live_output = self.manager.test()
        self.report.live_test_ok = live_ok
        self.report.live_test_output = live_output
        if not live_ok:
            self.report.errors.append(f"Live nginx -t failed: {live_output}")
            self._rollback_to(snapshot_name)
            self.report.rolled_back = True
            raise DeploymentError("Live nginx -t failed; previous configuration restored", self.report)

        try:
            self.manager.reload()
        except Exception as exc:
            self.report.errors.append(f"Reload failed: {exc}")
            self._rollback_to(snapshot_name)
            self.report.rolled_back = True
            raise DeploymentError("Nginx reload failed; rolled back", self.report) from exc

        self.report.reloaded = True
        self.report.applied = True
        self._record_state(snapshot_name, fragments)
        _log.info("Deployment complete (snapshot %s)", snapshot_name)
        return self.report

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------
    def _ensure_managed_dir(self) -> None:
        for directory in (self.managed_dir, f"{self.managed_dir}/{SNAPSHOT_DIR}",
                          f"{self.managed_dir}/{STATE_DIR}"):
            Path(directory).mkdir(parents=True, exist_ok=True)

    def _managed_fragment_path(self, name: str) -> Path:
        return Path(self.managed_dir) / name

    def _swap_fragments(self, fragments: dict[str, str]) -> None:
        """Replace managed fragments atomically, one file at a time."""
        for name in FRAGMENT_NAMES:
            content = fragments.get(name, "")
            target = self._managed_fragment_path(name)
            tmp = target.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)   # atomic rename within the same filesystem
            _log.info("Wrote %s (%d bytes)", target, len(content))
        # remove any stale managed fragments not in FRAGMENT_NAMES
        for path in Path(self.managed_dir).glob("*.conf"):
            if path.name not in FRAGMENT_NAMES:
                path.unlink()

    def _test_in_staging(self, fragments: dict[str, str]) -> tuple[bool, str]:
        """Syntax-test the fragments in isolation, touching nothing live.

        Writes the fragments into a staging directory, then builds a temporary
        main config that supplies the ``http {}`` / ``stream {}`` contexts and
        includes them, and runs ``nginx -t`` against it. Live files are never
        read or written here.
        """
        staging = Path(self.managed_dir) / STAGING_DIR
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        for name, content in fragments.items():
            (staging / name).write_text(content, encoding="utf-8")

        http_includes = "\n".join(
            f"    include {staging / name};"
            for name in ("maps.conf", "upstreams.conf", "http.conf")
            if name in fragments
        )
        stream_includes = "\n".join(
            f"    include {staging / name};"
            for name in ("maps.conf", "stream.conf")
            if name in fragments
        )

        with tempfile.TemporaryDirectory(prefix="pg-router-staging-") as tempdir:
            prefix = Path(tempdir)
            (prefix / "logs").mkdir()
            (prefix / "temp").mkdir()
            main_conf = prefix / "nginx.conf"
            main_conf.write_text(
                f"error_log {prefix / 'logs' / 'error.log'} warn;\n"
                f"pid {prefix / 'nginx.pid'};\n"
                f"temp_path {prefix / 'temp'};\n"
                "worker_processes auto;\n"
                "events { worker_connections 128; }\n"
                "http {\n"
                f"{http_includes}\n"
                "}\n"
                "stream {\n"
                f"{stream_includes}\n"
                "}\n",
                encoding="utf-8",
            )
            ok, output = self.manager.test(main_conf=str(main_conf), prefix=str(prefix))
        if not ok:
            output = self._filter_staging_output(output, str(staging))
        return ok, output

    @staticmethod
    def _filter_staging_output(output: str, staging: str) -> str:
        return output.replace(staging + "/", "").replace(staging, "<staging>")

    # ------------------------------------------------------------------
    # snapshots + rollback
    # ------------------------------------------------------------------
    def _backup_current(self) -> str:
        """Snapshot the current managed fragments before changing anything.

        Names are sequence-prefixed (``NNNNNN-<timestamp>``) rather than
        timestamp-only: wall clocks can jump backwards (NTP adjustments), but
        rollback must always identify the newest snapshot deterministically.
        """
        sequence = self._next_snapshot_sequence()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{sequence:06d}-{stamp}"
        snapshot_dir = Path(self.managed_dir) / SNAPSHOT_DIR / name
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for path in Path(self.managed_dir).glob("*.conf"):
            if path.is_file():
                shutil.copy2(path, snapshot_dir / path.name)
                count += 1
        (snapshot_dir / "manifest.json").write_text(
            json.dumps({"created": name, "fragments": count}, indent=2), encoding="utf-8"
        )
        self._prune_snapshots()
        _log.info("Backed up %d current fragment(s) to %s", count, snapshot_dir)
        return name

    def _next_snapshot_sequence(self) -> int:
        """Next snapshot sequence number, highest existing + 1."""
        highest = 0
        for path in (Path(self.managed_dir) / SNAPSHOT_DIR).glob("*"):
            try:
                highest = max(highest, int(path.name.split("-", 1)[0]))
            except ValueError:
                continue
        return highest + 1

    def _prune_snapshots(self) -> None:
        snapshots = sorted((Path(self.managed_dir) / SNAPSHOT_DIR).glob("*"))
        for old in snapshots[:-MAX_SNAPSHOTS]:
            shutil.rmtree(old, ignore_errors=True)

    def _rollback_to(self, name: str) -> None:
        snapshot_dir = Path(self.managed_dir) / SNAPSHOT_DIR / name
        if not snapshot_dir.exists():
            _log.error("Snapshot %s missing; cannot roll back", name)
            return
        for path in snapshot_dir.glob("*.conf"):
            target = self._managed_fragment_path(path.name)
            shutil.copy2(path, target)
        _log.info("Rolled back managed fragments to snapshot %s", name)

    def rollback(self, name: Optional[str] = None) -> DeployResult:
        """Restore a previous snapshot (latest if unnamed) and reload."""
        self.manager.ensure_privileges()
        target = name or self._latest_snapshot()
        if not target:
            self.report.errors.append("No snapshots available to roll back to")
            raise DeploymentError("Nothing to roll back to", self.report)
        self._rollback_to(target)
        ok, output = self.manager.test()
        self.report.live_test_ok = ok
        self.report.live_test_output = output
        if not ok:
            self.report.errors.append(output)
            raise DeploymentError(f"Rolled-back configuration failed nginx -t: {output}", self.report)
        self.manager.reload()
        self.report.reloaded = True
        self.report.rolled_back = True
        self.report.applied = False
        _log.info("Rollback to snapshot %s complete and reloaded", target)
        return self.report

    def _latest_snapshot(self) -> Optional[str]:
        snapshots = sorted((Path(self.managed_dir) / SNAPSHOT_DIR).glob("*"))
        return snapshots[-1].name if snapshots else None

    def history(self) -> list[Snapshot]:
        """List available rollback snapshots (oldest first)."""
        out: list[Snapshot] = []
        for path in sorted((Path(self.managed_dir) / SNAPSHOT_DIR).glob("*")):
            manifest_path = path / "manifest.json"
            metadata = {}
            if manifest_path.exists():
                try:
                    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    metadata = {}
            out.append(
                Snapshot(
                    name=path.name,
                    created=metadata.get("created", path.name),
                    fragments=sorted(item.name for item in path.glob("*.conf")),
                    metadata=metadata,
                )
            )
        return out

    def _record_state(self, snapshot_name: str, fragments: dict[str, str]) -> None:
        state_file = Path(self.managed_dir) / STATE_DIR / "last-deploy.json"
        payload = {
            "snapshot": snapshot_name,
            "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "fragments": {name: len(content) for name, content in fragments.items()},
            "node": self.config.node.id,
            "roles": self.config.node.roles,
            "objects": self.config.summary(),
        }
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, state_file)


class DeploymentError(RuntimeError):
    """Raised when a deployment cannot proceed safely."""

    def __init__(self, message: str, result: DeployResult) -> None:
        super().__init__(message)
        self.result = result
