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
#
# Gate policy (codex4 P1.3b):
#   Blocking (contributes to overall_rc): clang-tidy, cppcheck, gcc-fanalyzer, flawfinder
#   Advisory  (informational only):       scan-build (46 findings backlog, triage tracked separately)
#
# The advisory carve-out exists because scan-build's --status-bugs flag returns
# non-zero on any finding and we currently carry a 46-bug backlog. Flipping
# scan-build to blocking will follow once that backlog is triaged.
#
# Blocking-surface caveats (narrow on purpose; widen as findings get triaged):
#   * clang-tidy: only the analyzer/security/bug checks below trigger the gate
#     via -warnings-as-errors. The full `--checks=clang-analyzer-*,cert-*,
#     bugprone-*` set keeps running and prints findings — but only the
#     enumerated subset is BLOCKING. Otherwise the existing ~14k bugprone-*
#     style warnings would red-flag every run.
#   * cppcheck: --error-exitcode=2 only fires on severity=error. The
#     warning/performance/portability/style findings in the enabled set are
#     printed and reviewed but do NOT trip the gate today.
#   * gcc-fanalyzer: runs `make` against the WHOLE nginx tree (our module + the
#     entire nginx-core source set), so the analyzer fires on nginx-core paths
#     we don't own. The blocking surface is two-layered to keep the gate honest:
#       (i)  -Werror=analyzer-null-dereference + -Werror=analyzer-use-after-free
#            promote the two most severe classes to build-failures regardless
#            of which file they fire in (belt-and-suspenders).
#       (ii) After the build, a post-process greps gcc-fanalyzer.log for
#            -Wanalyzer-* findings whose path matches OUR module sources
#            (filter/ or static/, i.e. /work/module/) and trips overall_rc on
#            any match — regardless of class (fd-leak, null-argument,
#            uninitialized-value, etc.).
#     Other -Wanalyzer-* classes that fire on nginx-core paths remain advisory:
#     printed in the log, surfaced via `print_summary`, NOT blocking.

set -uo pipefail

# Aggregate exit code across all analyzer invocations in a single run. Only
# blocking analyzers contribute; scan-build's rc is logged but never OR'd in.
overall_rc=0

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
    local rc
    cd /work/nginx
    make clean 2>/dev/null || true
    # Capture configure's exit code via PIPESTATUS — we still pipe to `tail -3`
    # for a terse summary, but a piped failure must not slip through silently
    # (script uses `set -uo pipefail`, NOT `-e`, so without an explicit rc
    # check the caller would proceed to `make`/`scan-build` against an empty
    # or stale /work/nginx/objs and produce a false-green analyzer log).
    ./configure \
      --with-cc="$cc" \
      --with-cc-opt="-Wno-error ${extra_cflags}" \
      --with-compat \
      --with-http_ssl_module \
      --add-module=/work/module \
      2>&1 | tail -3
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "configure_nginx: ./configure failed (rc=${rc}) for cc=${cc}" >&2
        return "$rc"
    fi
    return 0
}

run_scan_build() {
    echo "=== scan-build ==="
    if ! configure_nginx clang; then
        echo "blocking: configure failed for scan-build — skipping analyzer" >&2
        overall_rc=1
        return
    fi
    # alpha checkers catch bounds/cast issues the default set doesn't. We
    # intentionally skip security.insecureAPI.DeprecatedOrUnsafeBufferHandling
    # — it fires on every memcpy/memset (nginx core uses them everywhere) and
    # drowns real findings in ~hundreds of FPs.
    #
    # ADVISORY analyzer: --status-bugs makes scan-build exit non-zero on any
    # finding. We carry a 46-bug backlog so we log the rc but never OR it into
    # overall_rc. Flip to blocking once the backlog is triaged.
    local rc
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
      2>&1 | tee "$RESULTS/scan-build.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        # Distinguish two non-zero-exit shapes:
        #   (1) "bugs found" — --status-bugs returned non-zero because the
        #       analyzer ran cleanly and found N findings. Advisory: log, do
        #       NOT contribute to overall_rc (we carry a 46-bug backlog).
        #   (2) tool-execution failure — scan-build itself crashed, the
        #       analyzer wrapper died, configure broke, the binary is missing,
        #       etc. The log does NOT contain a "bugs found" / "No bugs found"
        #       marker line in this case. We MUST surface this so a silently
        #       broken advisory analyzer doesn't masquerade as "ran clean".
        local bugs
        bugs=$(grep -Eo '[0-9]+ bugs? found' "$RESULTS/scan-build.log" | tail -1 || true)
        if [ -n "$bugs" ] || grep -qE 'No bugs found' "$RESULTS/scan-build.log"; then
            echo "advisory: scan-build exited $rc${bugs:+ (${bugs})} — NOT contributing to overall_rc"
        else
            # No "bugs found" marker AND non-zero exit => analyzer did not run
            # cleanly (infrastructure failure: scan-build crashed, the analyzer
            # wrapper died, the binary is missing, etc.). This is NOT the
            # advisory "we have a 46-bug backlog" case — the analyzer never
            # produced a verdict. Surface as a hard failure: a silently
            # regressed image must not pass CI by masquerading as "ran clean".
            echo "BLOCKING: scan-build exited $rc with NO 'bugs found' marker in log — analyzer crashed or failed to launch (check $RESULTS/scan-build.log)" >&2
            overall_rc=1
        fi
    fi
}

run_clang_tidy() {
    echo "=== clang-tidy ==="
    if [ ! -f /work/nginx/compile_commands.json ]; then
        echo "compile_commands.json missing — was the image built without the bear step?" >&2
        exit 1
    fi
    local rc
    # -warnings-as-errors narrows the BLOCKING surface (see header policy
    # comment): only true analyzer findings + a couple of high-signal
    # bugprone/cert checks trip the gate. The rest of --checks runs in
    # advisory mode (printed, not blocking) so we don't drown on existing
    # bugprone-* style noise.
    clang-tidy \
      --checks='-*,clang-analyzer-*,cert-*,bugprone-*' \
      --warnings-as-errors='clang-analyzer-*,cert-err33-c,bugprone-use-after-move' \
      -p /work/nginx/compile_commands.json \
      "${MODULE_SRCS[@]}" \
      2>&1 | tee "$RESULTS/clang-tidy.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "blocking: clang-tidy exited $rc"
        overall_rc=1
    fi
}

run_cppcheck() {
    echo "=== cppcheck ==="
    local rc
    cppcheck \
      -j "$(nproc)" \
      --enable=warning,performance,portability,style \
      --inconclusive \
      --force \
      --std=c11 \
      --error-exitcode=2 \
      -I /work/nginx/src/core \
      -I /work/nginx/src/event \
      -I /work/nginx/src/http \
      -I /work/nginx/src/http/modules \
      -I /work/nginx/objs \
      --suppress=missingIncludeSystem \
      --suppress=unusedFunction \
      --template='{file}:{line}: [{severity}] {id}: {message}' \
      "${MODULE_SRCS[@]}" \
      2>&1 | tee "$RESULTS/cppcheck.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "blocking: cppcheck exited $rc"
        overall_rc=1
    fi
}

run_gcc_fanalyzer() {
    echo "=== GCC -fanalyzer ==="
    if ! configure_nginx gcc "-fanalyzer -fanalyzer-verbosity=1 -Werror=analyzer-null-dereference -Werror=analyzer-use-after-free"; then
        echo "blocking: configure failed for gcc-fanalyzer — skipping analyzer" >&2
        overall_rc=1
        return
    fi
    cd /work/nginx
    local rc
    make -j"$(nproc)" 2>&1 | tee "$RESULTS/gcc-fanalyzer.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "blocking: gcc -fanalyzer build exited $rc"
        overall_rc=1
    fi

    # Module-scoped post-process: gcc -fanalyzer runs against the WHOLE nginx
    # tree, so the narrow -Werror= set above only catches null-deref and
    # use-after-free anywhere (including nginx-core). Other -Wanalyzer-*
    # classes (fd-leak, null-argument, uninitialized-value, ...) emit
    # warnings that exit 0 — fine for nginx-core paths we don't own, but
    # ANY -Wanalyzer-* finding in OUR module sources should block the gate.
    #
    # Match on /work/module/ (in-container module path) so we don't false-
    # positive on nginx-core paths that happen to contain "filter" or
    # "static" (e.g. ngx_http_*_filter_module.c, ngx_http_static_module.c).
    local module_findings
    module_findings=$(grep -E 'warning:.*-Wanalyzer-' "$RESULTS/gcc-fanalyzer.log" 2>/dev/null \
        | grep -F '/work/module/' || true)
    if [ -n "$module_findings" ]; then
        echo "blocking: gcc -fanalyzer reported -Wanalyzer-* finding(s) in module sources:" >&2
        printf '%s\n' "$module_findings" >&2
        overall_rc=1
    fi
}

run_flawfinder() {
    echo "=== flawfinder ==="
    # flawfinder --error-level=N makes it return non-zero when any finding at
    # severity >= N is reported. Level 4 = the documented "really bad" tier
    # (strcpy, gets, etc.); level 1+ findings are still printed but only
    # level>=4 trip the gate. Adjust downward as the codebase improves.
    local rc
    flawfinder --columns --context --minlevel=1 --error-level=4 \
      "${MODULE_SRCS[@]}" \
      2>&1 | tee "$RESULTS/flawfinder.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        echo "blocking: flawfinder exited $rc"
        overall_rc=1
    fi
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

# Exit non-zero if any BLOCKING analyzer reported failure. scan-build is
# advisory and never affects this.
if [ "$overall_rc" -ne 0 ]; then
    echo "run-sast.sh: blocking analyzer(s) failed — overall_rc=${overall_rc}" >&2
fi
exit "$overall_rc"
