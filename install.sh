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

# ---------------------------------------------------------------- python
# Pick any interpreter that is new enough. python3 might be old (Debian/Ubuntu
# ship an older default) while python3.10+ is already installed alongside it,
# so prefer an explicit version over the bare name.
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
    echo "[INFO] no suitable python found; trying to install one"

    # Try versioned packages directly, then (on Ubuntu) the deadsnakes PPA,
    # which carries modern interpreters for releases whose default 'python3'
    # is too old (e.g. Ubuntu 20.04 ships 3.8).
    try_apt_python() {
        for pk in python3.13 python3.12 python3.11 python3.10; do
            if apt-get install -y -qq "${pk}" "${pk}-venv" >/dev/null 2>&1; then
                PY="${pk}"
                return 0
            fi
        done
        return 1
    }

    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq
        if ! try_apt_python; then
            # shellcheck disable=SC1091
            if [[ -f /etc/os-release ]] && . /etc/os-release && [[ "${ID:-}" == "ubuntu" ]]; then
                echo "[INFO] enabling deadsnakes PPA for a modern python"
                apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
                add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || true
                apt-get update -qq
                try_apt_python || true
            fi
        fi
    elif command -v dnf >/dev/null 2>&1; then
        for pk in python3.13 python3.12 python3.11; do
            if dnf install -y -q "${pk}" >/dev/null 2>&1; then
                PY="${pk}"
                break
            fi
        done
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q python3 || true
        PY=python3
    elif command -v apk >/dev/null 2>&1; then
        apk add --quiet python3 py3-pip || true
        PY=python3
    fi
fi

if [[ -z "${PY}" ]] || ! command -v "${PY}" >/dev/null 2>&1; then
    echo "[ERROR] python 3.10+ required and could not be installed" >&2
    echo "[ERROR] install it manually (e.g. apt-get install python3.12 python3.12-venv) and re-run" >&2
    exit 1
fi

if ! "${PY}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "[ERROR] python 3.10+ required, found $( "${PY}" -V 2>&1 )" >&2
    exit 1
fi
echo "[INFO] python: $( "${PY}" -V 2>&1 )"

# Debian/Ubuntu ship python without the venv module unless pythonX.Y-venv is
# installed. Create a throwaway venv to detect that before relying on it.
if ! "${PY}" -m venv /tmp/pg-router-venvcheck >/dev/null 2>&1; then
    echo "[INFO] python venv module missing; installing venv support"
    rm -rf /tmp/pg-router-venvcheck
    if command -v apt-get >/dev/null 2>&1; then apt-get install -y -qq "${PY}-venv" python3-pip
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3-devel
    elif command -v apk >/dev/null 2>&1; then apk add --quiet py3-virtualenv
    else echo "[ERROR] cannot install venv support on this system" >&2; exit 1
    fi
    # Re-test after installing: python3-venv can still be unavailable when the
    # ensurepip wheel package is missing (Debian strips it sometimes).
    if ! "${PY}" -m venv /tmp/pg-router-venvcheck >/dev/null 2>&1; then
        rm -rf /tmp/pg-router-venvcheck
        echo "[ERROR] ${PY} -m venv still fails after installing venv support" >&2
        echo "[ERROR] on Debian try: apt-get install -y ${PY}-venv" >&2
        exit 1
    fi
fi
rm -rf /tmp/pg-router-venvcheck

# ---------------------------------------------------------------- source
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    echo "[INFO] updating existing installation"
    git -C "${INSTALL_DIR}" fetch --quiet --force origin "${BRANCH}"
    git -C "${INSTALL_DIR}" reset --quiet --hard "origin/${BRANCH}"
    git -C "${INSTALL_DIR}" clean --quiet -fd -e "*.yaml" -e "*.yml" -e "venv"
else
    echo "[INFO] cloning fresh installation"
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    rm -rf "${INSTALL_DIR}"
    git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

# ---------------------------------------------------------------- venv + install
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[INFO] creating virtualenv with ${PY}"
    "${PY}" -m venv "${VENV_DIR}"
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
