#!/usr/bin/env bash
# Unit tests for install.sh helper functions (version comparison, distro
# detection, deadsnakes eligibility). install.sh is sourced, which is why it
# guards main() behind a BASH_SOURCE check.
#
# usage: installer_units.sh <path/to/install.sh>
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

# shellcheck source=/dev/null
source "$install_sh"
# install.sh enables strict mode; the assertions need lenient mode.
set +euo pipefail

# ------------------------------------------------------------- version_ge
yn() { if "$@"; then echo y; else echo n; fi; }
check "version_ge 24.04 >= 18.04" "$(yn version_ge 24.04 18.04)" "y"
check "version_ge 18.04 >= 24.04" "$(yn version_ge 18.04 24.04)" "n"
check "version_ge 3.11.9 >= 3.10"  "$(yn version_ge 3.11.9 3.10)" "y"
check "version_ge 3.9 >= 3.10"     "$(yn version_ge 3.9 3.10)"    "n"
check "version_ge 3.10 >= 3.10"    "$(yn version_ge 3.10 3.10)"   "y"
check "version_ge 3.10.15 >= 3.10" "$(yn version_ge 3.10.15 3.10)" "y"
check "version_ge 1.1.1 >= 1.1.1"  "$(yn version_ge 1.1.1 1.1.1)" "y"
check "version_ge 1.0.2 >= 1.1.1"  "$(yn version_ge 1.0.2 1.1.1)" "n"
check "version_ge 3.20 >= 3.10"    "$(yn version_ge 3.20 3.10)"   "y"

# ------------------------------------------------------------ detect_distro
write_release() {
    cat > "$work/os-release" <<EOF
$1
EOF
    PG_ROUTER_OS_RELEASE="$work/os-release" detect_distro
}

write_release 'ID=ubuntu
VERSION_ID="18.04"
VERSION_CODENAME=bionic'
check "bionic id"        "$DISTRO_ID"         "ubuntu"
check "bionic version"   "$DISTRO_VERSION_ID" "18.04"
check "bionic codename"  "$DISTRO_CODENAME"   "bionic"

# no VERSION_CODENAME at all (some minimal images)
write_release 'ID=debian
VERSION_ID="11"'
check "debian11 id"       "$DISTRO_ID"         "debian"
check "debian11 codename" "$DISTRO_CODENAME"   "bullseye"

write_release 'ID="alpine"
VERSION_ID=3.20'
check "alpine id"      "$DISTRO_ID"         "alpine"
check "alpine version" "$DISTRO_VERSION_ID" "3.20"

# Linux Mint advertises its own codename; the archives use Ubuntu's.
write_release 'ID=linuxmint
VERSION_ID="21"
VERSION_CODENAME="virginia"'
check "mint 21 -> jammy" "$DISTRO_CODENAME" "jammy"

write_release 'ID=linuxmint
VERSION_ID="6"'
check "LMDE 6 keeps its version" "$DISTRO_VERSION_ID" "6"

# a host without os-release (containers built from scratch)
PG_ROUTER_OS_RELEASE="$work/does-not-exist" detect_distro
check "no os-release -> unknown" "$DISTRO_ID" "unknown"

# ------------------------------------------------------------ use_deadsnakes
ds() { if use_deadsnakes; then echo y; else echo n; fi; }

DISTRO_ID=ubuntu;     DISTRO_VERSION_ID=18.04; check "ubuntu 18.04 deadsnakes" "$(ds)" "y"
DISTRO_ID=ubuntu;     DISTRO_VERSION_ID=20.04; check "ubuntu 20.04 deadsnakes" "$(ds)" "y"
DISTRO_ID=ubuntu;     DISTRO_VERSION_ID=22.04; check "ubuntu 22.04 deadsnakes" "$(ds)" "y"
DISTRO_ID=ubuntu;     DISTRO_VERSION_ID=24.04; check "ubuntu 24.04 deadsnakes" "$(ds)" "n"
DISTRO_ID=ubuntu;     DISTRO_VERSION_ID="";    check "ubuntu unknown version"  "$(ds)" "n"
DISTRO_ID=debian;     DISTRO_VERSION_ID=11;    check "debian 11 deadsnakes"    "$(ds)" "n"
DISTRO_ID=linuxmint;  DISTRO_VERSION_ID=20;    check "mint 20 deadsnakes"       "$(ds)" "y"
DISTRO_ID=linuxmint;  DISTRO_VERSION_ID=6;     check "LMDE 6 deadsnakes"        "$(ds)" "n"
DISTRO_ID=fedora;     DISTRO_VERSION_ID=40;    check "fedora deadsnakes"        "$(ds)" "n"

echo
if [[ "$failures" -eq 0 ]]; then
    echo "ALL PASSED"
    exit 0
fi
echo "$failures FAILURES"
exit 1
