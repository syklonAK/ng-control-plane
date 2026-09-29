#!/usr/bin/env bash
# pg-router — uninstaller
#
# Removes the CLI, the virtualenv and (optionally) the generated nginx
# fragments. Safe by default: it never deletes the configuration file or
# nginx itself, and it asks before touching anything.
#
# Usage:
#   pg-router uninstall                       # interactive
#   pg-router uninstall --yes                 # no confirmation prompt
#   ./uninstall.sh --yes --purge              # also remove config + fragments
set -euo pipefail

INSTALL_DIR="${PG_ROUTER_HOME:-/opt/pg-router}"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_LINK="/usr/local/bin/pg-router"
CONFIG="${PG_ROUTER_CONFIG:-/etc/pg-router/config.yaml}"
MANAGED_DIR="${PG_ROUTER_DATA_DIR:-/etc/nginx/pg-router}"

PURGE=0
ASSUME_YES=0

for arg in "$@"; do
    case "${arg}" in
        --purge)   PURGE=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        -h|--help)
            sed -n '2,10p' "$0"
            exit 0
            ;;
        *)
            echo "[ERROR] unknown argument: ${arg}" >&2
            exit 2
            ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERROR] uninstaller must run as root (prefix with sudo)" >&2
    exit 1
fi

confirm() {
    if [[ "${ASSUME_YES}" -eq 1 ]]; then return 0; fi
    local reply
    read -r -p "$1 (y/N) " reply
    [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

echo "[INFO] pg-router uninstaller"

# ---------------------------------------------------------------- CLI symlink
if [[ -L "${BIN_LINK}" ]]; then
    if confirm "Remove CLI symlink ${BIN_LINK}?"; then
        rm -f "${BIN_LINK}"
        echo "[INFO] removed ${BIN_LINK}"
    else
        echo "[INFO] keeping CLI symlink; the shell command stays available"
    fi
else
    echo "[INFO] no CLI symlink at ${BIN_LINK}"
fi

# ---------------------------------------------------------------- installation
if [[ -d "${INSTALL_DIR}" ]]; then
    if confirm "Remove installation directory ${INSTALL_DIR} (source + virtualenv)?"; then
        rm -rf "${INSTALL_DIR}"
        echo "[INFO] removed ${INSTALL_DIR}"
    else
        echo "[INFO] keeping ${INSTALL_DIR}"
    fi
else
    echo "[INFO] no installation directory at ${INSTALL_DIR}"
fi

# ---------------------------------------------------------------- purge
if [[ "${PURGE}" -eq 1 ]]; then
    if [[ -d "${MANAGED_DIR}" ]] && confirm "Purge managed nginx fragments in ${MANAGED_DIR}?"; then
        rm -rf "${MANAGED_DIR}"
        echo "[INFO] removed ${MANAGED_DIR}"
    fi
    if [[ -f "${CONFIG}" ]] && confirm "Purge configuration ${CONFIG}?"; then
        rm -f "${CONFIG}"
        echo "[INFO] removed ${CONFIG}"
    fi
    echo "[WARNING] the include lines in /etc/nginx/nginx.conf were left in place;"
    echo "[WARNING] remove them manually if nginx is no longer managed by pg-router."
else
    echo "[INFO] configuration and nginx fragments kept (pass --purge to remove them)"
fi

# ---------------------------------------------------------------- verify
if command -v pg-router >/dev/null 2>&1; then
    echo "[WARNING] 'pg-router' is still on PATH (another installation or a stale symlink)" >&2
else
    echo "[INFO] pg-router uninstalled"
fi
