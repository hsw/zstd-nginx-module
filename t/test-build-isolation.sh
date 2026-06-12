#!/bin/bash
# t/test-build-isolation.sh — build-isolation self-test for filter/config and
# static/config.
#
# Coverage for the audit Tier-3 build-system cluster (C11-1, C11-2, C11-4,
# N03-3, N03-4): the regression gap that let C11-1 live was that the Docker
# matrix only ever builds the COMBINED module set (root config), where
# static/config's global CFLAGS mutation papers over filter/config routing
# flag-shaped tokens through ngx_module_incs (rewritten to `-I <token>` by
# auto/make). This script builds the modules in isolation and asserts the
# post-fix invariants:
#
#   filter-only           --add-dynamic-module=<repo>/filter alone configures,
#                         compiles, and the .so loads under `nginx -t`
#   static-only           --add-dynamic-module=<repo>/static alone builds and
#                         the .so has NO libzstd in its NEEDED entries
#   combined-hygiene      root-config build with a sentinel --with-ld-opt:
#                         link-recipe-level asserts (static .so recipe has no
#                         libzstd; NGX_LD_OPT appears exactly once per module
#                         recipe) plus the NEEDED checks
#   half-set              exactly one of ZSTD_INC/ZSTD_LIB set => configure
#                         fails fast via the both-or-none guard, naming BOTH
#                         variables
#   static-explicit-paths static --add-module with ZSTD_INC/ZSTD_LIB set:
#                         archive-first probe wins; with libzstd.a hidden the
#                         shared fallback wins and no .a leaks into the link
#   static-only-no-libzstd  with zstd.h + libzstd hidden, the static-only
#                         build still succeeds (module uses no zstd symbol)
#   makefile-hygiene      combined configure leaves no `-I -D…` / `-I -I…`
#                         artifact in objs/Makefile
#
# Like t/test-explicit-paths.sh this re-runs nginx configure + `make modules`
# inside the already-built ubuntu-24.04 test image, mounting the CURRENT
# working tree read-only so local config edits are exercised. Cases run in
# independent throwaway containers; a failing case does not stop the others.
#
# Exit codes follow the autoconf SKIP convention:
#   0  all cases pass
#   77 skip (docker missing / image absent)
#   1  at least one case failed

set -uo pipefail

IMAGE=${IMAGE:-zstd-nginx-test:ubuntu-24.04}

if ! command -v docker >/dev/null 2>&1; then
    echo "SKIP: docker not on PATH"
    exit 77
fi

# Use `docker images -q` (not `docker image inspect`) for the presence check:
# under Docker Desktop's containerd-snapshotter, `image inspect` by name:tag
# returns "No such image" for buildkit-produced multi-arch manifest entries
# even when the image is fully usable via `docker run`. `images -q` resolves
# the tag correctly in both stores. Mirrors t/run.sh:99.
if [ -z "$(docker images -q "$IMAGE" 2>/dev/null)" ]; then
    echo "SKIP: image $IMAGE not built (run \`bash t/build.sh ubuntu-24.04\` first)"
    exit 77
fi

# Mount the CURRENT working tree at /src-current so any local edits to
# filter/config + static/config are picked up — the baked /src in the
# image is a snapshot from image-build time.
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

LOG_DIR=$(mktemp -d)
trap 'rm -rf "$LOG_DIR"' EXIT

FAILED_CASES=()
PASSED_CASES=()

# run_case <name> <in-container-bash-script>
# Runs the script in a throwaway container with the working tree mounted
# read-only. PASS/FAIL is decided by the container exit code. On failure
# the log tail is printed for diagnosis.
run_case() {
    local name=$1
    local script=$2
    local log="$LOG_DIR/$name.log"

    echo "=== case $name ==="
    if docker run --rm \
        -v "$REPO_ROOT:/src-current:ro" \
        "$IMAGE" bash -c "$script" >"$log" 2>&1; then
        echo "PASS: $name"
        PASSED_CASES+=("$name")
    else
        echo "FAIL: $name"
        echo "--- last 30 log lines ($name) ---"
        tail -30 "$log"
        echo "--- end log ($name) ---"
        FAILED_CASES+=("$name")
    fi
}

# Shared in-container preamble: fresh copy of the baked nginx source tree
# (matches the apt-installed nginx binary; --with-compat keeps the module
# ABI-compatible so `nginx -t` can load the produced .so).
PREAMBLE='
set -euo pipefail
cp -a /usr/local/src/nginx /tmp/nginx-build
cd /tmp/nginx-build
'

# --- case filter-only ------------------------------------------------------
# C11-1: filter/config must be self-contained. Pre-fix, configure passed (the
# feature probe carried -DZSTD_STATIC_LINKING_ONLY via CC_TEST_FLAGS) but
# `make modules` broke: ngx_module_incs held flag-shaped tokens that
# auto/make rewrote to `-I -DZSTD_STATIC_LINKING_ONLY`, so the compiler
# never saw the define and zstd.h hid ZSTD_customMem.
run_case filter-only "$PREAMBLE"'
./configure --with-compat --add-dynamic-module=/src-current/filter
make -j"$(nproc)" modules
test -f objs/ngx_http_zstd_filter_module.so
cat > /tmp/test-nginx.conf <<EOF
load_module /tmp/nginx-build/objs/ngx_http_zstd_filter_module.so;
events {}
http {}
EOF
nginx -c /tmp/test-nginx.conf -t
'

# --- case static-only ------------------------------------------------------
# N03-4 + C11-4: the static module uses no zstd symbol, so a static-only
# build must neither require libzstd at configure time nor record it as a
# NEEDED dependency of the .so.
run_case static-only "$PREAMBLE"'
./configure --with-compat --add-dynamic-module=/src-current/static
make -j"$(nproc)" modules
test -f objs/ngx_http_zstd_static_module.so
echo "--- NEEDED entries (static .so) ---"
readelf -d objs/ngx_http_zstd_static_module.so | grep NEEDED || true
if readelf -d objs/ngx_http_zstd_static_module.so | grep NEEDED | grep -q libzstd; then
    echo "ASSERT-FAIL: static-only .so has libzstd in NEEDED"
    exit 1
fi
cat > /tmp/test-nginx.conf <<EOF
load_module /tmp/nginx-build/objs/ngx_http_zstd_static_module.so;
events {}
http {}
EOF
nginx -c /tmp/test-nginx.conf -t
'

# --- case combined-hygiene -------------------------------------------------
# Root-config build (the path every Docker variant already exercises): the
# filter .so legitimately links libzstd; the static .so must not inherit it
# (C11-4 stale ngx_module_libs / N03-3 NGX_LD_OPT duplication).
#
# The teeth are at the objs/Makefile LINK-RECIPE level: the NEEDED checks
# below are supplementary only — gcc defaults to -Wl,--as-needed on Ubuntu,
# which strips an unused -lzstd from NEEDED, so pre-fix link-line pollution
# never showed up there.
#
#   C11-4: the static .so link recipe must carry no libzstd at all.
#   N03-3: configure with a sentinel --with-ld-opt. nginx legitimately puts
#          NGX_LD_OPT ONCE on every dynamic-module link recipe
#          (auto/make:609-610 emits "$NGX_LD_OPT $ngx_module_libs"); the bug
#          duplicated it by assigning ngx_module_libs=$NGX_LD_OPT. Assert
#          the sentinel appears EXACTLY ONCE per module recipe.
run_case combined-hygiene "$PREAMBLE"'
./configure --with-compat --add-dynamic-module=/src-current \
    --with-ld-opt=-L/opt/n033-sentinel

# extract_recipe <module>: the objs/Makefile block from the .so target line
# through the $(LINK) recipe. auto/make emits a blank line BETWEEN the
# dependency list and the recipe, so stop at the SECOND blank line (the one
# after the recipe), not the first.
extract_recipe() {
    awk -v target="objs/$1.so:" "
        index(\$0, target) == 1 { f = 1 }
        f && /^\$/ { blank++; if (blank == 2) exit }
        f { print }
    " objs/Makefile
}
extract_recipe ngx_http_zstd_filter_module > /tmp/filter-recipe.txt
extract_recipe ngx_http_zstd_static_module > /tmp/static-recipe.txt
test -s /tmp/filter-recipe.txt
test -s /tmp/static-recipe.txt

# C11-4: no libzstd in the static .so link recipe. Do NOT grep bare "zstd" —
# the module source paths in the recipe contain it.
if grep -E -- "-lzstd|libzstd\.(a|so)" /tmp/static-recipe.txt; then
    echo "ASSERT-FAIL: static .so link recipe references libzstd (lines above)"
    exit 1
fi
if ! grep -qE -- "-lzstd|libzstd\.(a|so)" /tmp/filter-recipe.txt; then
    echo "ASSERT-FAIL: filter .so link recipe is MISSING libzstd"
    exit 1
fi

# N03-3: sentinel exactly once per module link recipe.
for m in filter static; do
    n=$( (grep -o -- "-L/opt/n033-sentinel" /tmp/$m-recipe.txt || true) | wc -l)
    if [ "$n" -ne 1 ]; then
        echo "ASSERT-FAIL: --with-ld-opt sentinel appears $n times in the $m .so link recipe (expected exactly 1)"
        exit 1
    fi
done

make -j"$(nproc)" modules
test -f objs/ngx_http_zstd_filter_module.so
test -f objs/ngx_http_zstd_static_module.so
echo "--- NEEDED entries (filter .so) ---"
readelf -d objs/ngx_http_zstd_filter_module.so | grep NEEDED || true
echo "--- NEEDED entries (static .so) ---"
readelf -d objs/ngx_http_zstd_static_module.so | grep NEEDED || true
if ! readelf -d objs/ngx_http_zstd_filter_module.so | grep NEEDED | grep -q libzstd; then
    echo "ASSERT-FAIL: filter .so is MISSING libzstd in NEEDED"
    exit 1
fi
if readelf -d objs/ngx_http_zstd_static_module.so | grep NEEDED | grep -q libzstd; then
    echo "ASSERT-FAIL: static .so has libzstd in NEEDED"
    exit 1
fi
'

# --- case half-set ---------------------------------------------------------
# C11-2: exactly one of ZSTD_INC/ZSTD_LIB set must make configure fail fast
# with an error naming BOTH variables (instead of emitting dangling -I/-L
# that swallow the next flag). Asserting the guard's distinctive "must be
# set together" phrase matters: the pre-fix ZSTD_INC-only failure mode
# (probe link failure) already exited 1 with a message naming both
# variables, so a names-only grep could not tell the guard from the old
# accidental probe failure.
run_case half-set "$PREAMBLE"'
set +e

ZSTD_INC=/usr/include ./configure --with-compat --add-dynamic-module=/src-current \
    > /tmp/half-inc.log 2>&1
rc_inc=$?
echo "ZSTD_INC-only: configure exit code $rc_inc"
tail -8 /tmp/half-inc.log

ZSTD_LIB="/usr/lib/$(gcc -print-multiarch)" ./configure --with-compat --add-dynamic-module=/src-current \
    > /tmp/half-lib.log 2>&1
rc_lib=$?
echo "ZSTD_LIB-only: configure exit code $rc_lib"
tail -8 /tmp/half-lib.log

fail=0
# check_half <inc|lib> <configure-exit-code>
check_half() {
    if [ "$2" -eq 0 ]; then
        echo "ASSERT-FAIL: half-set ($1-only) configure succeeded; must fail fast"
        fail=1
    elif ! grep -q "must be set together" /tmp/half-$1.log; then
        echo "ASSERT-FAIL: half-set ($1-only) failure is not the both-or-none guard"
        fail=1
    elif ! grep -q ZSTD_INC /tmp/half-$1.log || ! grep -q ZSTD_LIB /tmp/half-$1.log; then
        echo "ASSERT-FAIL: half-set ($1-only) error does not name both ZSTD_INC and ZSTD_LIB"
        fail=1
    fi
}
check_half inc "$rc_inc"
check_half lib "$rc_lib"
exit $fail
'

# --- case static-explicit-paths --------------------------------------------
# Explicit-path STATIC branch of filter/config (--add-module with
# ZSTD_INC/ZSTD_LIB set): two consecutive auto/feature probes — the archive
# ($ZSTD_LIB/libzstd.a) is tried first, the shared library is the fallback.
# Configure-level assertions only (a full static nginx make is not needed to
# prove the probe/link wiring):
#
#   run 1 (libzstd.a present): the "ZStandard static library in ..." probe
#         wins and the archive path lands in objs/Makefile.
#   run 2 (libzstd.a hidden):  the archive probe reports "not found", the
#         shared "ZStandard dynamic library in ..." fallback wins, and NO
#         libzstd.a leaks into objs/Makefile — this is the per-probe
#         ngx_feature_libs reassignment contract (a stale first-probe value
#         would link the .a path on the fallback too).
run_case static-explicit-paths "$PREAMBLE"'
export ZSTD_INC=/usr/include
export ZSTD_LIB="/usr/lib/$(gcc -print-multiarch)"
if [ ! -f /usr/include/zstd.h ] || [ ! -f "$ZSTD_LIB/libzstd.a" ]; then
    echo "ENV-FAIL: libzstd dev artifacts not at the assumed Ubuntu layout"
    exit 1
fi

./configure --with-compat --add-module=/src-current > /tmp/static-archive.log 2>&1
grep -E "ZStandard static library in .* \.\.\. found$" /tmp/static-archive.log \
    || { echo "ASSERT-FAIL: archive-first probe did not win with libzstd.a present"; tail -20 /tmp/static-archive.log; exit 1; }
grep -q "libzstd\.a" objs/Makefile \
    || { echo "ASSERT-FAIL: libzstd.a missing from objs/Makefile link line"; exit 1; }

mv "$ZSTD_LIB/libzstd.a" /tmp/
./configure --with-compat --add-module=/src-current > /tmp/static-shared.log 2>&1
grep -E "ZStandard static library in .* \.\.\. not found$" /tmp/static-shared.log \
    || { echo "ASSERT-FAIL: archive probe unexpectedly succeeded with libzstd.a hidden"; tail -20 /tmp/static-shared.log; exit 1; }
grep -E "ZStandard dynamic library in .* \.\.\. found$" /tmp/static-shared.log \
    || { echo "ASSERT-FAIL: shared fallback probe did not win"; tail -20 /tmp/static-shared.log; exit 1; }
if grep -q "libzstd\.a" objs/Makefile; then
    echo "ASSERT-FAIL: stale libzstd.a leaked into the shared-fallback link line"
    exit 1
fi
grep -qE -- "-lzstd" objs/Makefile \
    || { echo "ASSERT-FAIL: -lzstd missing from the shared-fallback link line"; exit 1; }
'

# --- case static-only-no-libzstd -------------------------------------------
# N03-4: hide the libzstd DEV artifacts, then a static-only build must STILL
# succeed — the module includes no zstd header and uses no zstd symbol.
# Pre-fix, static/config ran the full feature probe and hard-exited with
# "requires the ZStandard library". Container is discarded; no cleanup needed.
#
# Only the dev artifacts (header, unversioned .so symlink, archive) are
# hidden: the runtime libzstd.so.1 must STAY — Ubuntu 24.04's cc1 itself
# links libzstd.so.1, so hiding it breaks the compiler ("C compiler cc is
# not found") and the case would fail for environment, not tree, reasons.
# The explicit pre-checks keep an environment drift (different image layout
# via IMAGE override) from surfacing as a confusing mv failure under set -e.
run_case static-only-no-libzstd "$PREAMBLE"'
if [ ! -f /usr/include/zstd.h ] || ! ls /usr/lib/*/libzstd.so >/dev/null 2>&1; then
    echo "ENV-FAIL: libzstd dev artifacts not at the assumed Ubuntu layout"
    exit 1
fi
mv /usr/include/zstd.h /tmp/
mv /usr/lib/*/libzstd.so /tmp/
mv /usr/lib/*/libzstd.a /tmp/ 2>/dev/null || true
./configure --with-compat --add-dynamic-module=/src-current/static
make -j"$(nproc)" modules
test -f objs/ngx_http_zstd_static_module.so
'

# --- case makefile-hygiene -------------------------------------------------
# C11-1 mechanism check: after a combined configure, objs/Makefile must not
# contain the dead `-I <flag>` artifact produced by auto/make rewriting
# flag-shaped ngx_module_incs tokens (`-I -DZSTD_STATIC_LINKING_ONLY`,
# `-I -I/...`). Grep pinned to the observed RED-run token spacing: auto/make
# emits each rewritten token on its own continuation line as `-I <token>`
# with a single space (observed: "-I -DZSTD_STATIC_LINKING_ONLY \").
run_case makefile-hygiene "$PREAMBLE"'
./configure --with-compat --add-dynamic-module=/src-current
if grep -nE -- "-I -[DIL]" objs/Makefile; then
    echo "ASSERT-FAIL: objs/Makefile contains -I <flag> artifacts (lines above)"
    exit 1
fi
echo "objs/Makefile clean of -I <flag> artifacts"
'

# --- summary ----------------------------------------------------------------
echo ""
echo "=== summary ==="
for c in "${PASSED_CASES[@]+"${PASSED_CASES[@]}"}"; do echo "PASS: $c"; done
for c in "${FAILED_CASES[@]+"${FAILED_CASES[@]}"}"; do echo "FAIL: $c"; done

if [ "${#FAILED_CASES[@]}" -gt 0 ]; then
    echo "FAIL: ${#FAILED_CASES[@]} case(s) failed"
    exit 1
fi

echo "PASS: all build-isolation cases green"
