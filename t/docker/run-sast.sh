#!/bin/bash
# run-sast.sh — in-container SAST dispatcher for zstd-nginx-module.
#
# Runs one of:
#   scan-build | clang-tidy | cppcheck | gcc-fanalyzer | flawfinder | all | summary
#
# Each tool writes its raw output to /work/sast-results/<tool>.log (mounted
# from the host as tmp/sast-results/). The host driver `t/sast.sh` is the
# convenient entry point; this script is only invoked inside the SAST image.
#
# Adapted from nginx-ssl-fingerprint's run-sast.sh:
#   * Module targets are the two zstd .c files (filter + static).
#   * NGINX_SRCS is the subset of nginx-core sources whose code paths the zstd
#     filter actually touches (palloc, string, buf, output filter chain). We
#     exclude OpenSSL, http_v2, stream, etc. — they are irrelevant here.

set -uo pipefail

TOOL="${1:-all}"
RESULTS=/work/sast-results
mkdir -p "$RESULTS"

MODULE_SRCS=(
  /work/module/filter/ngx_http_zstd_filter_module.c
  /work/module/static/ngx_http_zstd_static_module.c
)

# Include paths required to parse module .c files outside of compile_commands.
# These cover the nginx headers our modules transitively pull in.
NGINX_INCS=(
  -I/work/nginx/src/core
  -I/work/nginx/src/event
  -I/work/nginx/src/event/modules
  -I/work/nginx/src/os/unix
  -I/work/nginx/src/http
  -I/work/nginx/src/http/modules
  -I/work/nginx/objs
)

# Nginx-core .c files that the zstd filter interacts with (pool, string utils,
# output chain, buffer mgmt). Keeping this list small intentionally — adding
# every src/core/*.c file pushes scan time past 10min without finding new
# defects in our module code.
NGINX_SRCS=(
  /work/nginx/src/core/ngx_palloc.c
  /work/nginx/src/core/ngx_string.c
  /work/nginx/src/core/ngx_buf.c
  /work/nginx/src/core/ngx_output_chain.c
  /work/nginx/src/http/ngx_http_request.c
  /work/nginx/src/http/modules/ngx_http_gzip_filter_module.c
)

configure_nginx() {
    local cc="$1"
    local extra_cflags="${2:-}"
    cd /work/nginx
    make clean 2>/dev/null || true
    ./configure \
      --with-cc="$cc" \
      --with-cc-opt="-Wno-error ${extra_cflags}" \
      --with-compat \
      --with-http_ssl_module \
      --add-module=/work/module \
      2>&1 | tail -3
}

run_scan_build() {
    echo "=== scan-build ==="
    configure_nginx clang
    # alpha checkers catch bounds/cast issues the default set doesn't. We
    # intentionally skip security.insecureAPI.DeprecatedOrUnsafeBufferHandling
    # — it fires on every memcpy/memset (nginx core uses them everywhere) and
    # drowns real findings in ~hundreds of FPs.
    CCC_CC=clang CCC_CXX=clang++ \
    scan-build \
      -o "$RESULTS/scan-build" \
      --status-bugs \
      -enable-checker alpha.security.ArrayBoundV2 \
      -enable-checker alpha.security.ReturnPtrRange \
      -enable-checker alpha.unix.cstring.OutOfBounds \
      -enable-checker alpha.core.CastSize \
      -enable-checker alpha.core.SizeofPtr \
      -enable-checker alpha.deadcode.UnreachableCode \
      make -j"$(nproc)" \
      2>&1 | tee "$RESULTS/scan-build.log" || true
}

run_clang_tidy() {
    echo "=== clang-tidy ==="
    if [ ! -f /work/nginx/compile_commands.json ]; then
        echo "compile_commands.json missing — was the image built without the bear step?" >&2
        exit 1
    fi
    clang-tidy \
      --checks='-*,clang-analyzer-*,cert-*,bugprone-*' \
      -p /work/nginx/compile_commands.json \
      "${MODULE_SRCS[@]}" \
      2>&1 | tee "$RESULTS/clang-tidy.log" || true
}

run_cppcheck() {
    echo "=== cppcheck ==="
    cppcheck \
      -j "$(nproc)" \
      --enable=warning,performance,portability,style \
      --inconclusive \
      --force \
      --std=c11 \
      -I /work/nginx/src/core \
      -I /work/nginx/src/event \
      -I /work/nginx/src/http \
      -I /work/nginx/src/http/modules \
      -I /work/nginx/objs \
      --suppress=missingIncludeSystem \
      --suppress=unusedFunction \
      --template='{file}:{line}: [{severity}] {id}: {message}' \
      "${MODULE_SRCS[@]}" \
      2>&1 | tee "$RESULTS/cppcheck.log" || true
}

run_gcc_fanalyzer() {
    echo "=== GCC -fanalyzer ==="
    configure_nginx gcc "-fanalyzer -fanalyzer-verbosity=1"
    cd /work/nginx
    make -j"$(nproc)" 2>&1 | tee "$RESULTS/gcc-fanalyzer.log" || true
}

run_flawfinder() {
    echo "=== flawfinder ==="
    flawfinder --columns --context --minlevel=1 \
      "${MODULE_SRCS[@]}" \
      2>&1 | tee "$RESULTS/flawfinder.log" || true
}

print_summary() {
    echo "========== scan-build =========="
    grep -v '^scan-build: ' "$RESULTS/scan-build.log" 2>/dev/null | grep -v '^$' | head -100
    echo
    echo "========== clang-tidy =========="
    grep -v '^$' "$RESULTS/clang-tidy.log" 2>/dev/null | head -200
    echo
    echo "========== cppcheck =========="
    grep -v '^$' "$RESULTS/cppcheck.log" 2>/dev/null \
      | grep -v 'checkersReport\|Active checkers' | head -100
    echo
    echo "========== GCC -fanalyzer =========="
    grep -E 'warning:.*-Wanalyzer' "$RESULTS/gcc-fanalyzer.log" 2>/dev/null \
      | grep -iE 'module/filter|module/static' \
      | head -80
    echo
    echo "========== flawfinder =========="
    head -200 "$RESULTS/flawfinder.log" 2>/dev/null
}

case "$TOOL" in
    scan-build)    run_scan_build ;;
    clang-tidy)    run_clang_tidy ;;
    cppcheck)      run_cppcheck ;;
    gcc-fanalyzer) run_gcc_fanalyzer ;;
    flawfinder)    run_flawfinder ;;
    all)
        run_scan_build
        run_clang_tidy
        run_cppcheck
        run_gcc_fanalyzer
        run_flawfinder
        print_summary
        ;;
    summary)       print_summary ;;
    *)
        echo "Unknown tool: $TOOL" >&2
        echo "Usage: $0 {scan-build|clang-tidy|cppcheck|gcc-fanalyzer|flawfinder|all|summary}" >&2
        exit 2
        ;;
esac
