#!/bin/bash
# run-valgrind.sh — in-container Valgrind memcheck dispatcher.
#
# Self-contained version of t/valgrind.sh: wraps /usr/sbin/nginx with a
# valgrind shim, runs the pytest subset, collects per-pid valgrind logs into
# /work/valgrind-logs, greps for actionable findings, and exits nonzero on
# real leaks / errors.
#
# Designed to be invoked from `docker compose up valgrind` so it can run
# in parallel with the SAST services. The host-side t/valgrind.sh keeps
# working as an alternative single-shot driver.
#
# Volume contract (mounted from host via compose):
#   /opt/regression                                  : t/regression/  (ro)
#   /etc/nginx/templates/nginx.conf.template         : t/docker/nginx.conf.template  (ro)
#   /work/valgrind-logs                              : tmp/valgrind-logs/  (rw)

set -uo pipefail

LOG_DIR=/work/valgrind-logs
mkdir -p "${LOG_DIR}/raw"

# Replace nginx binary with a valgrind-wrapped shim so every `nginx ...` call
# (configure-test + run) goes through memcheck. --trace-children=yes is kept
# for any hand-spawned children (e.g. python fixtures); master_process off is
# enabled via ZSTD_REGRESSION_NO_DAEMON below.
if [ ! -f /usr/sbin/nginx.real ]; then
    mv /usr/sbin/nginx /usr/sbin/nginx.real
    cat > /usr/sbin/nginx <<'EOF'
#!/bin/sh
exec valgrind \
    --tool=memcheck \
    --leak-check=full \
    --show-leak-kinds=definite,indirect \
    --track-origins=yes \
    --trace-children=yes \
    --error-exitcode=99 \
    --log-file=/tmp/valgrind.%p.log \
    --suppressions=/etc/valgrind.supp \
    /usr/sbin/nginx.real "$@"
EOF
    chmod +x /usr/sbin/nginx
fi

overall_rc=0

echo "==> valgrind :: pytest (test_accept_encoding.py)"
pytest_log="${LOG_DIR}/pytest.log"
junit_xml="${LOG_DIR}/pytest-junit.xml"
if ZSTD_REGRESSION_NO_DAEMON=1 \
   bash -c "cd /opt/regression && python3 -m pytest test_accept_encoding.py -v --tb=short --color=no -p no:cacheprovider --junitxml=${junit_xml} 2>&1" \
        > "$pytest_log" 2>&1; then
    echo "  pass  pytest (log: tmp/valgrind-logs/pytest.log)"
else
    rc=$?
    echo "  fail  pytest rc=${rc} (log: tmp/valgrind-logs/pytest.log)"
    overall_rc=1
fi

# Copy per-pid valgrind logs into the host-mounted log dir.
for f in /tmp/valgrind.*.log; do
    [ -f "$f" ] || continue
    cp "$f" "${LOG_DIR}/raw/$(basename "$f")"
done

# Findings:
#   * "definitely lost: [1-9]" → real leak
#   * "ERROR SUMMARY: N errors from M contexts (suppressed: K)" with
#     N - K > 0  → real memcheck error (Invalid read/write, uninit, etc.)
# Fully-suppressed summaries (N == K, e.g. "1 errors ... (suppressed: 1)")
# are noise we accept. Indirectly-lost reports without the above are
# typically chained off a suppressed nginx-core init pool.
#
# Classification lives in t/check-valgrind-log.sh so the gate semantics are
# unit-tested via t/test-gate-semantics.sh and shared with the host-side
# driver (t/valgrind.sh).
echo
echo "=== valgrind findings ==="
found_real=0
# Gate helper is baked into the image at a stable path by Dockerfile.valgrind.
# If it's missing the image build was incomplete — fail loud, don't silently
# fall back to an inline detector that can drift from the canonical helper.
GATE=/usr/local/bin/check-valgrind-log.sh
if [ ! -x "$GATE" ]; then
    echo "FATAL: gate helper missing at $GATE — rebuild the valgrind image" >&2
    exit 2
fi

# Fail closed if zero per-PID logs were collected — that means valgrind never
# actually ran (pytest skipped, nginx wrapper broken, log copy failed, etc.).
# Without this guard the loop below sees no files and reports
# "no leaks beyond suppressions", silently turning the gate green.
shopt -s nullglob
_vg_logs=("${LOG_DIR}/raw"/*.log)
shopt -u nullglob
if [ "${#_vg_logs[@]}" -eq 0 ]; then
    echo "ERROR: valgrind gate found zero per-PID logs in ${LOG_DIR}/raw/ — Memcheck never ran" >&2
    exit 1
fi

for f in "${LOG_DIR}/raw"/*.log; do
    [ -f "$f" ] || continue
    if ! gate_out="$("$GATE" "$f" 2>&1)"; then
        echo "--- $(basename "$f") ---"
        printf '%s\n' "$gate_out"
        grep -E 'definitely lost:|indirectly lost:|possibly lost:|still reachable:|ERROR SUMMARY:|Invalid (read|write)|Conditional jump|Use of uninitialised' "$f" | head -12
        found_real=1
    fi
done

if [ "$found_real" -eq 0 ]; then
    echo "  no leaks beyond suppressions"
else
    overall_rc=1
fi

echo
echo "=== valgrind summary ==="
echo "  pytest target: test_accept_encoding.py"
echo "  log dir:       ${LOG_DIR}/"

exit "$overall_rc"
