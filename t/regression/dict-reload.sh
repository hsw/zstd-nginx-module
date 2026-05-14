#!/bin/bash
# dict-reload.sh — regression for Task 7 (CDict pool-cleanup hook).
#
# Background: ngx_http_zstd_filter_merge_loc_conf() calls
# ZSTD_createCDict_byReference() when zstd_dict_file is set. Before the fix
# (commit e6f2899), every `nginx -s reload` allocated a new CDict via that
# call but never freed the previous one, leaking one CDict's worth of memory
# (heap + libzstd-internal tables, typically tens of KiB per reload). The
# fix registers a pool cleanup so the CDict is freed when its config-cycle
# pool is destroyed.
#
# Coverage: render a config with zstd_dict_file pointing at a small file,
# reload nginx N times, sample master RSS before/after, assert delta stays
# under a generous threshold. With the leak, RSS climbs linearly with the
# reload count (CDict + libzstd state ≈ tens of KiB per cycle).
#
# Runs inside the container.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="dict-reload"
TMPDIR="$(mktemp -d)"

cleanup() {
    stop_local_nginx
    rm -rf "$TMPDIR"
}
trap 'cleanup' EXIT

# Generate a small dict file. libzstd accepts arbitrary data ≥ 8 bytes as a
# raw-content prefix dict; we don't need a trained dictionary to exercise the
# CDict allocation path.
DICT_FILE="/var/fixtures/zstd-dict-reload"
mkdir -p /var/fixtures
dd if=/dev/zero bs=4096 count=1 status=none | tr '\0' 'D' > "$DICT_FILE"

render_conf() {
    sed \
        -e 's|__LOAD_MODULES__|load_module modules/ngx_http_zstd_filter_module.so;|' \
        -e "s|__EXTRA_DIRECTIVES__|zstd_dict_file ${DICT_FILE};|" \
        -e 's|__EXTRA_LOCATIONS__||' \
        -e 's|__SERVER_PORT__|8080|' \
        /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf

    if ! nginx -V 2>&1 | grep -q -- '--with-compat'; then
        sed -i '/^load_module /d' /etc/nginx/nginx.conf
    fi
    sed -i 's|^daemon off;|daemon on;|' /etc/nginx/nginx.conf
}

start_local_nginx() {
    nginx -c /etc/nginx/nginx.conf -t >/tmp/nginx-t.log 2>&1 || {
        echo "nginx -t failed:" >&2
        cat /tmp/nginx-t.log >&2
        return 1
    }
    nginx -c /etc/nginx/nginx.conf
    local i=0
    while ! curl -fsS --max-time 1 http://127.0.0.1:8080/ >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "nginx did not start within 3s" >&2
            return 1
        fi
        sleep 0.1
    done
}

render_conf
start_local_nginx

PASS=0
FAIL=0

# /proc/<pid>/status VmRSS in KiB. Sample master only (worker churn is noise).
master_rss_kib() {
    awk '/^VmRSS:/ {print $2}' "/proc/${1}/status" 2>/dev/null || echo 0
}

MASTER_PID="$(cat /tmp/nginx.pid 2>/dev/null || true)"
if [ -z "$MASTER_PID" ]; then
    _log_fail "${LABEL}/master-pid" "could not read /tmp/nginx.pid"
    exit 1
fi

do_reloads() {
    local n="$1"
    local i
    for i in $(seq 1 "$n"); do
        if ! nginx -c /etc/nginx/nginx.conf -s reload >/tmp/reload.err 2>&1; then
            echo "nginx -s reload #${i} failed" >&2
            cat /tmp/reload.err >&2
            return 1
        fi
        # Master forks new workers and shuts down old ones on each reload; give
        # the new master a moment to finish parse+CDict-create+old-config-destroy.
        sleep 0.05
    done
    # Let any pending old-cycle teardown settle before measuring.
    sleep 1
}

# Warm-up batch: nginx core / glibc heap stabilises after the first few
# reloads (allocator caches grow until they plateau). Sample RSS *after* the
# warm-up, then run another batch the same size and measure the delta of
# stable-state-to-stable-state. With the cleanup fix the second batch grows
# by near-zero; with the leak the CDict + libzstd-internal tables keep piling
# up proportional to reload count (≈ tens of KiB per cycle).
RELOADS=50
do_reloads "$RELOADS" || { _log_fail "${LABEL}/warmup-reloads" "warm-up batch failed"; exit 1; }
RSS_BASELINE="$(master_rss_kib "$MASTER_PID")"

do_reloads "$RELOADS" || { _log_fail "${LABEL}/measure-reloads" "measure batch failed"; exit 1; }
RSS_AFTER="$(master_rss_kib "$MASTER_PID")"
DELTA_KIB=$((RSS_AFTER - RSS_BASELINE))

# 512 KiB threshold over 50 stable-state reloads. Buggy code would compound
# at ≈ tens of KiB per reload (CDict + libzstd internal tables); fixed code
# stays near zero. Generous absolute ceiling absorbs glibc-allocator and
# nginx-core noise under qemu/Rosetta emulation.
THRESHOLD_KIB=512
if [ "$DELTA_KIB" -le "$THRESHOLD_KIB" ]; then
    _log_pass "${LABEL}/no-rss-growth (baseline=${RSS_BASELINE}K after=${RSS_AFTER}K delta=${DELTA_KIB}K over ${RELOADS} post-warmup reloads)"
    PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/no-rss-growth" \
        "RSS grew ${DELTA_KIB} KiB over ${RELOADS} post-warmup reloads (must be <=${THRESHOLD_KIB} KiB)"
    FAIL=$((FAIL + 1))
fi

# Sanity check: nginx still serves the baseline after the reload storm. The
# CDict cleanup path runs at pool-destroy; a buggy implementation that
# double-frees would have crashed the master long before we got here.
if curl -fsS --max-time 2 http://127.0.0.1:8080/ >/dev/null 2>&1; then
    _log_pass "${LABEL}/still-serving"; PASS=$((PASS + 1))
else
    _log_fail "${LABEL}/still-serving" "baseline request failed after ${RELOADS} reloads"
    FAIL=$((FAIL + 1))
fi

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
