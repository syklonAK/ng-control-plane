#!/usr/bin/env bash
# install.sh must work under its own documented one-line form:
#   curl -fsSL .../install.sh | sudo bash
#
# When bash reads a script from stdin BASH_SOURCE is an empty array, so the
# guard that keeps main() from running while the file is sourced used to trip
# `set -u` and abort the install with "BASH_SOURCE[0]: unbound variable" before
# anything happened. The guard must: run main when executed (file or stdin),
# stay silent when sourced (the test suite relies on that).
#
# usage: installer_piped.sh <path/to/install.sh>
set -uo pipefail

install_sh="$1"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0

pass() { echo "ok   $1"; }
fail() { echo "FAIL $1: $2"; failures=$((failures + 1)); }

# Replace the real main with a marker so the guard can be exercised without
# actually installing anything.
probe="$work/probe.sh"
sed 's/^    main "\$@"$/    echo GUARD_REACHED_MAIN/' "$install_sh" > "$probe"

# 1. Piped through bash — the documented one-line install form.
out="$(bash -s < "$probe" 2>&1)"
if [[ "$out" == *"GUARD_REACHED_MAIN"* ]]; then
    pass "piped via stdin runs main"
else
    fail "piped via stdin runs main" "no marker; output: $out"
fi
if [[ "$out" == *"unbound variable"* ]]; then
    fail "piped via stdin has no unbound-variable abort" "$out"
fi

# 2. Executed as a plain file.
out="$(bash "$probe" 2>&1)"
if [[ "$out" == *"GUARD_REACHED_MAIN"* ]]; then
    pass "executed as a file runs main"
else
    fail "executed as a file runs main" "no marker; output: $out"
fi

# 3. Sourced — the unit tests need the helpers without main running.
out="$(bash -c "source '$probe'; echo SOURCED_OK" 2>&1)"
if [[ "$out" == *"SOURCED_OK"* && "$out" != *"GUARD_REACHED_MAIN"* ]]; then
    pass "sourced exposes helpers without running main"
else
    fail "sourced exposes helpers without running main" "$out"
fi

# 4. The real, unmodified installer must still be syntax-clean under strict mode.
if bash -n "$install_sh" 2>/dev/null; then
    pass "real installer parses cleanly"
else
    fail "real installer parses cleanly" "bash -n reported an error"
fi

# 5. Strict mode must hold: no executable line may expand BASH_SOURCE (a
#    literal mention in a comment explaining the fix is fine, so comments are
#    stripped first).
if grep -vE '^\s*#' "$install_sh" | grep -q '${BASH_SOURCE'; then
    fail "no BASH_SOURCE expansion usage" "found one in $install_sh"
else
    pass "no BASH_SOURCE expansion usage"
fi

if [[ "$failures" -eq 0 ]]; then
    echo "ALL PASSED"
    exit 0
fi
echo "$failures FAILED"
exit 1
