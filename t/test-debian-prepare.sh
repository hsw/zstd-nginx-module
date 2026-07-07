#!/bin/bash
# test-debian-prepare.sh — assertions for debian/prepare.sh placeholder
# substitution.
#
# Replays the bundled control.in.fixture through debian/prepare.sh and
# compares stdout / written control file to the expected fixtures.
#
# The substitution rule (patch-exact ABI pin):
#     NGINX_VERSION_LOWER = <major>.<minor>.<patch>     (exact build version)
#     NGINX_VERSION_UPPER = <major>.<minor>.<patch+1>   (next patch release)
#
# Fixtures live in t/fixtures/debian/:
#   - control.in.fixture                — input with @NGINX_VERSION_*@ placeholders
#   - expected-control-1.29.5.txt       — happy path (LOWER=1.29.5 UPPER=1.29.6)
#   - expected-control-1.29.0.txt       — edge case (patch already .0)
#
# Run:   bash t/test-debian-prepare.sh
# Exit:  0 = all assertions hold; 1 = any assertion failed; 2 = harness error
#        (script missing or non-executable — expected RED state until Task 2
#        lands debian/prepare.sh).

# `-e` deliberately omitted — assertion helpers below capture nonzero exit
# codes from invocations under test (assert_rejects expects rc!=0 as the
# pass condition) and would short-circuit the harness under `set -e`.
# Matches the convention used by t/test-gate-semantics.sh, t/test-debian-rules.sh,
# and t/test-build-cache-env.sh.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREPARE_SCRIPT="${REPO_ROOT}/debian/prepare.sh"
FIX_DIR="${REPO_ROOT}/t/fixtures/debian"
CONTROL_IN_FIXTURE="${FIX_DIR}/control.in.fixture"

fail_count=0
pass_count=0

if [ ! -x "$PREPARE_SCRIPT" ]; then
    echo "missing or non-executable script: $PREPARE_SCRIPT" >&2
    exit 2
fi

if [ ! -f "$CONTROL_IN_FIXTURE" ]; then
    echo "missing fixture: $CONTROL_IN_FIXTURE" >&2
    exit 2
fi

# Prepare an isolated workspace: copy fixture to <tmp>/debian/control.in and
# run prepare.sh from that working dir so it writes <tmp>/debian/control. We
# never mutate the real debian/ tree here.
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "${work_dir}/debian"
cp "$CONTROL_IN_FIXTURE" "${work_dir}/debian/control.in"

assert_substitution() {
    local nginx_version="$1"
    local expected="$2"
    local label="$3"
    local rc=0
    local out

    # Re-prime the input each call — earlier runs may have written control.
    cp "$CONTROL_IN_FIXTURE" "${work_dir}/debian/control.in"
    rm -f "${work_dir}/debian/control"

    out=$(cd "$work_dir" && "$PREPARE_SCRIPT" "$nginx_version" 2>&1) || rc=$?

    if [ "$rc" -ne 0 ]; then
        printf '  FAIL  %s — prepare.sh exited %d\n' "$label" "$rc"
        printf '         output: %s\n' "$out"
        fail_count=$((fail_count + 1))
        return
    fi

    if [ ! -f "${work_dir}/debian/control" ]; then
        printf '  FAIL  %s — debian/control was not written\n' "$label"
        fail_count=$((fail_count + 1))
        return
    fi

    if diff -u "$expected" "${work_dir}/debian/control" >/dev/null; then
        printf '  PASS  %s (nginx=%s → control matches %s)\n' \
            "$label" "$nginx_version" "$(basename "$expected")"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — output diverged from %s\n' "$label" "$(basename "$expected")"
        diff -u "$expected" "${work_dir}/debian/control" | sed 's/^/         /'
        fail_count=$((fail_count + 1))
    fi
}

assert_rejects() {
    local label="$1"
    shift
    local rc=0
    local out

    cp "$CONTROL_IN_FIXTURE" "${work_dir}/debian/control.in"

    out=$(cd "$work_dir" && "$PREPARE_SCRIPT" "$@" 2>&1) || rc=$?

    if [ "$rc" -ne 0 ]; then
        printf '  PASS  %s (rc=%d as expected)\n' "$label" "$rc"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — prepare.sh exited 0, expected nonzero\n' "$label"
        printf '         output: %s\n' "$out"
        fail_count=$((fail_count + 1))
    fi
}

echo "=== debian/prepare.sh substitution ==="

assert_substitution "1.29.5" "${FIX_DIR}/expected-control-1.29.5.txt" \
    "happy path: 1.29.5 → LOWER=1.29.5 UPPER=1.29.6"

assert_substitution "1.29.0" "${FIX_DIR}/expected-control-1.29.0.txt" \
    "edge: patch is already .0 (1.29.0 → LOWER=1.29.0 UPPER=1.29.1)"

echo
echo "=== debian/prepare.sh input validation ==="

assert_rejects "no args → usage error"
assert_rejects "non-numeric component (1.29.foo)" "1.29.foo"
assert_rejects "incomplete version (1)" "1"
# T4 edge cases: strict format check must reject 4-part versions, trailing
# whitespace, and pre-release suffixes. The parser pins the input shape to
# <digits>.<digits>.<digits> with no extras (anchored regex).
assert_rejects "four-component version (1.29.5.0)" "1.29.5.0"
assert_rejects "trailing whitespace (\"1.29.5 \")" "1.29.5 "
assert_rejects "pre-release suffix (1.29.5-beta1)" "1.29.5-beta1"

echo
echo "=== debian/prepare.sh patch-rollover edge ==="

# T4: ensure patch=9 increments to patch=10 (string-vs-arithmetic trap).
# Build expected control on the fly from the fixture so we don't need to
# commit yet another expected-* file for a one-off math check.
rollover_expected="${work_dir}/expected-control-1.29.9.txt"
sed -e 's/@NGINX_VERSION_LOWER@/1.29.9/g' \
    -e 's/@NGINX_VERSION_UPPER@/1.29.10/g' \
    "$CONTROL_IN_FIXTURE" > "$rollover_expected"
assert_substitution "1.29.9" "$rollover_expected" \
    "patch=9 rollover (1.29.9 → LOWER=1.29.9 UPPER=1.29.10)"

echo
echo "=== debian/prepare.sh production control.in round-trip ==="

# T2: exercise the REAL debian/control.in (2 binary packages, 23 lines) so a
# divergence between the fixture and the production input is caught here.
# Run prepare.sh from a sandbox holding a copy of the production control.in,
# check there are no remaining placeholders, and confirm both binary-package
# Depends lines were substituted with the expected lower/upper pins.
prod_label="production debian/control.in round-trip"
prod_dir="${work_dir}/prod"
mkdir -p "${prod_dir}/debian"
cp "${REPO_ROOT}/debian/control.in" "${prod_dir}/debian/control.in"
prod_rc=0
prod_out=$(cd "$prod_dir" && "$PREPARE_SCRIPT" "1.29.5" 2>&1) || prod_rc=$?

if [ "$prod_rc" -ne 0 ]; then
    printf '  FAIL  %s — exited %d\n         output: %s\n' "$prod_label" "$prod_rc" "$prod_out"
    fail_count=$((fail_count + 1))
elif [ ! -f "${prod_dir}/debian/control" ]; then
    printf '  FAIL  %s — control not written\n' "$prod_label"
    fail_count=$((fail_count + 1))
elif grep -q '@NGINX_VERSION_' "${prod_dir}/debian/control"; then
    printf '  FAIL  %s — unsubstituted placeholders remain\n' "$prod_label"
    grep '@NGINX_VERSION_' "${prod_dir}/debian/control" | sed 's/^/         /'
    fail_count=$((fail_count + 1))
else
    lower_hits=$(grep -c 'nginx (>= 1.29.5)' "${prod_dir}/debian/control" || true)
    upper_hits=$(grep -c 'nginx (<< 1.29.6)' "${prod_dir}/debian/control" || true)
    if [ "$lower_hits" -eq 2 ] && [ "$upper_hits" -eq 2 ]; then
        printf '  PASS  %s (both packages pin lower=1.29.5 upper=1.29.6)\n' "$prod_label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — expected 2 of each pin, got lower=%d upper=%d\n' \
            "$prod_label" "$lower_hits" "$upper_hits"
        fail_count=$((fail_count + 1))
    fi
fi

# Extra drift guard: the production round-trip greps above only check the
# nginx version pins. If someone adds (or removes) a Build-Depends token in
# debian/control.in but forgets to update t/fixtures/debian/control.in.fixture,
# the fixture-driven tests still pass and the drift goes unnoticed (codex
# iter4 G2). Assert the Build-Depends line is preserved verbatim by prepare.sh
# — control.in has no placeholders on this line, so input must equal output.
drift_label="production Build-Depends preserved verbatim"
if [ -f "${prod_dir}/debian/control" ]; then
    in_bd=$(grep '^Build-Depends:' "${REPO_ROOT}/debian/control.in" || true)
    out_bd=$(grep '^Build-Depends:' "${prod_dir}/debian/control" || true)
    if [ -n "$in_bd" ] && [ "$in_bd" = "$out_bd" ]; then
        printf '  PASS  %s\n' "$drift_label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s\n         input:  %s\n         output: %s\n' \
            "$drift_label" "$in_bd" "$out_bd"
        fail_count=$((fail_count + 1))
    fi
fi

echo
echo "=== summary ==="
printf '  passed: %d\n' "$pass_count"
printf '  failed: %d\n' "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi

exit 0
