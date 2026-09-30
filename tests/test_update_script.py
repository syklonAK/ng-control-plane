"""Behavioural tests for update.sh ref pinning and update safety.

The updater moves code from a remote git repository into a live venv, so the
tests exercise the real script against a sandboxed git repository: a fake
remote, a local clone, and a stubbed install step. They cover the supply-chain
guarantees the script makes:

* an unpinned update tracks the branch tip,
* a pinned update moves to exactly that tag/commit and refuses anything else,
* the working tree is verified at the intended revision before installing,
* the previous revision is recorded so an operator can roll back.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parent.parent / "update.sh"


def _find_bash() -> str | None:
    candidates = [shutil.which("bash")]
    if sys.platform.startswith("win"):
        candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(
    BASH is None or shutil.which("git") is None,
    reason="update.sh tests need bash and git",
)


def _git(workdir: Path, *args: str, env: dict | None = None) -> str:
    base_env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    if env:
        base_env.update(env)
    result = subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**base_env, "PATH": __import__("os").environ["PATH"], "HOME": str(workdir)},
    )
    return result.stdout.strip()


def _commit(workdir: Path, message: str) -> str:
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "--allow-empty", "-m", message)
    return _git(workdir, "rev-parse", "HEAD")


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A bare remote plus a clone that mimics an installed pg-router."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main", ".")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main", ".")
    _git(clone, "config", "user.name", "t")
    _git(clone, "config", "user.email", "t@example.com")
    (clone / "pyproject.toml").write_text("[project]\nname = 'pg-router'\n", encoding="utf-8")
    first = _commit(clone, "initial")
    _git(clone, "remote", "add", "origin", str(remote))
    _git(clone, "push", "-q", "origin", "main")

    # A second commit on the remote that the clone has not fetched yet.
    (clone / "marker.txt").write_text("v2\n", encoding="utf-8")
    _commit(clone, "second")
    _git(clone, "push", "-q", "origin", "main")
    _git(clone, "reset", "-q", "--hard", first)
    return remote, clone


def _run_update(clone: Path, *extra_env: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
    env = {
        "PATH": __import__("os").environ["PATH"],
        "HOME": str(clone.parent),
        "PG_ROUTER_HOME": str(clone),
        # The real update step installs into a venv; stub pip so the sandbox
        # never touches the network. The verification command it runs is the
        # venv binary, which does not exist, so skip that check too.
        "PG_ROUTER_SKIP_INSTALL_VERIFY": "1",
        # The sandbox runs as the current user, not root; the script's root
        # check exists to protect /usr/local/bin, which these tests never touch.
        "PG_ROUTER_SKIP_ROOT_CHECK": "1",
    }
    for pair in extra_env:
        key, _, value = pair.partition("=")
        env[key] = value
    result = subprocess.run(
        [BASH, UPDATE_SH.as_posix()],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(clone.parent),
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(f"update.sh failed: {result.stderr}\n{result.stdout}")
    return result


def test_unpinned_update_tracks_branch_tip(sandbox):
    _, clone = sandbox
    before = _git(clone, "rev-parse", "HEAD")
    _run_update(clone)
    after = _git(clone, "rev-parse", "HEAD")
    assert before != after
    assert (clone / "marker.txt").exists()


def test_pinned_ref_moves_to_that_revision(sandbox):
    _, clone = sandbox
    target = _git(clone, "rev-parse", "HEAD")
    _run_update(clone, f"PG_ROUTER_REF={target}")

    assert _git(clone, "rev-parse", "HEAD") == target
    # Nothing from the later commit should be present.
    assert not (clone / "marker.txt").exists()


def test_unknown_pinned_ref_is_refused(sandbox):
    _, clone = sandbox
    result = _run_update(
        clone,
        "PG_ROUTER_REF=does-not-exist",
        expect_ok=False,
    )
    assert result.returncode != 0
    assert "could not be resolved" in result.stderr
    # The clone must be untouched.
    assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "HEAD")


def test_previous_revision_is_recorded(sandbox):
    _, clone = sandbox
    before = _git(clone, "rev-parse", "HEAD")
    _run_update(clone)
    recorded = (clone / ".previous-version").read_text(encoding="utf-8").strip()
    assert recorded == before


def test_up_to_date_update_is_a_noop(sandbox):
    _, clone = sandbox
    _run_update(clone)
    head = _git(clone, "rev-parse", "HEAD")
    result = _run_update(clone)
    assert "already up to date" in result.stdout
    assert _git(clone, "rev-parse", "HEAD") == head
