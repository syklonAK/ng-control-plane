#!/usr/bin/env bash
# pg-router — one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/syklonAK/ng-control-plane/main/install.sh | sudo bash
#
# Idempotent: safe to re-run. Installs into /opt/pg-router by default
# (override with PG_ROUTER_HOME). Never touches existing nginx config.
set -euo pipefail

REPO_URL="${PG_ROUTER_REPO:-https://github.com/syklonAK/ng-control-plane.git}"
BRANCH="${PG_ROUTER_BRANCH:-main}"
INSTALL_DIR="${PG_ROUTER_HOME:-/opt/pg-router}"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_LINK="/usr/local/bin/pg-router"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERROR] installer must run as root (prefix with sudo)" >&2
    exit 1
fi

echo "[INFO] pg-router installer"
echo "[INFO] target: ${INSTALL_DIR} (branch ${BRANCH})"

# ---------------------------------------------------------------- deps
if ! command -v git >/dev/null 2>&1; then
    echo "[INFO] installing git"
    if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq git
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q git
    elif command -v yum >/dev/null 2>&1; then yum install -y -q git
    elif command -v apk >/dev/null 2>&1; then apk add --quiet git
    else echo "[ERROR] no supported package manager to install git" >&2; exit 1
    fi
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "[INFO] installing python3"
    if command -v apt-get >/dev/null 2>&1; then apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3 python3-devel
    elif command -v yum >/dev/null 2>&1; then yum install -y -q python3
    elif command -v apk >/dev/null 2>&1; then apk add --quiet python3 py3-pip
    else echo "[ERROR] no supported package manager to install python3" >&2; exit 1
    fi
fi

PY_VER_MAJOR="$(python3 -c 'import sys; print(sys.version_info[0])')"
PY_VER_MINOR="$(python3 -c 'import sys; print(sys.version_info[1])')"
if [[ "${PY_VER_MAJOR}" -lt "${PY_MIN_MAJOR}" || \
      ( "${PY_VER_MAJOR}" -eq "${PY_MIN_MAJOR}" && "${PY_VER_MINOR}" -lt "${PY_MIN_MINOR}" ) ]]; then
    echo "[ERROR] python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+ required, found ${PY_VER_MAJOR}.${PY_VER_MINOR}" >&2
    exit 1
fi
echo "[INFO] python: $(python3 -V 2>&1)"

# Debian/Ubuntu ship python3 without the venv module unless python3-venv is
# installed. Create a throwaway venv to detect that before relying on it.
if ! python3 -m venv /tmp/pg-router-venvcheck >/dev/null 2>&1; then
    echo "[INFO] python venv module missing; installing python3-venv"
    rm -rf /tmp/pg-router-venvcheck
    if command -v apt-get >/dev/null 2>&1; then apt-get install -y -qq python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3-devel
    elif command -v apk >/dev/null 2>&1; then apk add --quiet py3-virtualenv
    else echo "[ERROR] cannot install venv support on this system" >&2; exit 1
    fi
fi
rm -rf /tmp/pg-router-venvcheck

# ---------------------------------------------------------------- source
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    echo "[INFO] updating existing installation"
    git -C "${INSTALL_DIR}" fetch --quiet --force origin "${BRANCH}"
    git -C "${INSTALL_DIR}" reset --quiet --hard "origin/${BRANCH}"
    git -C "${INSTALL_DIR}" clean --quiet -fd
else
    echo "[INFO] cloning fresh installation"
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    rm -rf "${INSTALL_DIR}"
    git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

# ---------------------------------------------------------------- venv + install
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[INFO] creating virtualenv"
    python3 -m venv "${VENV_DIR}"
fi

echo "[INFO] installing python dependencies"
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
"${VENV_DIR}/bin/python" -m pip install --quiet --no-cache-dir "${INSTALL_DIR}"

echo "[INFO] linking CLI to ${BIN_LINK}"
ln -sf "${VENV_DIR}/bin/pg-router" "${BIN_LINK}"

# ---------------------------------------------------------------- verify
if ! "${BIN_LINK}" --version >/dev/null 2>&1; then
    echo "[ERROR] installation verification failed: '${BIN_LINK} --version' returned non-zero" >&2
    exit 1
fi

echo "[INFO] $( "${BIN_LINK}" --version 2>&1 ) installed successfully"
echo
echo "Next steps:"
echo "  pg-router menu                       # interactive terminal menu"
echo "  pg-router init                       # create starter config"
echo "  pg-router -c /etc/pg-router/config.yaml validate"
echo "  pg-router -c /etc/pg-router/config.yaml generate --dry-run"
echo "  pg-router -c /etc/pg-router/config.yaml apply"
echo
echo "Or simply run 'pg-router' with no arguments on a terminal for the menu."
echo
echo "Update later with:"
echo "  pg-router update"
echo
echo "Remove with:"
echo "  pg-router uninstall [--yes] [--purge]"
