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
set -euo pipefail

INSTALL_DIR="${PG_ROUTER_HOME:-/opt/pg-router}"
BRANCH="${PG_ROUTER_BRANCH:-main}"
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

if [[ "$(id -u)" -ne 0 ]]; then
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
git -C "${SRC_DIR}" fetch --quiet --force origin "${BRANCH}"

LOCAL_REV="$(git -C "${SRC_DIR}" rev-parse HEAD)"
REMOTE_REV="$(git -C "${SRC_DIR}" rev-parse "origin/${BRANCH}")"

if [[ "${LOCAL_REV}" == "${REMOTE_REV}" ]]; then
    echo "[INFO] already up to date (${LOCAL_REV:0:12})"
    exit 0
fi

echo "[INFO] update available:"
echo "       local  ${LOCAL_REV:0:12}"
echo "       remote ${REMOTE_REV:0:12}"

# ---------------------------------------------------------------- apply update
git -C "${SRC_DIR}" reset --quiet --hard "origin/${BRANCH}"
# -fd removes untracked files; the exclusions keep a developer's local config
# and any yaml lying next to the checkout. Ignored directories (venv/) are
# preserved automatically.
git -C "${SRC_DIR}" clean --quiet -fd -e "*.yaml" -e "*.yml" -e "venv"

VENV_DIR="${SRC_DIR}/venv"
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[INFO] creating virtualenv"
    python3 -m venv "${VENV_DIR}"
fi

VENV_PY="${VENV_DIR}/bin/python"
VENV_BIN="${VENV_DIR}/bin/pg-router"

echo "[INFO] reinstalling dependencies"
"${VENV_PY}" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# Refresh this package only, then let pip reconcile dependencies: a single
# --force-reinstall would rebuild PyYAML and every wheel on every update.
"${VENV_PY}" -m pip install --quiet --no-cache-dir --no-deps --force-reinstall "${SRC_DIR}"
"${VENV_PY}" -m pip install --quiet --no-cache-dir "${SRC_DIR}"

# Verify the new code through the venv *before* touching the live symlink, so
# a failed build leaves the working CLI in place instead of a dead link.
if ! "${VENV_BIN}" --version >/dev/null 2>&1; then
    echo "[ERROR] new version failed verification; the existing CLI was left untouched" >&2
    exit 1
fi

ln -sf "${VENV_BIN}" "${BIN_LINK}"

echo "[INFO] updated to $( "${BIN_LINK}" --version 2>&1 ) (${REMOTE_REV:0:12})"

# ---------------------------------------------------------------- post-update check
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
