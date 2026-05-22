#!/bin/bash
# test-build-cache-env.sh — assert t/build.sh passes BUILDX_CACHE_FROM/TO
# env vars through to `docker buildx build --load` and survives `set -u`
# with empty cache env / no --platform flag.
#
# Uses ZSTD_BUILD_DRYRUN=1 (handled inside t/build.sh) which makes the
# script print the resolved `docker buildx build --load …` argv on stdout
# and skip the actual docker invocation — no stub-docker shim, no
# spurious-pass risk.
#
# Mirrors t/test-gate-semantics.sh shape (pass/fail counter, exit 0/1).

# `-e` deliberately omitted — out2 captures rc=$? from a subshell whose
# nonzero exit (when build.sh trips `set -u` on empty arrays) is the
# observation under test, not a fatal harness error. Matches the convention
# used by t/test-gate-semantics.sh and the other self-tests under t/.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_SH="${REPO_ROOT}/t/build.sh"

pass_count=0
fail_count=0

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local label="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        printf '  PASS  %s (contains: %s)\n' "$label" "$needle"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — missing: %s\n' "$label" "$needle"
        printf '         output: %s\n' "$haystack"
        fail_count=$((fail_count + 1))
    fi
}

assert_not_contains() {
    local haystack="$1"
    local needle="$2"
    local label="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        printf '  FAIL  %s — unexpectedly present: %s\n' "$label" "$needle"
        printf '         output: %s\n' "$haystack"
        fail_count=$((fail_count + 1))
    else
        printf '  PASS  %s (absent: %s)\n' "$label" "$needle"
        pass_count=$((pass_count + 1))
    fi
}

if [ ! -f "$BUILD_SH" ]; then
    echo "missing build.sh at $BUILD_SH" >&2
    exit 2
fi

echo "=== build.sh BUILDX cache env passthrough ==="

# --- Assertion 1: cache env vars threaded into argv ---
out1=$(
    ZSTD_BUILD_DRYRUN=1 \
    BUILDX_CACHE_FROM=type=foo \
    BUILDX_CACHE_TO=type=bar,mode=max \
    bash "$BUILD_SH" ubuntu-24.04 2>&1
) || {
    printf '  FAIL  dry-run with cache env exited nonzero\n'
    printf '         output: %s\n' "$out1"
    fail_count=$((fail_count + 1))
}

assert_contains "$out1" "docker buildx build" \
    "dry-run with cache env emits docker buildx build line"
assert_contains "$out1" "--cache-from=type=foo" \
    "dry-run with cache env emits --cache-from"
assert_contains "$out1" "--cache-to=type=bar,mode=max" \
    "dry-run with cache env emits --cache-to"

echo
echo "=== build.sh no-cache-env emits no cache flags ==="

# --- Assertion 2: no cache env → no cache flags ---
# Explicitly unset cache vars in the subshell so the parent env can't leak in.
out2_rc=0
out2=$(
    unset BUILDX_CACHE_FROM BUILDX_CACHE_TO || true
    ZSTD_BUILD_DRYRUN=1 bash "$BUILD_SH" ubuntu-24.04 2>&1
) || out2_rc=$?

assert_contains "$out2" "docker buildx build" \
    "dry-run without cache env still emits docker buildx build line"
assert_not_contains "$out2" "--cache-from" \
    "dry-run without cache env does not emit --cache-from"
assert_not_contains "$out2" "--cache-to" \
    "dry-run without cache env does not emit --cache-to"

echo
echo "=== build.sh set -u survival with empty arrays ==="

# --- Assertion 3: rc=0 when no env at all (set -u empty-array trap) ---
if [ "$out2_rc" -eq 0 ]; then
    printf '  PASS  dry-run without cache env exits 0 (set -u survives empty arrays)\n'
    pass_count=$((pass_count + 1))
else
    printf '  FAIL  dry-run without cache env exited %d (set -u likely tripped on empty array)\n' "$out2_rc"
    printf '         output: %s\n' "$out2"
    fail_count=$((fail_count + 1))
fi

echo
echo "=== summary ==="
printf '  passed: %d\n' "$pass_count"
printf '  failed: %d\n' "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi

exit 0
