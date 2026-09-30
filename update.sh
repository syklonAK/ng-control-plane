#!/usr/bin/env bash
# pg-router — self-updater
#
# Compares the local installation against the git remote, pulls only when a
# newer revision exists, reinstalls Python dependencies, and re-links the CLI.
# Never touches generated nginx fragments, snapshots, or the live config.
#
# Usage:
#   pg-router update              # uses the installed clone at /opt/pg-router
#   ./update.sh                   # run from a dev checkout
#
# Ref pinning: by default this tracks the live tip of BRANCH. Supplying
# PG_ROUTER_REF (a tag like "v1.2.0", or a commit sha) updates to exactly that
# revision instead, so a host never silently moves to unreviewed code:
#
#   PG_ROUTER_REF=v1.2.0 pg-router update
#
# When PG_ROUTER_REF is set the updater refuses to proceed unless the remote
# actually contains that revision, and prints the commit it landed on.
set -euo pipefail

INSTALL_DIR="${PG_ROUTER_HOME:-/opt/pg-router}"
BRANCH="${PG_ROUTER_BRANCH:-main}"
REF="${PG_ROUTER_REF:-}"
BIN_LINK="/usr/local/bin/pg-router"

# Prefer the running installation; fall back to the directory of this script.
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    SRC_DIR="${INSTALL_DIR}"
elif [[ -d "$(dirname "$(readlink -f "$0")")/.git" ]]; then
    SRC_DIR="$(dirname "$(readlink -f "$0")")"
else
    echo "[ERROR] no pg-router git installation found (looked in ${INSTALL_DIR})" >&2
    exit 1
fi

if [[ "$(id -u)" -ne 0 && "${PG_ROUTER_SKIP_ROOT_CHECK:-0}" != "1" ]]; then
    echo "[ERROR] updater must run as root (prefix with sudo)" >&2
    exit 1
fi

# Two updates at once would race on the venv and leave a half-written install.
# flock is optional: where it is missing (some containers, WSL1, BusyBox) the
# update still runs, just without the concurrency guard.
LOCK_FILE="/var/run/pg-router-update.lock"
if command -v flock >/dev/null 2>&1; then
    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        echo "[ERROR] another pg-router update is already running" >&2
        exit 1
    fi
else
    echo "[WARNING] flock not found; running without an update lock" >&2
fi

echo "[INFO] pg-router updater — source: ${SRC_DIR}"

# ---------------------------------------------------------------- up-to-date check
# Fetch the branch tip (needed for the default tracking behaviour) and, when a
# pinned ref was requested, resolve it explicitly. A tag must exist on the
# remote before we are willing to move to it.
git -C "${SRC_DIR}" fetch --quiet --force origin "${BRANCH}"

LOCAL_REV="$(git -C "${SRC_DIR}" rev-parse HEAD)"

if [[ -n "${REF}" ]]; then
    # Resolve the pinned ref from the remote refs we just fetched.
    if ! REMOTE_REV="$(git -C "${SRC_DIR}" rev-parse --verify --quiet "${REF}^{commit}")"; then
        echo "[ERROR] pinned ref '${REF}' could not be resolved in ${SRC_DIR}" >&2
        echo "[ERROR] check the tag/commit exists on the remote, or unset PG_ROUTER_REF" >&2
        exit 1
    fi
    TARGET_DESC="${REF}"
else
    REMOTE_REV="$(git -C "${SRC_DIR}" rev-parse "origin/${BRANCH}")"
    TARGET_DESC="origin/${BRANCH}"
fi

if [[ "${LOCAL_REV}" == "${REMOTE_REV}" ]]; then
    echo "[INFO] already up to date (${LOCAL_REV:0:12})"
    exit 0
fi

echo "[INFO] update available:"
echo "       local  ${LOCAL_REV:0:12}"
echo "       target ${REMOTE_REV:0:12} (${TARGET_DESC})"

# ---------------------------------------------------------------- apply update
# Record the pre-update revision so the operator (or a rollback procedure) can
# see exactly what was replaced. Written *before* clean -fd runs below, so it
# is also added to that command's exclusion list.
PREV_REF_FILE="${INSTALL_DIR}/.previous-version"
git -C "${SRC_DIR}" rev-parse HEAD > "${PREV_REF_FILE}" 2>/dev/null || true

git -C "${SRC_DIR}" reset --quiet --hard "${REMOTE_REV}"
# -fd removes untracked files; the exclusions keep a developer's local config,
# any yaml lying next to the checkout, and the rollback record. Ignored
# directories (venv/) are preserved automatically.
git -C "${SRC_DIR}" clean --quiet -fd -e "*.yaml" -e "*.yml" -e "venv" -e ".previous-version"

# Confirm the working tree really is at the intended revision before any
# package is installed: a partial fetch or a rewritten ref must not slip
# unreviewed code into the venv.
ACTUAL_REV="$(git -C "${SRC_DIR}" rev-parse HEAD)"
if [[ "${ACTUAL_REV}" != "${REMOTE_REV}" ]]; then
    echo "[ERROR] working tree is at ${ACTUAL_REV:0:12}, expected ${REMOTE_REV:0:12}" >&2
    echo "[ERROR] refusing to install; inspect ${SRC_DIR} manually" >&2
    exit 1
fi

VENV_DIR="${SRC_DIR}/venv"
if [[ "${PG_ROUTER_SKIP_INSTALL_VERIFY:-0}" != "1" ]]; then
    if [[ ! -d "${VENV_DIR}" ]]; then
        # Mirror the installer's interpreter search: the system 'python3' may be
        # older than 3.10 even when a newer interpreter is installed alongside it.
        PY=""
        for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
            if command -v "${candidate}" >/dev/null 2>&1; then
                if "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
                    PY="${candidate}"
                    break
                fi
            fi
        done
        if [[ -z "${PY}" ]]; then
            echo "[ERROR] no python 3.10+ found to rebuild the virtualenv" >&2
            echo "[ERROR] run the installer again: curl -fsSL <install.sh> | sudo bash" >&2
            exit 1
        fi
        echo "[INFO] creating virtualenv with ${PY}"
        "${PY}" -m venv "${VENV_DIR}"
    fi

    VENV_PY="${VENV_DIR}/bin/python"
    VENV_BIN="${VENV_DIR}/bin/pg-router"

    echo "[INFO] reinstalling dependencies"
    "${VENV_PY}" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    # Refresh this package only, then let pip reconcile dependencies: a single
    # --force-reinstall would rebuild PyYAML and every wheel on every update.
    "${VENV_PY}" -m pip install --quiet --no-cache-dir --no-deps --force-reinstall "${SRC_DIR}"
    "${VENV_PY}" -m pip install --quiet --no-cache-dir "${SRC_DIR}"
fi

# Verify the new code through the venv *before* touching the live symlink, so
# a failed build leaves the working CLI in place instead of a dead link.
# CI sandboxes can set PG_ROUTER_SKIP_INSTALL_VERIFY=1 to test the git logic
# without a real venv; production runs must leave it unset.
if [[ "${PG_ROUTER_SKIP_INSTALL_VERIFY:-0}" != "1" ]]; then
    if ! "${VENV_BIN}" --version >/dev/null 2>&1; then
        echo "[ERROR] new version failed verification; the existing CLI was left untouched" >&2
        exit 1
    fi
fi

if [[ "${PG_ROUTER_SKIP_INSTALL_VERIFY:-0}" != "1" ]]; then
    ln -sf "${VENV_BIN}" "${BIN_LINK}"

    echo "[INFO] updated to $( "${BIN_LINK}" --version 2>&1 ) (${REMOTE_REV:0:12})"

    # ---------------------------------------------------------------- post-update check
    # The updater never rewrites generated fragments or the live config: the new
    # code may generate different fragments, and re-deploying them is an explicit,
    # reviewable operator action.
    CONFIG="${PG_ROUTER_CONFIG:-/etc/pg-router/config.yaml}"
    if [[ -f "${CONFIG}" ]]; then
        echo "[INFO] validating existing configuration against new version"
        if "${BIN_LINK}" -c "${CONFIG}" validate >/dev/null 2>&1; then
            echo "[INFO] configuration still valid — run 'pg-router -c ${CONFIG} apply' to redeploy"
        else
            echo "[WARNING] current configuration fails validation with the new version" >&2
            echo "[WARNING] run: pg-router -c ${CONFIG} validate" >&2
        fi
    else
        echo "[INFO] no config at ${CONFIG}; nothing to re-validate"
    fi
else
    echo "[INFO] install verification skipped (PG_ROUTER_SKIP_INSTALL_VERIFY=1); at ${REMOTE_REV:0:12}"
fi
