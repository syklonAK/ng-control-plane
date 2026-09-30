"""Inter-process lock guarding mutating deployment operations.

``apply`` and ``rollback`` both rewrite the shared managed fragment directory
and resequence snapshots. Running them concurrently -- two shells, a menu
session plus a cron job -- corrupts that state: interleaved atomic swaps,
snapshot sequence collisions, a rollback undoing a half-applied configuration.
This module serializes those operations with an advisory file lock held for
the whole duration.

The lock is OS-level, not merely a marker file: it is released automatically
when the holder's process exits, so a crashed deploy never leaves the tool
permanently wedged. The lock file itself stays on disk and records who holds
it, so a blocked operator can see the competing operation instead of guessing
at a hang.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

from ..utils.logging import get_logger
from ..utils.security import validate_filesystem_path

_log = get_logger(__name__)

LOCK_NAME = "deploy.lock"

# OS advisory locks are per-process, not per-handle: a second lock attempt in
# the same process succeeds even when the first is still held (POSIX flock) or
# is simply a no-op (Windows byte-range locks). This registry closes that gap
# so two *different* DeployLock instances in one process still exclude each
# other, while the same instance can nest freely.
_HELD: dict[str, "DeployLock"] = {}
_registry_lock = threading.Lock()


class DeployLockBusy(RuntimeError):
    """Another process holds the deployment lock."""

    def __init__(self, holder: str) -> None:
        super().__init__(
            f"Another deployment operation is in progress ({holder}). "
            "Wait for it to finish, or remove its lock file if it crashed."
        )
        self.holder = holder


class DeployLock:
    """Exclusive advisory lock on the managed fragment directory.

    Usage::

        with DeployLock(managed_dir, operation="apply") as lock:
            ...swap fragments, snapshot, reload...

    Nesting the *same* instance is re-entrant and does not deadlock; a
    *different* instance for the same directory blocks until the first is
    released. The deployer takes the lock once at the public
    ``apply``/``rollback`` boundary, so nesting only matters for helpers that
    are sometimes called inside that boundary.
    """

    def __init__(self, managed_dir: str, *, operation: str = "deploy") -> None:
        self.path = Path(validate_filesystem_path(managed_dir)) / "state" / LOCK_NAME
        self.operation = operation
        self._fd: Optional[int] = None
        self._depth = 0

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------
    def acquire(self, timeout: float = 0.0) -> "DeployLock":
        """Take the lock, optionally waiting up to ``timeout`` seconds.

        Raises :class:`DeployLockBusy` if another holder is active.
        """
        key = str(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while True:
            # Fast path: this instance already owns it (nested entry).
            with _registry_lock:
                owner = _HELD.get(key)
                if owner is self:
                    self._depth += 1
                    return self
                description = owner._describe() if owner is not None else None

            if description is not None:
                # A different instance in this process holds it. The OS lock
                # cannot arbitrate this, so the registry must.
                if deadline is not None and time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
                raise DeployLockBusy(description)

            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                self._lock_fd(fd)
            except DeployLockBusy:
                os.close(fd)
                if deadline is not None and time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
                raise
            except BaseException:
                os.close(fd)
                raise
            with _registry_lock:
                if _HELD.get(key) is not None:
                    # Lost a race with another thread; undo and retry.
                    self._unlock_fd(fd)
                    os.close(fd)
                    continue
                _HELD[key] = self
            self._fd = fd
            self._depth = 1
            self._write_holder()
            return self

    def release(self) -> None:
        """Release the lock if this process holds it."""
        key = str(self.path)
        with _registry_lock:
            if _HELD.get(key) is self:
                self._depth -= 1
                if self._depth > 0:
                    return
                _HELD.pop(key, None)
        if self._fd is None:
            return
        try:
            self._unlock_fd(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def _describe(self) -> str:
        """Short description of this holder for competing callers."""
        return f"pid={os.getpid()} op={self.operation}"

    def __enter__(self) -> "DeployLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    # ------------------------------------------------------------------
    # platform primitives
    # ------------------------------------------------------------------
    def _lock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise DeployLockBusy(self._read_holder()) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise DeployLockBusy(self._read_holder()) from exc

    def _unlock_fd(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # holder bookkeeping (informational only; not the lock itself)
    # ------------------------------------------------------------------
    def _write_holder(self) -> None:
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        os.lseek(self._fd, 0, os.SEEK_SET)  # type: ignore[arg-type]
        payload = f"pid={os.getpid()} op={self.operation} at={started}\n".encode()
        os.write(self._fd, payload)
        os.ftruncate(self._fd, len(payload))

    def _read_holder(self) -> str:
        """Best-effort description of the current holder for error messages."""
        try:
            content = self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "unknown process"
        return content or "unknown process"

    @property
    def holder(self) -> Optional[str]:
        """Recorded holder, or ``None`` when the lock file is absent."""
        try:
            content = self.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        return content or None
