#!/usr/bin/env bash
# pg-router — one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/syklonAK/ng-control-plane/main/install.sh | sudo bash
#
# Idempotent: safe to re-run. Installs into /opt/pg-router by default
# (override with PG_ROUTER_HOME). Never touches existing nginx config.
#
# nginx itself and the dynamic modules this project needs (stream, ssl, ...) are
# installed when missing, via 'pg-router install' which knows the package names
# per distro and probes what is already loaded. Set PG_ROUTER_SKIP_NGINX=1 to
# leave nginx entirely to the operator.
#
# The system python3 version does not matter. A 3.10+ interpreter is, in order:
#   1. picked from interpreters already installed,
#   2. installed from the distro's own packages,
#   3. pulled from the deadsnakes PPA (Ubuntu family with an old default),
#   4. built from source.
#
# Tested on Ubuntu 18.04+ (including archived releases whose apt metadata has
# moved to old-releases.ubuntu.com), Debian 10+, RHEL/Rocky/Alma/Fedora, Alpine.

set -euo pipefail

REPO_URL="${PG_ROUTER_REPO:-https://github.com/syklonAK/ng-control-plane.git}"
BRANCH="${PG_ROUTER_BRANCH:-main}"
INSTALL_DIR="${PG_ROUTER_HOME:-/opt/pg-router}"
VENV_DIR="${INSTALL_DIR}/venv"
BIN_LINK="${PG_ROUTER_BIN_LINK:-/usr/local/bin/pg-router}"

PY_MIN_VERSION="3.10"
# Built from source only when no package can provide a modern interpreter.
PY_SRC_VERSION="3.11.9"
PY_SRC_PREFIX="${PG_ROUTER_PY_PREFIX:-/usr/local}"

# Filesystem locations the installer mutates. Every one has an override so the
# test suite can exercise the apt/EOL/PPA logic against a sandbox instead of a
# real system; defaults match Debian/Ubuntu conventions.
APT_SOURCES_LIST="${PG_ROUTER_APT_SOURCES:-/etc/apt/sources.list}"
APT_UBUNTU_SOURCES="${PG_ROUTER_UBUNTU_SOURCES:-/etc/apt/sources.list.d/ubuntu.sources}"
APT_DEADSNAKES_LIST="${PG_ROUTER_DEADSNAKES_LIST:-/etc/apt/sources.list.d/deadsnakes.list}"
APT_KEYRING_DIR="${PG_ROUTER_KEYRING_DIR:-/etc/apt/keyrings}"
APT_CONF_DROPIN="${PG_ROUTER_APT_CONF:-/etc/apt/apt.conf.d/99pg-router-no-check-valid}"
OPENSSL_PREFIX="${PG_ROUTER_OPENSSL_PREFIX:-/usr/local/openssl11}"

# Helpers write to stderr so their stdout stays clean for callers that capture
# a return value via command substitution; the installer output is unaffected.
log() { echo "[INFO] $*" >&2; }
err() { echo "[ERROR] $*" >&2; }

# ------------------------------------------------------------------ version
# Numeric comparison of 2- or 3-part dotted versions: version_ge 3.11.9 3.10.
version_ge() {
    local a="$1" b="$2"
    local IFS=.
    # shellcheck disable=SC2206
    local -a ap=($a) bp=($b)
    local i x y
    for i in 0 1 2; do
        x="${ap[i]:-0}"
        y="${bp[i]:-0}"
        [[ "$x" =~ ^[0-9]+$ ]] || x=0
        [[ "$y" =~ ^[0-9]+$ ]] || y=0
        (( x > y )) && return 0
        (( x < y )) && return 1
    done
    return 0
}

# ------------------------------------------------------------------ distro
# Parse /etc/os-release without polluting the environment. PG_ROUTER_OS_RELEASE
# exists only so the test suite can feed a fake file.
DISTRO_ID="unknown"
DISTRO_VERSION_ID=""
DISTRO_CODENAME=""

detect_distro() {
    DISTRO_ID="unknown"
    DISTRO_VERSION_ID=""
    DISTRO_CODENAME=""
    local os_release="${PG_ROUTER_OS_RELEASE:-/etc/os-release}"
    [[ -f "$os_release" ]] || return 0

    local key val
    while IFS='=' read -r key val; do
        val="${val%\"}"
        val="${val#\"}"
        case "$key" in
            ID) DISTRO_ID="$val" ;;
            VERSION_ID) DISTRO_VERSION_ID="$val" ;;
            VERSION_CODENAME) DISTRO_CODENAME="$val" ;;
        esac
    done < "$os_release"

    # Releases do not always advertise a codename, and Linux Mint advertises
    # its own. Map to the Ubuntu/Debian suite name the archives use.
    case "${DISTRO_ID}:${DISTRO_VERSION_ID}" in
        ubuntu:18.04|linuxmint:19) DISTRO_CODENAME="bionic" ;;
        ubuntu:20.04|linuxmint:20) DISTRO_CODENAME="focal" ;;
        ubuntu:22.04|linuxmint:21) DISTRO_CODENAME="jammy" ;;
        ubuntu:22.10) DISTRO_CODENAME="kinetic" ;;
        ubuntu:23.04) DISTRO_CODENAME="lunar" ;;
        ubuntu:23.10) DISTRO_CODENAME="mantic" ;;
        ubuntu:24.04|linuxmint:22) DISTRO_CODENAME="noble" ;;
        ubuntu:24.10) DISTRO_CODENAME="oracular" ;;
        debian:9) DISTRO_CODENAME="stretch" ;;
        debian:10) DISTRO_CODENAME="buster" ;;
        debian:11) DISTRO_CODENAME="bullseye" ;;
        debian:12) DISTRO_CODENAME="bookworm" ;;
        debian:13) DISTRO_CODENAME="trixie" ;;
    esac
}

# The deadsnakes PPA hosts modern interpreters for Ubuntu releases whose own
# archives stop at an older python (18.04 ships 3.6, 20.04 ships 3.8).
use_deadsnakes() {
    case "$DISTRO_ID" in
        ubuntu|pop)
            # From 24.04 on the system python is already 3.12.
            [[ -n "$DISTRO_VERSION_ID" ]] && ! version_ge "$DISTRO_VERSION_ID" 24.04
            ;;
        linuxmint)
            # Mint 19-22 are Ubuntu-based; LMDE releases are Debian-based.
            case "$DISTRO_VERSION_ID" in
                19|20|21|22) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

# ------------------------------------------------------------------ download
download() {
    # download <url> <output file>
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 --max-time 600 "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=600 "$1" -O "$2"
    else
        err "neither curl nor wget is available to download $1"
        return 1
    fi
}

# ------------------------------------------------------------------ python
python_meets_minimum() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

# Prefer an explicit version over the bare 'python3' name: distros pin the bare
# name to an old release while a newer interpreter is installed alongside it.
pick_existing_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        python_meets_minimum "$candidate" || continue
        echo "$candidate"
        return 0
    done
    return 1
}

apt_install_modern_python() {
    local pk
    for pk in python3.13 python3.12 python3.11 python3.10; do
        # Verify afterwards: an apt transaction can "succeed" while leaving the
        # requested package unconfigured, which would break the venv step.
        if apt-get install -y -qq "${pk}" "${pk}-venv" >/dev/null 2>&1 && python_meets_minimum "$pk"; then
            echo "$pk"
            return 0
        fi
    done
    return 1
}

# Archived releases (Ubuntu 18.04/20.04, Debian 10/11) keep their packages on
# old-releases.ubuntu.com / archive.debian.org; the stock sources.list still
# points at the live mirrors, so every apt operation fails until it is fixed.
apt_update_retried() {
    if apt-get update -qq >/dev/null 2>&1; then return 0; fi
    log "apt update failed; re-pointing to the archived-release mirrors"
    apt_remap_eol_repos || return 1
    apt-get update -qq >/dev/null 2>&1
}

apt_remap_eol_repos() {
    local changed=0 file
    case "$DISTRO_ID" in
        ubuntu)
            for file in "$APT_UBUNTU_SOURCES" "$APT_SOURCES_LIST"; do
                [[ -f "$file" ]] || continue
                grep -Eq 'archive\.ubuntu\.com|security\.ubuntu\.com|ports\.ubuntu\.com' "$file" || continue
                cp -a "$file" "${file}.pg-router.bak"
                sed -i -E 's#(https?://)(archive|security|ports)\.ubuntu\.com#\1old-releases.ubuntu.com#g' "$file"
                changed=1
            done
            ;;
        debian)
            if [[ -f "$APT_SOURCES_LIST" ]] \
                && grep -Eq 'deb\.debian\.org|security\.debian\.org|ftp\.[a-z0-9.-]+\.debian\.org' "$APT_SOURCES_LIST"; then
                cp -a "$APT_SOURCES_LIST" "${APT_SOURCES_LIST}.pg-router.bak"
                sed -i -E 's#https?://(deb\.debian\.org|security\.debian\.org|ftp\.[a-z0-9.-]+\.debian\.org)#http://archive.debian.org#g' "$APT_SOURCES_LIST"
                # Archived Release files are signed with long-expired keys.
                echo 'Acquire::Check-Valid-Until "false";' > "$APT_CONF_DROPIN"
                changed=1
            fi
            ;;
        *) return 1 ;;
    esac
    if [[ "$changed" -eq 1 ]]; then
        log "apt sources re-pointed (backup saved with a .pg-router.bak suffix)"
        return 0
    fi
    return 1
}

enable_deadsnakes() {
    [[ -n "$DISTRO_CODENAME" ]] || return 1
    command -v apt-get >/dev/null 2>&1 || return 1
    log "enabling the deadsnakes PPA (suite ${DISTRO_CODENAME})"

    install -d -m 0755 "$APT_KEYRING_DIR" 2>/dev/null || true
    if ! command -v add-apt-repository >/dev/null 2>&1; then
        apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
    fi
    if command -v add-apt-repository >/dev/null 2>&1; then
        if add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1; then
            return 0
        fi
    fi

    # Manual entry: works when add-apt-repository is missing or the apt
    # keyserver transport is unavailable on a locked-down host.
    local keyring="${APT_KEYRING_DIR}/deadsnakes.gpg"
    local key="F23C5A6CF475977595C89F51BA6932366A755776"
    if [[ ! -s "$keyring" ]]; then
        rm -f "$keyring"
        download "https://keyserver.ubuntu.com/pks/lookup?op=get&options=nm&search=0x${key}" "${keyring}.asc" \
            && gpg --dearmor --yes -o "$keyring" "${keyring}.asc" >/dev/null 2>&1 \
            && rm -f "${keyring}.asc" \
            || rm -f "${keyring}.asc" || true
    fi
    local signed_by=""
    if [[ ! -s "$keyring" ]]; then
        # Legacy path for hosts without a usable gpg: apt-key is deprecated but
        # still present on every release that needs this fallback.
        apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys "$key" >/dev/null 2>&1 || true
    else
        signed_by="[signed-by=${keyring}] "
    fi
    echo "deb ${signed_by}http://ppa.launchpad.net/deadsnakes/ppa/ubuntu ${DISTRO_CODENAME} main" \
        > "$APT_DEADSNAKES_LIST"
    apt-get update -qq >/dev/null 2>&1 || return 1
}

ensure_modern_openssl() {
    # python 3.10+ requires openssl 1.1.1+ for a working ssl module; without it
    # pip cannot reach PyPI over https. Old enterprise releases (CentOS 7) ship
    # 1.0.2, so build a private copy and point python's configure at it.
    if command -v openssl >/dev/null 2>&1; then
        local version
        version="$(openssl version 2>/dev/null | awk 'NR==1 {print $2}')"
        if version_ge "$version" "1.1.1"; then
            return 0
        fi
    fi
    local prefix="$OPENSSL_PREFIX"
    [[ -x "${prefix}/bin/openssl" ]] && return 0
    log "openssl too old; building openssl 1.1.1w into ${prefix}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y -qq build-essential zlib1g-dev perl >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q gcc make perl zlib-devel >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q gcc make perl zlib-devel >/dev/null 2>&1 || true
    elif command -v apk >/dev/null 2>&1; then
        apk add --quiet build-base perl zlib-dev >/dev/null 2>&1 || true
    fi
    local tmp tarball
    tmp="$(mktemp -d)"
    tarball="${tmp}/openssl.tar.gz"
    download "https://www.openssl.org/source/openssl-1.1.1w.tar.gz" "$tarball" || { rm -rf "$tmp"; return 1; }
    tar -C "$tmp" -xzf "$tarball" || { rm -rf "$tmp"; return 1; }
    ( cd "${tmp}/openssl-1.1.1w" \
        && ./config --prefix="$prefix" --openssldir="$prefix" shared zlib >/dev/null 2>&1 \
        && make -j"$(nproc)" >/dev/null 2>&1 \
        && make install_sw >/dev/null 2>&1 ) || { rm -rf "$tmp"; return 1; }
    rm -rf "$tmp"
}

build_python_from_source() {
    local ver="${PY_SRC_VERSION}"
    log "building python ${ver} from source into ${PY_SRC_PREFIX} (this takes a few minutes)"

    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y -qq build-essential libssl-dev zlib1g-dev libbz2-dev \
            libreadline-dev libsqlite3-dev libffi-dev libncursesw5-dev \
            libgdbm-dev liblzma-dev uuid-dev tk-dev perl >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q gcc make diffutils openssl-devel zlib-devel bzip2-devel \
            readline-devel sqlite-devel libffi-devel ncurses-devel gdbm-devel \
            xz-devel tk-devel perl >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q gcc make diffutils openssl-devel zlib-devel bzip2-devel \
            readline-devel sqlite-devel libffi-devel ncurses-devel gdbm-devel \
            xz-devel tk-devel perl >/dev/null 2>&1 || true
    elif command -v apk >/dev/null 2>&1; then
        apk add --quiet build-base openssl-dev zlib-dev bzip2-dev readline-dev \
            sqlite-dev libffi-dev ncurses-dev gdbm-dev xz-dev tk-dev perl >/dev/null 2>&1 || true
    fi

    local openssl_flag=()
    if ! ensure_modern_openssl 2>/dev/null; then
        err "no openssl 1.1.1+ available; the built python may lack ssl support"
    elif [[ -d "$OPENSSL_PREFIX" ]]; then
        openssl_flag=(--with-openssl="$OPENSSL_PREFIX")
    fi
    local tmp tarball
    tmp="$(mktemp -d)"
    tarball="${tmp}/Python.tgz"
    if ! download "https://www.python.org/ftp/python/${ver}/Python-${ver}.tgz" "$tarball"; then
        err "could not download python ${ver}"
        rm -rf "$tmp"
        return 1
    fi
    tar -C "$tmp" -xzf "$tarball" || { rm -rf "$tmp"; return 1; }
    log "compiling (configure + make, be patient)"
    local -a configure=(./configure --prefix="${PY_SRC_PREFIX}")
    if [[ ${#openssl_flag[@]} -gt 0 ]]; then
        configure+=("${openssl_flag[@]}")
    fi
    if ! ( cd "${tmp}/Python-${ver}" \
        && "${configure[@]}" >/dev/null 2>&1 \
        && make -j"$(nproc)" >/dev/null 2>&1 \
        && make install >/dev/null 2>&1 ); then
        err "building python ${ver} failed"
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"

    local out="${PY_SRC_PREFIX}/bin/python${ver%.*}"
    [[ -x "$out" ]] || out="${PY_SRC_PREFIX}/bin/python3"
    python_meets_minimum "$out" || { err "built python is older than ${PY_MIN_VERSION}"; return 1; }
    echo "$out"
}

ensure_python_with_apt() {
    command -v apt-get >/dev/null 2>&1 || return 1
    apt_update_retried || true

    local out
    out="$(apt_install_modern_python)" && { echo "$out"; return 0; } || true

    if use_deadsnakes && enable_deadsnakes; then
        out="$(apt_install_modern_python)" && { echo "$out"; return 0; } || true
    fi

    # Last resort on any apt host: compile. Reaches modern python on distros
    # whose archives simply do not carry one (Debian 10/11).
    out="$(build_python_from_source)" && { echo "$out"; return 0; } || true
    return 1
}

ensure_python_with_dnf() {
    local out pk
    if command -v dnf >/dev/null 2>&1; then
        for pk in python3.13 python3.12 python3.11; do
            if dnf install -y -q "$pk" >/dev/null 2>&1 && python_meets_minimum "$pk"; then
                echo "$pk"
                return 0
            fi
        done
    elif command -v yum >/dev/null 2>&1; then
        for pk in python3.11 python3.12; do
            if yum install -y -q "$pk" >/dev/null 2>&1 && python_meets_minimum "$pk"; then
                echo "$pk"
                return 0
            fi
        done
    else
        return 1
    fi
    out="$(build_python_from_source)" && { echo "$out"; return 0; } || true
    return 1
}

ensure_python_with_apk() {
    command -v apk >/dev/null 2>&1 || return 1
    apk add --quiet python3 py3-pip >/dev/null 2>&1 || true
    python_meets_minimum python3 && { echo "python3"; return 0; } || return 1
}

ensure_python_generic() {
    # Unknown distro: try every provisioner we have.
    local out
    out="$(ensure_python_with_apt)" && { echo "$out"; return 0; } || true
    out="$(ensure_python_with_dnf)" && { echo "$out"; return 0; } || true
    out="$(ensure_python_with_apk)" && { echo "$out"; return 0; } || true
    return 1
}

PY=""

ensure_python() {
    detect_distro
    local found
    if found="$(pick_existing_python)"; then
        PY="$found"
        log "python: $("${PY}" -V 2>&1) (already installed)"
        return 0
    fi

    log "no python ${PY_MIN_VERSION}+ found on ${DISTRO_ID} ${DISTRO_VERSION_ID:-unknown}; provisioning one"
    case "$DISTRO_ID" in
        ubuntu|linuxmint|pop|debian)
            found="$(ensure_python_with_apt)" || true
            ;;
        rhel|centos|rocky|almalinux|fedora|amzn|ol)
            found="$(ensure_python_with_dnf)" || true
            ;;
        alpine)
            found="$(ensure_python_with_apk)" || found="$(ensure_python_generic)" || true
            ;;
        *)
            found="$(ensure_python_generic)" || true
            ;;
    esac

    if [[ -z "$found" ]] || ! command -v "$found" >/dev/null 2>&1 || ! python_meets_minimum "$found"; then
        err "python ${PY_MIN_VERSION}+ could not be provisioned on this system"
        err "install it manually (e.g. 'apt-get install python3.12 python3.12-venv') and re-run"
        return 1
    fi
    PY="$found"
    log "python: $("${PY}" -V 2>&1) (provisioned)"
}

ensure_venv_module() {
    # Source-built pythons ship venv; distro pythons often split it into a
    # separate package (Debian/Ubuntu strip ensurepip unless pythonX.Y-venv is
    # installed), so probe with a throwaway venv before relying on it.
    local probe
    probe="$(mktemp -d)/probe-venv"
    if "${PY}" -m venv "$probe" >/dev/null 2>&1; then
        rm -rf "$probe"
        return 0
    fi
    rm -rf "$probe"

    log "the venv module is missing; installing venv support"
    local base="${PY##*/}"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y -qq "${base}-venv" python3-pip >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3-devel >/dev/null 2>&1 || true
    elif command -v apk >/dev/null 2>&1; then
        apk add --quiet py3-virtualenv >/dev/null 2>&1 || true
    else
        err "cannot install venv support on this system"
        return 1
    fi

    probe="$(mktemp -d)/probe-venv"
    if ! "${PY}" -m venv "$probe" >/dev/null 2>&1; then
        rm -rf "$probe"
        err "${PY} -m venv still fails after installing venv support"
        err "on Debian/Ubuntu try: apt-get install -y ${base}-venv"
        return 1
    fi
    rm -rf "$probe"
}

ensure_git() {
    command -v git >/dev/null 2>&1 && return 0
    log "installing git"
    if command -v apt-get >/dev/null 2>&1; then
        apt_update_retried || true
        apt-get install -y -qq git
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q git
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q git
    elif command -v apk >/dev/null 2>&1; then
        apk add --quiet git
    else
        err "no supported package manager to install git"
        return 1
    fi
}

install_sources() {
    # A pinned ref (tag like "v1.2.0" or a commit sha) installs exactly that
    # revision instead of the live branch tip. This matters for repeatability:
    # a host provisioned today and one provisioned in six months must be able
    # to run identical code.
    local ref="${PG_ROUTER_REF:-}"

    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        log "updating existing installation"
        git -C "${INSTALL_DIR}" fetch --quiet --force origin "${BRANCH}"
        if [[ -n "${ref}" ]]; then
            if ! git -C "${INSTALL_DIR}" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null; then
                err "pinned ref '${ref}' could not be resolved; refusing to update blindly"
                return 1
            fi
            git -C "${INSTALL_DIR}" reset --quiet --hard "${ref}"
        else
            git -C "${INSTALL_DIR}" reset --quiet --hard "origin/${BRANCH}"
        fi
        git -C "${INSTALL_DIR}" clean --quiet -fd -e "*.yaml" -e "*.yml" -e "venv"
    else
        log "cloning fresh installation"
        mkdir -p "$(dirname "${INSTALL_DIR}")"
        rm -rf "${INSTALL_DIR}"
        if [[ -n "${ref}" ]]; then
            # Clone the history needed to resolve the pinned ref, then check it
            # out: --depth 1 --branch would only accept a branch or tag name.
            git clone --quiet "${REPO_URL}" "${INSTALL_DIR}"
            git -C "${INSTALL_DIR}" fetch --quiet --force origin "${BRANCH}"
            if ! git -C "${INSTALL_DIR}" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null; then
                err "pinned ref '${ref}' could not be resolved; refusing to install blindly"
                return 1
            fi
            git -C "${INSTALL_DIR}" reset --quiet --hard "${ref}"
        else
            git clone --quiet --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
        fi
    fi

    # Verify the working tree is exactly what was asked for before any package
    # is installed from it.
    local actual expected
    actual="$(git -C "${INSTALL_DIR}" rev-parse HEAD)"
    if [[ -n "${ref}" ]]; then
        expected="$(git -C "${INSTALL_DIR}" rev-parse "${ref}^{commit}")"
    else
        expected="$(git -C "${INSTALL_DIR}" rev-parse "origin/${BRANCH}")"
    fi
    if [[ "${actual}" != "${expected}" ]]; then
        err "working tree is at ${actual:0:12}, expected ${expected:0:12}; aborting before install"
        return 1
    fi
    log "sources at ${actual:0:12}"
}

install_package() {
    if [[ ! -d "${VENV_DIR}" ]]; then
        log "creating virtualenv with ${PY}"
        "${PY}" -m venv "${VENV_DIR}"
    fi

    log "installing python dependencies"
    "${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    "${VENV_DIR}/bin/python" -m pip install --quiet --no-cache-dir "${INSTALL_DIR}"

    log "linking CLI to ${BIN_LINK}"
    ln -sf "${VENV_DIR}/bin/pg-router" "${BIN_LINK}"

    if ! "${BIN_LINK}" --version >/dev/null 2>&1; then
        err "installation verification failed: '${BIN_LINK} --version' returned non-zero"
        return 1
    fi
    log "$("${BIN_LINK}" --version 2>&1) installed successfully"
}

ensure_nginx() {
    # Delegated to the CLI: it detects what is already loaded with 'nginx -t',
    # knows the module package names per distro family, and installs only what
    # is missing — so a fully provisioned host is a no-op and a partial one gets
    # just the gap filled. Safe to re-run any number of times.
    if [[ "${PG_ROUTER_SKIP_NGINX:-0}" == "1" ]]; then
        log "PG_ROUTER_SKIP_NGINX=1; leaving nginx to the operator"
        return 0
    fi

    log "checking nginx and required modules"
    if "${BIN_LINK}" install; then
        log "nginx and required modules are ready"
        return 0
    fi

    # Archived releases keep their packages on retired mirrors; the CLI's own
    # apt update then fails. Re-point the sources and give it one more chance
    # before giving up.
    if command -v apt-get >/dev/null 2>&1 && apt_update_retried; then
        if "${BIN_LINK}" install; then
            log "nginx and required modules are ready"
            return 0
        fi
    fi

    # Not fatal: the CLI is installed and usable, and nginx may be supplied by
    # another host, a build outside PATH, or a later provisioning step.
    err "nginx or its modules could not be installed automatically"
    err "re-run 'sudo ${BIN_LINK} install' once nginx is available,"
    err "or set PG_ROUTER_SKIP_NGINX=1 if nginx is managed elsewhere"
}

main() {
    if [[ "$(id -u)" -ne 0 ]]; then
        err "installer must run as root (prefix with sudo)"
        exit 1
    fi

    log "pg-router installer"
    log "target: ${INSTALL_DIR} (branch ${BRANCH})"

    # detect_distro first: installing git on an archived release needs the EOL
    # remap, which is decided by the distro id.
    detect_distro
    ensure_git || exit 1
    ensure_python || exit 1
    ensure_venv_module || exit 1
    install_sources || exit 1
    install_package || exit 1
    ensure_nginx

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
}

# Sourced by the test suite to exercise the helpers above. Only run main when
# the script is executed directly. The naive "${BASH_SOURCE[0]}" == "$0" test
# cannot be used here: this installer is documented as
# `curl -fsSL .../install.sh | sudo bash`, and when bash reads the script from
# stdin BASH_SOURCE is an empty array, so `set -u` aborts the whole install
# with "BASH_SOURCE[0]: unbound variable" before main is ever reached.
if ! (return 0 2>/dev/null); then
    main "$@"
fi
