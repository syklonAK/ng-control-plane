"""Human-readable diff of generated Nginx fragments.

``apply`` and ``generate --dry-run`` show what would change on the live tree
before anything is written, so an operator can review a risky change while the
old configuration is still serving traffic.

The diff is deliberately line-based and dependency-free: the fragments are
generated configuration files, so ``difflib.unified_diff`` is the right tool,
and the output must stay readable in a terminal and in a menu.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from ..nginx.generator import FRAGMENT_NAMES

# Fragments nginx loads unconditionally once any route exists; a config that
# produces no routes still emits an empty marker here rather than vanishing.
ALWAYS_REPORTED = ("maps.conf", "upstreams.conf", "http.conf", "stream.conf")


def _read_live(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []


def diff_fragments(
    managed_dir: str,
    generated: dict[str, str],
    *,
    names: tuple[str, ...] = ALWAYS_REPORTED,
    context: int = 3,
) -> str:
    """Unified diff of live fragments vs the freshly generated set.

    Returns a single string, empty when the deployment would change nothing.
    Fragments are always reported in a stable order so repeated runs of the
    same configuration produce byte-identical output (idempotency matters:
    an operator should be able to trust "no diff" as "no change").
    """
    base = Path(managed_dir)
    ordered = [name for name in names if name in FRAGMENT_NAMES]
    ordered += sorted(set(generated) - set(ordered))
    lines: list[str] = []
    changed = 0

    for name in ordered:
        live = _read_live(base / name)
        new_text = generated.get(name, "")
        # An absent generated fragment means the live one would be deleted.
        if name not in generated:
            if not live:
                continue  # nothing live, nothing to remove
            new: list[str] = []
        else:
            new = new_text.splitlines()

        if live == new:
            continue
        changed += 1
        lines.extend(
            difflib.unified_diff(
                live,
                new,
                fromfile=f"live: {name}",
                tofile=f"new: {name}",
                lineterm="",
                n=context,
            )
        )
        lines.append("")

    summary = f"{changed} fragment(s) changed" if changed else "no changes"
    lines.append(f"# {summary}")
    return "\n".join(lines).rstrip("\n") + "\n"
