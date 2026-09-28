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
git -C "${SRC_DIR}" clean --quiet -fd

VENV_DIR="${SRC_DIR}/venv"
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[INFO] creating virtualenv"
    python3 -m venv "${VENV_DIR}"
fi

echo "[INFO] reinstalling dependencies"
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# --force-reinstall guarantees the entry point reflects the new source tree.
"${VENV_DIR}/bin/python" -m pip install --quiet --no-cache-dir --force-reinstall "${SRC_DIR}"

ln -sf "${VENV_DIR}/bin/pg-router" "${BIN_LINK}"

if ! "${BIN_LINK}" --version >/dev/null 2>&1; then
    echo "[ERROR] post-update verification failed" >&2
    exit 1
fi

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
