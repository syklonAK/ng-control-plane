#!/usr/bin/env bash
# End-to-end installer scenarios against a sandboxed "system".
#
# install.sh is sourced (its main() guard keeps it inert), the network, the
# package managers and the interpreters are stubbed, and main() runs with a
# controlled PATH so only the fake tools exist. This exercises the real
# provisioning decisions: EOL archive remapping, the deadsnakes PPA (both the
# add-apt-repository and the manual key paths), the source-build fallback and
# the interpreter that is already installed.
#
# usage: installer_scenarios.sh <path/to/install.sh> <workdir> <scenario>...
set -uo pipefail

install_sh="$1"
WORK="$2"
shift 2
# Canonicalise: a "C:/..." style path contains a colon, which would split PATH
# apart further down and silently drop the stub directory from the lookup.
if WP="$(cd "$WORK" 2>/dev/null && pwd)"; then
    WORK="$WP"
fi
STUBS="$WORK/stubs"
ETC="$WORK/etc"
failures=0
EXTRA_OVERRIDES=""

pass() { echo "ok   $1"; }
fail() { echo "FAIL $1: ${2:-}"; failures=$((failures + 1)); }

setup_common() {
    rm -rf "$WORK"; mkdir -p "$STUBS" "$ETC"
    printf '#!/usr/bin/env bash\necho 0\n' > "$STUBS/id"
    printf '#!/usr/bin/env bash\nexit 0\n' > "$STUBS/git"
    # shared interpreter-stub factory, called by the fake package managers
    cat > "$STUBS/mkpy" <<'EOF'
#!/usr/bin/env bash
self="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
    /*) bin="$1" ;;
    *) bin="$self/$1" ;;
esac
mkdir -p "$(dirname "$bin")"
printf '#!/usr/bin/env bash\nif [[ "${1:-}" == "-c" ]]; then exit 0; fi\nif [[ "${1:-}" == "-V" ]]; then echo "Python (stub)"; fi\nexit 0\n' > "$bin"
chmod +x "$bin"
EOF
    chmod +x "$STUBS"/*
}

# A system python3 that is too old, so it must never be picked.
write_old_python3() {
    cat > "$STUBS/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then exit 1; fi
echo "Python 3.6.9"; exit 0
EOF
    chmod +x "$STUBS/python3"
}

write_os_release() {
    cat > "$ETC/os-release" <<EOF
ID=$1
VERSION_ID="$2"
${3:+VERSION_CODENAME=$3}
EOF
}

# sources.list that only resolves after the EOL remap.
write_sources() {
    # write_sources <host> <path-and-suite>, e.g. archive.ubuntu.com "ubuntu bionic"
    printf 'deb http://%s/%s main\n' "$1" "$2" > "$ETC/sources.list"
}

# apt-get whose 'update' fails until the sources.list points at an archive
# mirror, and which only knows modern python once the deadsnakes list exists.
write_aptget() {
    local deadsnakes_list="$1"
    cat > "$STUBS/apt-get" <<EOF
#!/usr/bin/env bash
case "\$1" in
    update)
        if grep -q 'old-releases.ubuntu.com\|archive.debian.org' "$ETC/sources.list" 2>/dev/null; then
            exit 0
        fi
        echo "E: Failed to fetch" >&2; exit 100 ;;
    install)
        local ok=1
        for arg in "\$@"; do
            case "\$arg" in
                python3.1[0-3])
                    if [[ -f "$deadsnakes_list" ]]; then
                        mkpy "\$arg"; ok=0
                    else
                        echo "E: Unable to locate package \$arg" >&2
                    fi ;;
            esac
        done
        [[ "\$ok" -eq 0 ]] ;;
esac
exit 0
EOF
    chmod +x "$STUBS/apt-get"
}

run_install() {
    env PATH="$STUBS:/usr/bin:/bin" \
        PG_ROUTER_OS_RELEASE="$ETC/os-release" \
        PG_ROUTER_APT_SOURCES="$ETC/sources.list" \
        PG_ROUTER_DEADSNAKES_LIST="$ETC/deadsnakes.list" \
        PG_ROUTER_KEYRING_DIR="$ETC/keyrings" \
        PG_ROUTER_APT_CONF="$ETC/apt.conf" \
        PG_ROUTER_OPENSSL_PREFIX="$ETC/openssl11" \
        PG_ROUTER_PY_PREFIX="$WORK/local" \
        bash -c "
            source '$install_sh'
            set +euo pipefail
            install_sources() { :; }
            install_package() { :; }
            ensure_nginx() { :; }
            $EXTRA_OVERRIDES
            main
            echo \"PY=\$PY\"
        " 2>&1
}

# nginx provisioning is exercised on its own: the interpreter, the source
# clone and the CLI are stubbed so only ensure_nginx's decisions run.
# usage: run_install_nginx <bin-link stub> [PG_ROUTER_SKIP_NGINX]
run_install_nginx() {
    env PATH="$STUBS:/usr/bin:/bin" \
        PG_ROUTER_OS_RELEASE="$ETC/os-release" \
        PG_ROUTER_APT_SOURCES="$ETC/sources.list" \
        PG_ROUTER_DEADSNAKES_LIST="$ETC/deadsnakes.list" \
        PG_ROUTER_KEYRING_DIR="$ETC/keyrings" \
        PG_ROUTER_APT_CONF="$ETC/apt.conf" \
        PG_ROUTER_BIN_LINK="$1" \
        PG_ROUTER_SKIP_NGINX="${2:-0}" \
        bash -c "
            source '$install_sh'
            set +euo pipefail
            ensure_python() { PY=python3; }
            ensure_venv_module() { :; }
            install_sources() { :; }
            install_package() { :; }
            main
        " 2>&1
}

# ---------------------------------------------------------------- scenarios

# Ubuntu 18.04: default python is 3.6, apt is dead until the archive remap,
# and the PPA is registered with the distro's add-apt-repository tool.
scenario_bionic_ppa() {
    setup_common
    write_old_python3
    write_os_release ubuntu 18.04 bionic
    write_sources archive.ubuntu.com "ubuntu bionic"
    cat > "$STUBS/add-apt-repository" <<EOF
#!/usr/bin/env bash
echo "deb http://ppa.launchpad.net/deadsnakes/ppa/ubuntu bionic main" > "$ETC/deadsnakes.list"
exit 0
EOF
    chmod +x "$STUBS/add-apt-repository"
    write_aptget "$ETC/deadsnakes.list"

    local out
    out="$(run_install)"
    if [[ "$out" == *"PY=python3.1"* ]]; then pass "bionic provisions a modern python via add-apt-repository"; else fail "bionic+ppa" "$out"; fi
    if grep -q "old-releases.ubuntu.com" "$ETC/sources.list"; then pass "bionic sources re-pointed to old-releases"; else fail "bionic eol remap" "sources.list unchanged"; fi
    if [[ -f "$ETC/sources.list.pg-router.bak" ]]; then pass "original sources.list backed up"; else fail "bionic eol remap" "no backup"; fi
}

# Same host, but add-apt-repository is unavailable, so the installer must
# write the PPA entry and its key by hand.
scenario_bionic_manual_key() {
    setup_common
    write_old_python3
    write_os_release ubuntu 18.04 bionic
    write_sources archive.ubuntu.com "ubuntu bionic"
    write_aptget "$ETC/deadsnakes.list"
    cat > "$STUBS/curl" <<'EOF'
#!/usr/bin/env bash
printf -- "-----BEGIN PGP PUBLIC KEY BLOCK-----\nstub\n" > "${!#}"
exit 0
EOF
    cat > "$STUBS/gpg" <<'EOF'
#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o) shift; out="$1" ;;
    esac
    shift
done
mkdir -p "$(dirname "$out")" 2>/dev/null
echo "stub-key-material" > "$out"
exit 0
EOF
    chmod +x "$STUBS/curl" "$STUBS/gpg"

    local out
    out="$(run_install)"
    if [[ "$out" == *"PY=python3.1"* ]]; then pass "bionic provisions python via the manual key path"; else fail "bionic manual-key" "$out"; fi
    if [[ -f "$ETC/deadsnakes.list" ]] && grep -q "signed-by=" "$ETC/deadsnakes.list"; then
        pass "deadsnakes.list written with a signed-by keyring"
    else
        fail "bionic manual-key list" "$(cat "$ETC/deadsnakes.list" 2>/dev/null)"
    fi
    if [[ -s "$ETC/keyrings/deadsnakes.gpg" ]]; then pass "keyring material written"; else fail "bionic manual-key keyring" "empty or missing"; fi
}

# A modern interpreter is already installed: nothing may be provisioned.
scenario_existing_python() {
    setup_common
    write_old_python3
    "$STUBS/mkpy" python3.10
    write_os_release ubuntu 20.04 focal
    printf '#!/usr/bin/env bash\necho "APT WAS CALLED: $*" >&2; exit 99\n' > "$STUBS/apt-get"
    chmod +x "$STUBS/apt-get"

    local out
    out="$(run_install)"
    if [[ "$out" == *"PY=python3.10"* ]]; then pass "an existing python3.10 is picked"; else fail "focal existing" "$out"; fi
    if [[ "$out" != *"APT WAS CALLED"* ]]; then pass "apt untouched when an interpreter exists"; else fail "focal existing" "apt-get was invoked"; fi
}

# Debian 11: archived repos, and no modern python in the archives at all, so
# the installer must fall back to building one.
scenario_debian_source() {
    setup_common
    write_old_python3
    write_os_release debian 11 bullseye
    write_sources deb.debian.org "debian buster"
    write_aptget "$ETC/deadsnakes.list"
    "$STUBS/mkpy" "$WORK/local/bin/python3.11"

    EXTRA_OVERRIDES="build_python_from_source() { echo '$WORK/local/bin/python3.11'; }"
    local out
    out="$(run_install)"
    EXTRA_OVERRIDES=""
    if [[ "$out" == *"PY=$WORK/local/bin/python3.11"* ]]; then pass "debian 11 falls back to a source build"; else fail "debian source" "$out"; fi
    if grep -q "archive.debian.org" "$ETC/sources.list"; then pass "debian sources re-pointed to archive.debian.org"; else fail "debian eol remap" "sources.list unchanged"; fi
    if [[ -f "$ETC/apt.conf" ]]; then pass "check-valid-until disabled for the archived release"; else fail "debian apt.conf" "drop-in missing"; fi
}

# RHEL-family: python comes from dnf, no apt involved.
scenario_rocky_dnf() {
    setup_common
    write_os_release rocky 9 ""
    cat > "$STUBS/dnf" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
        python3.1[0-3]) mkpy "$arg" ;;
    esac
done
exit 0
EOF
    chmod +x "$STUBS/dnf"

    local out
    out="$(run_install)"
    if [[ "$out" == *"PY=python3.1"* ]]; then pass "rocky 9 provisions a modern python via dnf"; else fail "rocky dnf" "$out"; fi
}

scenario_alpine() {
    setup_common
    write_os_release alpine 3.20 ""
    cat > "$STUBS/apk" <<'EOF'
#!/usr/bin/env bash
mkpy python3
exit 0
EOF
    chmod +x "$STUBS/apk"

    local out
    out="$(run_install)"
    if [[ "$out" == *"PY=python3"* ]]; then pass "alpine provisions python3 via apk"; else fail "alpine" "$out"; fi
}

# Nothing identifiable and nothing installable: fail with an actionable error
# rather than a traceback.
scenario_unsupported() {
    setup_common
    write_os_release unknownlinux 1 ""
    printf '#!/usr/bin/env bash\nexit 100\n' > "$STUBS/apt-get"
    chmod +x "$STUBS/apt-get"

    local out
    out="$(run_install)"
    if [[ "$out" == *"could not be provisioned"* ]]; then pass "unknown distro without python errors cleanly"; else fail "unsupported" "$out"; fi
}

# ------------------------------------------------------------------ runner

# A fake pg-router CLI that records its argv, so the installer's delegation is
# observable without a real nginx.
write_cli_stub() {
    local rc="${1:-0}"
    cat > "$STUBS/pg-router" <<EOF
#!/usr/bin/env bash
echo "argv: \$*" >> "$WORK/cli.log"
exit $rc
EOF
    chmod +x "$STUBS/pg-router"
}

# nginx is missing: the installer must hand the work to 'pg-router install'.
scenario_nginx_delegated() {
    setup_common
    write_os_release ubuntu 22.04 jammy
    write_cli_stub 0

    local out
    out="$(run_install_nginx "$STUBS/pg-router")"
    if [[ -f "$WORK/cli.log" ]] && grep -qx "argv: install" "$WORK/cli.log"; then
        pass "installer delegates to 'pg-router install'"
    else
        fail "nginx delegation" "$(cat "$WORK/cli.log" 2>/dev/null)"
    fi
    if [[ "$out" == *"nginx and required modules are ready"* ]]; then
        pass "successful provisioning is reported"
    else
        fail "nginx delegation" "$out"
    fi
}

# PG_ROUTER_SKIP_NGINX=1: the CLI must never be invoked.
scenario_nginx_skipped() {
    setup_common
    write_os_release ubuntu 22.04 jammy
    write_cli_stub 0

    local out
    out="$(run_install_nginx "$STUBS/pg-router" 1)"
    if [[ ! -f "$WORK/cli.log" ]]; then
        pass "nginx provisioning is skipped entirely"
    else
        fail "nginx skip" "CLI was invoked: $(cat "$WORK/cli.log")"
    fi
    if [[ "$out" == *"PG_ROUTER_SKIP_NGINX=1"* ]]; then
        pass "skip is reported to the operator"
    else
        fail "nginx skip" "$out"
    fi
}

# The CLI cannot install nginx (locked-down host, unsupported distro, ...): the
# installer warns with an actionable hint but still finishes successfully.
scenario_nginx_failure_is_not_fatal() {
    setup_common
    write_os_release ubuntu 22.04 jammy
    write_cli_stub 3
    # Keeps the EOL-retry path hermetic: apt refresh succeeds, the retry still
    # fails because the CLI stub always exits 3.
    printf '#!/usr/bin/env bash\nexit 0\n' > "$STUBS/apt-get"
    chmod +x "$STUBS/apt-get"

    local out code
    out="$(run_install_nginx "$STUBS/pg-router")"
    code=$?
    if [[ "$out" == *"could not be installed automatically"* ]]; then
        pass "an unprovisionable nginx is reported clearly"
    else
        fail "nginx failure" "$out"
    fi
    if [[ "$code" -eq 0 ]]; then
        pass "install still completes without nginx"
    else
        fail "nginx failure" "main exited $code"
    fi
    if [[ "$(grep -c 'argv: install' "$WORK/cli.log" 2>/dev/null)" -ge 2 ]]; then
        pass "the apt retry re-invokes the installer"
    else
        fail "nginx retry" "expected >=2 CLI invocations, got $(grep -c 'argv: install' "$WORK/cli.log" 2>/dev/null)"
    fi
}

for scenario in "$@"; do
    case "$scenario" in
        bionic-ppa)   scenario_bionic_ppa ;;
        bionic-key)   scenario_bionic_manual_key ;;
        focal)        scenario_existing_python ;;
        debian-src)   scenario_debian_source ;;
        rocky)        scenario_rocky_dnf ;;
        alpine)       scenario_alpine ;;
        unsupported)  scenario_unsupported ;;
        nginx)        scenario_nginx_delegated ;;
        nginx-skip)   scenario_nginx_skipped ;;
        nginx-fails)  scenario_nginx_failure_is_not_fatal ;;
        *) echo "unknown scenario: $scenario"; failures=$((failures + 1)) ;;
    esac
done

echo
if [[ "$failures" -eq 0 ]]; then
    echo "ALL SCENARIOS PASSED"
    exit 0
fi
echo "$failures FAILURES"
exit 1
