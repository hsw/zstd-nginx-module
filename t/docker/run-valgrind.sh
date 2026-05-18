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
#   * "ERROR SUMMARY: [1-9]"   → memcheck error (Invalid read/write, uninit, etc.)
# Indirectly-lost reports without these are typically chained off a suppressed
# nginx-core init pool, so we don't flag them on their own.
echo
echo "=== valgrind findings ==="
found_real=0
for f in "${LOG_DIR}/raw"/*.log; do
    [ -f "$f" ] || continue
    if grep -E 'definitely lost: [1-9]' "$f" >/dev/null 2>&1 \
       || grep -E 'ERROR SUMMARY: [1-9]' "$f" >/dev/null 2>&1; then
        echo "--- $(basename "$f") ---"
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
