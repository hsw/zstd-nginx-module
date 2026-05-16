#!/bin/bash
# compress-state-machine.sh — exercise edge cases of the zstd body filter's
# action state machine (ctx->action transitions in ngx_http_zstd_filter_compress).
#
# Why this file exists separately from h2-truncation.sh / infinite-loop.sh:
# those tests pin specific bugs (PR #49 / PR #23). This one is a structural
# regression suite for the state machine itself — every sub-test maps to a
# concrete code path inside ngx_http_zstd_filter_compress:
#
#   1. empty-body        — last_buf=1 with no payload → sentinel-buf path
#                          (filter/ngx_http_zstd_filter_module.c sentinel buf
#                          on ngx_buf_size(out_buf)==0).
#   2. single-byte       — minimal COMPRESS→END cycle.
#   3. h1-131072         — HTTP/1.1 counterpart to h2-truncation.sh boundary.
#                          Same code path on filter side; framing on response
#                          side is chunked rather than DATA frames. Documents
#                          that the bug (or its absence) is filter-side, not
#                          transport-side.
#   4. h1-200000         — multi-redo COMPRESS→FLUSH cycling at scale.
#   5. keep-alive-3      — 3 sequential compressed responses on the SAME TCP
#                          connection (Connection: keep-alive). The filter
#                          context is per-request, but the worker's CCtx
#                          lifecycle and any leaked state would surface here.
#   6. parallel-burst    — 10 concurrent connections, mixed body sizes.
#                          Exercises re-entrancy across requests in the same
#                          worker.
#
# **Coverage scope**: structural regression tests — byte-equality of
# decompressed output, valid framing, no state leak across requests. All
# sub-tests PASS on master baseline (`test1` = master + step1 test suite).
# That's expected: the master compress state machine handles static-file
# COMPRESS→END and the in-memory chain shapes these tests produce. The bugs
# in step1's cherry-pick set (PR #49 truncation at 131072 over H2,
# PR #23 / 0.2.1 flush-promotion) need transport / upstream patterns these
# tests do not produce — H2 DATA frame strict accounting, slow chunked
# upstream. Those bugs are surfaced by sibling scripts h2-truncation.sh and
# proxy-flush.sh.
#
# Value of this file: regression net for any future refactor of the action
# state machine — specifically the V2 sequencing
# (compressStream2 migration → Option γ retire-action-machine → Direction B
# pool). Each sub-test pins a specific code path in compress(); any
# refactor that breaks one will fail this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/_common.sh"

LABEL="compress-state-machine"
TMPDIR="$(mktemp -d)"
PASS=0
FAIL=0

cleanup() {
    stop_local_nginx
    rm -rf "$TMPDIR"
}
trap 'cleanup' EXIT

render_conf() {
    # Extra locations exercising specific state machine entry points.
    local extra_locs
    extra_locs="$(cat <<'NGX'
        # Empty 200 — last_buf=1 fires with zero-byte buffer_in. Forces the
        # sentinel-buf allocation path (out_buf size == 0 after endStream).
        location = /empty {
            add_header Content-Type text/plain;
            return 200 "";
        }

        # Single-byte body — minimal viable input to a streaming compressor.
        location = /single {
            add_header Content-Type text/plain;
            return 200 "a";
        }

        # Equivalence test — proxy_pass back to this same nginx instance
        # serving the static file. Strip Accept-Encoding from the upstream
        # request so the inner location returns RAW bytes; the outer body
        # filter then compresses fresh. Without this header strip, the inner
        # location would compress and the outer filter would double-compress
        # (Content-Encoding chain only carries the outer value).
        location = /equiv-proxy/ {
            proxy_pass http://127.0.0.1:8080/random/equiv.css;
            proxy_http_version 1.1;
            proxy_set_header Accept-Encoding "";
        }
NGX
)"

    render_nginx_template \
        'load_module modules/ngx_http_zstd_filter_module.so;' \
        '' \
        "$extra_locs" \
        8080 \
        /etc/nginx/nginx.conf

    if ! nginx -V 2>&1 | grep -q -- '--with-compat'; then
        sed -i '/^load_module /d' /etc/nginx/nginx.conf
    fi
    apply_daemon_mode /etc/nginx/nginx.conf
}

# Render boundary-size fixture bodies once. /var/fixtures/random is the same
# path used by h2-truncation.sh; reuse files if present.
prepare_fixtures() {
    mkdir -p /var/fixtures/random
    local n
    for n in 131072 200000; do
        if [ ! -f "/var/fixtures/random/${n}" ]; then
            head -c "$n" /dev/urandom > "/var/fixtures/random/${n}"
        fi
    done
}

# fetch_decompress <label> <url> [curl-extra-args...]
# Curls the URL with Accept-Encoding: zstd, decompresses with `zstd -d`, fails
# the sub-test if the decoded body does not byte-match the expected fixture or
# expected literal.
fetch_decompress_expect_file() {
    local label="$1" url="$2" expect_file="$3"
    shift 3
    local out="${TMPDIR}/${label}.body"
    local dec="${TMPDIR}/${label}.dec"
    local hdr="${TMPDIR}/${label}.hdr"

    if ! curl -sS -D "$hdr" "$@" -H "Accept-Encoding: zstd" \
            --max-time 15 "$url" -o "$out"; then
        _log_fail "${LABEL}/${label}" "curl failed"
        FAIL=$((FAIL + 1))
        return 1
    fi

    local ce
    ce="$(header_value "$(cat "$hdr")" Content-Encoding)"
    if [ "$ce" != "zstd" ]; then
        _log_fail "${LABEL}/${label}" \
            "Content-Encoding=[${ce}] (expected zstd) — body filter did not run"
        FAIL=$((FAIL + 1))
        return 1
    fi

    if ! zstd -dc -- "$out" > "$dec" 2>/tmp/zstd-err; then
        _log_fail "${LABEL}/${label}" "zstd -d failed: $(cat /tmp/zstd-err)"
        FAIL=$((FAIL + 1))
        return 1
    fi

    if ! cmp -s "$expect_file" "$dec"; then
        _log_fail "${LABEL}/${label}" \
            "decoded differs (orig=$(wc -c < "$expect_file") dec=$(wc -c < "$dec"))"
        FAIL=$((FAIL + 1))
        return 1
    fi

    _log_pass "${LABEL}/${label}"
    PASS=$((PASS + 1))
}

# Variant that takes an inline expected string rather than a file.
fetch_decompress_expect_literal() {
    local label="$1" url="$2" expect_literal="$3"
    shift 3
    local expect_file="${TMPDIR}/${label}.expect"
    printf '%s' "$expect_literal" > "$expect_file"
    fetch_decompress_expect_file "$label" "$url" "$expect_file" "$@"
}

prepare_fixtures
render_conf
start_local_nginx_bg /etc/nginx/nginx.conf

# 1. Empty 200 — last_buf=1 on zero-byte buffer_in. The compress filter must
# emit a valid zstd frame containing zero decompressed bytes. Touches the
# sentinel-buf path: out_buf size is 0 after ZSTD_endStream produces the empty
# frame's epilogue (frame header + zero blocks + checksum is ~13 bytes, which
# fits in the first cycle; subsequent flush emits zero bytes).
fetch_decompress_expect_literal empty "http://127.0.0.1:8080/empty" "" || true

# 2. Single byte — minimal viable streaming input. Verifies the COMPRESS→END
# transition fires on the first compress call (rc=0 from compressStream with
# all input consumed → restore-to-COMPRESS, then last_buf path emits).
fetch_decompress_expect_literal single "http://127.0.0.1:8080/single" "a" || true

# 3. HTTP/1.1 counterpart to h2-truncation.sh — same bug class, different
# transport framing. Documents that the 131072-byte truncation is filter-side.
fetch_decompress_expect_file h1-131072 \
    "http://127.0.0.1:8080/random/131072" \
    /var/fixtures/random/131072 \
    --http1.1 || true

# 4. Larger body forces multiple COMPRESS→FLUSH redo cycles on a single
# request. With Content-Length unknown post-compression, response uses
# Transfer-Encoding: chunked back to client.
fetch_decompress_expect_file h1-200000 \
    "http://127.0.0.1:8080/random/200000" \
    /var/fixtures/random/200000 \
    --http1.1 || true

# 5. Keep-alive: 3 sequential compressed responses on the same TCP connection.
# Each request gets a fresh ctx and a fresh ZSTD_CStream — there should be no
# state leak between them. curl reuses the connection when given multiple
# URLs in one invocation; --write-out '%{num_connects}' reports how many
# *new* TCP connections were opened across the run. Expected value: 1
# (single keep-alive connection across all three requests).
#
# Note: curl's -D writes ALL responses' headers into one file; per-request
# Content-Encoding check is not possible with one invocation. We trust that
# successful `zstd -d` on each body implies the response was valid zstd.
keep_alive_three() {
    local label="${LABEL}/keep-alive-3"
    local num_connects
    # -w '%{num_connects}\n' prints once per transfer; we take the last one.
    num_connects="$(curl -sS --http1.1 -H "Accept-Encoding: zstd" \
        --max-time 15 -w '%{num_connects}\n' \
        -o "${TMPDIR}/ka1.body" \
            "http://127.0.0.1:8080/random/131072" \
        -o "${TMPDIR}/ka2.body" \
            "http://127.0.0.1:8080/random/200000" \
        -o "${TMPDIR}/ka3.body" \
            "http://127.0.0.1:8080/text" 2>/dev/null | tail -1)"
    if [ -z "$num_connects" ]; then
        _log_fail "$label" "curl failed (no num_connects)"
        return 1
    fi
    local i
    for i in 1 2 3; do
        if ! zstd -dc -- "${TMPDIR}/ka${i}.body" > "${TMPDIR}/ka${i}.dec" \
                2>/tmp/zstd-err; then
            _log_fail "$label" \
                "request ${i}: zstd -d failed: $(cat /tmp/zstd-err)"
            return 1
        fi
    done
    if ! cmp -s /var/fixtures/random/131072 "${TMPDIR}/ka1.dec"; then
        _log_fail "$label" "request 1 body mismatch"
        return 1
    fi
    if ! cmp -s /var/fixtures/random/200000 "${TMPDIR}/ka2.dec"; then
        _log_fail "$label" "request 2 body mismatch"
        return 1
    fi
    # Keep-alive sanity: 1 TCP connection should have served all 3 requests.
    # If the worker crashed or reset between requests, num_connects >= 2.
    if [ "$num_connects" -gt 1 ]; then
        _log_fail "$label" \
            "num_connects=${num_connects} (expected 1 — keep-alive lost)"
        return 1
    fi
    _log_pass "${label} (num_connects=${num_connects})"
    return 0
}
if keep_alive_three; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

# 6. Parallel burst: 10 concurrent connections, mixed sizes. Each connection
# gets a new ctx in the worker; this exercises re-entrancy of the state
# machine init / teardown across requests. Implementation note: previous
# attempts used `xargs -I% bash -c '...' _ %`, but xargs' `-I` replacement
# also rewrites every literal `%` it finds in the script body — including
# `$((i % 4))` inside the body, which arithmetic-errored to "i 1 4: syntax
# error". Bash `&` + `wait` is shorter and avoids any quoting collision.
parallel_burst() {
    local label="${LABEL}/parallel-burst"
    local burst_dir="${TMPDIR}/burst"
    mkdir -p "$burst_dir"
    local urls=(
        http://127.0.0.1:8080/random/131072
        http://127.0.0.1:8080/random/200000
        http://127.0.0.1:8080/text
        http://127.0.0.1:8080/single
    )
    local i url out rc
    for i in 1 2 3 4 5 6 7 8 9 10; do
        url="${urls[$((i % 4))]}"
        out="${burst_dir}/${i}.body"
        rc="${burst_dir}/${i}.rc"
        (
            if ! curl -sS --http1.1 -H "Accept-Encoding: zstd" \
                    --max-time 30 -o "$out" "$url" 2>/dev/null; then
                echo "curl-fail $url" > "$rc"
                exit 1
            fi
            if ! zstd -dc -- "$out" >/dev/null 2>&1; then
                echo "decode-fail $url" > "$rc"
                exit 1
            fi
            echo "ok $url" > "$rc"
        ) &
    done
    wait
    local fails
    fails="$(grep -h '^\(decode-fail\|curl-fail\)' "${burst_dir}"/*.rc 2>/dev/null | sort -u | head -5 || true)"
    if [ -n "$fails" ]; then
        _log_fail "$label" "$fails"
        return 1
    fi
    _log_pass "$label"
    return 0
}
if parallel_burst; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

# 7. Disk vs proxy equivalence — same payload served two ways must produce
# identical decompressed bodies. Path 1: static file via `alias` (in_file
# buffer → body_filter pulls, no b->flush, COMPRESS→END only). Path 2:
# proxy_pass to a local nginx-served file (in-memory chain links from
# upstream connection, potentially with b->flush=1 if the upstream's chunks
# don't coalesce in proxy_buffering). The compression filter sees these as
# structurally different chain shapes; the decompressed output must be
# byte-identical regardless. Documents the equivalence the V2 compressStream2
# migration must preserve.
disk_vs_proxy_equiv() {
    local label="${LABEL}/disk-vs-proxy-equiv"
    local src="/var/fixtures/random/equiv.css"
    # Repetitive CSS-like payload — well within zstd's coalesce window, so
    # the upstream side is likely to send it as one or two chain links.
    # ~20 KiB: bigger than proxy_buffer_size default (4-8 KiB) so the proxy
    # path can fragment if proxy_buffering is off.
    if [ ! -f "$src" ]; then
        {
            for _ in $(seq 1 400); do
                printf '.btn-%d { color: #%06x; padding: 4px 8px; margin: 2px; }\n' \
                    $((RANDOM % 1000)) $((RANDOM * RANDOM % 0x1000000))
            done
        } > "$src"
    fi
    local disk_dec="${TMPDIR}/equiv-disk.dec"
    local proxy_dec="${TMPDIR}/equiv-proxy.dec"
    local hdr1="${TMPDIR}/equiv-disk.hdr"
    local hdr2="${TMPDIR}/equiv-proxy.hdr"
    local body1="${TMPDIR}/equiv-disk.body"
    local body2="${TMPDIR}/equiv-proxy.body"

    # Path 1: disk via /random/ alias (in_file buffer).
    if ! curl -sS --http1.1 -H "Accept-Encoding: zstd" --max-time 15 \
            -o "$body1" -D "$hdr1" \
            "http://127.0.0.1:8080/random/equiv.css"; then
        _log_fail "$label" "disk-path curl failed"
        return 1
    fi
    # Path 2: proxy_pass to /equiv-origin/ which itself serves from /random/.
    if ! curl -sS --http1.1 -H "Accept-Encoding: zstd" --max-time 15 \
            -o "$body2" -D "$hdr2" \
            "http://127.0.0.1:8080/equiv-proxy/"; then
        _log_fail "$label" "proxy-path curl failed"
        return 1
    fi
    local ce1 ce2
    ce1="$(header_value "$(cat "$hdr1")" Content-Encoding)"
    ce2="$(header_value "$(cat "$hdr2")" Content-Encoding)"
    if [ "$ce1" != "zstd" ] || [ "$ce2" != "zstd" ]; then
        _log_fail "$label" "Content-Encoding: disk=[${ce1}] proxy=[${ce2}]"
        return 1
    fi
    if ! zstd -dc -- "$body1" > "$disk_dec" 2>/tmp/zstd-err; then
        _log_fail "$label" "disk-path zstd -d: $(cat /tmp/zstd-err)"
        return 1
    fi
    if ! zstd -dc -- "$body2" > "$proxy_dec" 2>/tmp/zstd-err; then
        _log_fail "$label" "proxy-path zstd -d: $(cat /tmp/zstd-err)"
        return 1
    fi
    if ! cmp -s "$src" "$disk_dec"; then
        _log_fail "$label" "disk decoded != source ($(wc -c < "$src") vs $(wc -c < "$disk_dec"))"
        return 1
    fi
    if ! cmp -s "$src" "$proxy_dec"; then
        _log_fail "$label" "proxy decoded != source ($(wc -c < "$src") vs $(wc -c < "$proxy_dec"))"
        return 1
    fi
    if ! cmp -s "$disk_dec" "$proxy_dec"; then
        _log_fail "$label" "disk decoded != proxy decoded (compression-path divergence)"
        return 1
    fi
    _log_pass "$label"
    return 0
}
if disk_vs_proxy_equiv; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1))
fi

echo "${LABEL}: pass=${PASS} fail=${FAIL}"
[ "$FAIL" -eq 0 ]
