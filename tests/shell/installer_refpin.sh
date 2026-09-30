#!/usr/bin/env bash
# Unit tests for install_sources(): ref pinning and the working-tree check.
#
# install.sh is sourced like in installer_units.sh, then install_sources is
# driven against a fake git remote in a sandbox. The git operations are real;
# only the network is stubbed (the "remote" is a local bare repository).
#
# usage: installer_refpin.sh <path/to/install.sh>
set -uo pipefail

install_sh="$1"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0

check() {
    if [[ "$2" == "$3" ]]; then
        echo "ok   $1"
    else
        echo "FAIL $1: expected [$3] got [$2]"
        failures=$((failures + 1))
    fi
}

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com \
       GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com

remote="$work/remote.git"
git init --quiet --bare -b main "$remote"

# Two commits on the remote: v1 (base) and v2 (later).
content="$work/content"
git init --quiet -b main "$content"
printf '[project]\nname="pg-router"\n' > "$content/pyproject.toml"
git -C "$content" add -A
git -C "$content" commit --quiet -m v1
git -C "$content" remote add origin "$remote"
git -C "$content" push --quiet origin main
v1="$(git -C "$content" rev-parse HEAD)"

echo second > "$content/marker.txt"
git -C "$content" add -A
git -C "$content" commit --quiet -m v2
git -C "$content" push --quiet origin main
v2="$(git -C "$content" rev-parse HEAD)"

# shellcheck source=/dev/null
source "$install_sh"
set +euo pipefail

INSTALL_DIR="$work/install"
REPO_URL="$remote"
BRANCH="main"

# ------------------------------------------------------- fresh unpinned clone
PG_ROUTER_REF="" install_sources >/dev/null 2>&1
check "unpinned clone lands on branch tip" \
    "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$v2"
check "unpinned clone has v2 marker" \
    "$([ -f "$INSTALL_DIR/marker.txt" ] && echo yes || echo no)" "yes"

# ------------------------------------------------------- fresh pinned clone
rm -rf "$INSTALL_DIR"
PG_ROUTER_REF="$v1" install_sources >/dev/null 2>&1
check "pinned clone lands on the pinned commit" \
    "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$v1"
check "pinned clone does not fetch v2" \
    "$([ -f "$INSTALL_DIR/marker.txt" ] && echo yes || echo no)" "no"

# ------------------------------------------------- pinned update of an install
# Now the install sits at v1 and an unpinned update should move it to v2.
PG_ROUTER_REF="" install_sources >/dev/null 2>&1
check "unpinned update moves to the tip" \
    "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$v2"
check "unpinned update fetches the marker" \
    "$([ -f "$INSTALL_DIR/marker.txt" ] && echo yes || echo no)" "yes"

# A pinned update back to v1 rewinds cleanly.
PG_ROUTER_REF="$v1" install_sources >/dev/null 2>&1
check "pinned update rewinds to v1" \
    "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$v1"

# ------------------------------------------------------- unknown ref refused
# The script must exit non-zero and must not move the tree.
if PG_ROUTER_REF="no-such-ref" install_sources >/dev/null 2>&1; then
    check "unknown ref exits non-zero" "0" "non-zero"
else
    check "unknown ref exits non-zero" "non-zero" "non-zero"
fi
check "unknown ref leaves the tree at v1" \
    "$(git -C "$INSTALL_DIR" rev-parse HEAD)" "$v1"

echo
if [[ "$failures" -eq 0 ]]; then
    echo "ALL PASSED"
    exit 0
fi
echo "$failures FAILURES"
exit 1
