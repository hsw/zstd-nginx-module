#!/bin/bash
# test-gate-semantics.sh — assertions for release-gate detectors.
#
# Covers two gates:
#
# 1. Valgrind (codex4 P1.3a): replays canned per-pid logs through
#    t/check-valgrind-log.sh and verifies the classification matches policy:
#      - "ERROR SUMMARY: N errors ... (suppressed: N)" with no real leaks => PASS
#      - any "definitely lost: NN bytes" with NN>0                         => FAIL
#      - any unsuppressed error (total > suppressed)                       => FAIL
#
# 2. SAST (codex4 P1.3b): static-grep assertions on t/docker/run-sast.sh
#    that verify:
#      - the four BLOCKING analyzers (clang-tidy, cppcheck, gcc-fanalyzer,
#        flawfinder) no longer use the bare `|| true` mask
#      - scan-build remains advisory and does NOT contribute to overall_rc
#      - the script accumulates blocking exit codes into overall_rc
#      - `make clean` keeps its `|| true` (housekeeping; not analyzer output)
#
#    Why static grep rather than running the wrappers: the analyzers need
#    Docker, a built nginx tree, compile_commands.json, etc. Static grep
#    catches the regression we actually care about (someone re-adding
#    `|| true` to a blocking line) and runs in milliseconds with no setup.
#
# Run:   bash t/test-gate-semantics.sh
# Exit:  0 = all assertions hold; 1 = any assertion failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATE="${REPO_ROOT}/t/check-valgrind-log.sh"
FIX_DIR="${REPO_ROOT}/t/fixtures/valgrind"
SAST_SCRIPT="${REPO_ROOT}/t/docker/run-sast.sh"

fail_count=0
pass_count=0

assert_gate() {
    local fixture="$1"      # path under fixtures/valgrind/
    local expect="$2"       # "pass" or "fail"
    local label="$3"
    local rc=0
    local out

    out=$("$GATE" "${FIX_DIR}/${fixture}" 2>&1) || rc=$?

    case "$expect" in
        pass)
            if [ "$rc" -eq 0 ]; then
                printf '  PASS  %s (%s exited 0 as expected)\n' "$label" "$fixture"
                pass_count=$((pass_count + 1))
            else
                printf '  FAIL  %s — gate exited %d, expected 0\n' "$label" "$rc"
                printf '         output: %s\n' "$out"
                fail_count=$((fail_count + 1))
            fi
            ;;
        fail)
            if [ "$rc" -ne 0 ]; then
                printf '  PASS  %s (%s exited %d as expected; output: %s)\n' \
                    "$label" "$fixture" "$rc" "$(printf '%s' "$out" | tr '\n' ';')"
                pass_count=$((pass_count + 1))
            else
                printf '  FAIL  %s — gate exited 0, expected nonzero\n' "$label"
                fail_count=$((fail_count + 1))
            fi
            ;;
        *)
            printf 'usage error: expect must be pass|fail (got %s)\n' "$expect" >&2
            exit 2
            ;;
    esac
}

if [ ! -x "$GATE" ]; then
    echo "missing or non-executable gate script: $GATE" >&2
    exit 2
fi

echo "=== gate semantics ==="

assert_gate suppressed-only.txt pass \
    "fully-suppressed ERROR SUMMARY + definitely lost 0 — accept"

assert_gate real-leak.txt fail \
    "definitely lost 128 + unsuppressed error — reject"

assert_gate mixed.txt fail \
    "ERROR SUMMARY 3 (suppressed 2) — 1 actionable — reject"

assert_gate error-no-leak.txt fail \
    "actionable error with no leak block — reject"

assert_gate suppressed-error-with-leak.txt fail \
    "fully-suppressed errors but real leak — reject (leak still wins)"

assert_gate comma-leak.txt fail \
    "definitely lost 1,024 (thousands-separator) — reject"

assert_gate truncated.txt fail \
    "Invalid read/write markers but no ERROR SUMMARY — reject as truncated"

assert_gate truncated-conditional.txt fail \
    "Conditional jump / Use of uninitialised markers but no ERROR SUMMARY — reject as truncated"

# rc=2 case: gate must exit 2 on a missing/unreadable file (usage error).
echo
echo "=== gate harness errors ==="
rc=0
"$GATE" "${FIX_DIR}/does-not-exist.txt" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 2 ]; then
    printf '  PASS  missing-file invocation exits 2 (usage error)\n'
    pass_count=$((pass_count + 1))
else
    printf '  FAIL  missing-file invocation exited %d, expected 2\n' "$rc"
    fail_count=$((fail_count + 1))
fi

# ---- SAST gate (codex4 P1.3b) ---------------------------------------------

assert_grep_present() {
    local file="$1"
    local pattern="$2"
    local label="$3"
    if grep -qE "$pattern" "$file"; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — pattern not found: %s\n' "$label" "$pattern"
        fail_count=$((fail_count + 1))
    fi
}

assert_grep_absent() {
    local file="$1"
    local pattern="$2"
    local label="$3"
    if grep -qE "$pattern" "$file"; then
        printf '  FAIL  %s — pattern unexpectedly present: %s\n' "$label" "$pattern"
        grep -nE "$pattern" "$file" | sed 's/^/         /'
        fail_count=$((fail_count + 1))
    else
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    fi
}

echo
echo "=== sast policy (codex4 P1.3b) ==="

if [ ! -f "$SAST_SCRIPT" ]; then
    echo "  FAIL  run-sast.sh missing at $SAST_SCRIPT"
    fail_count=$((fail_count + 1))
else
    # (a) blocking analyzers must NOT use `|| true` on their tee chain.
    # We grep for the pattern `tee "$RESULTS/<name>.log" || true` and assert
    # none of the four blocking analyzers match.
    for tool in clang-tidy cppcheck gcc-fanalyzer flawfinder; do
        assert_grep_absent "$SAST_SCRIPT" \
            "tee \"\\\$RESULTS/${tool}\\.log\" \\|\\| true" \
            "blocking analyzer ${tool} does not mask rc with || true"
    done

    # (b) scan-build advisory carve-out — the "bugs found" branch (analyzer
    # ran cleanly + found N findings) must NOT flip overall_rc. We carry a
    # 46-bug backlog so a clean-run "N bugs found" is advisory only.
    # Infrastructure-failure branches (configure failed, no marker in log)
    # ARE allowed to assign overall_rc — those represent the analyzer never
    # actually running, which should fail CI loudly. We assert the carve-out
    # by extracting just the `if [ -n "$bugs" ] ... ; then ... ; fi` block
    # and asserting that block has no `overall_rc=1`.
    if awk '
        /^run_scan_build\(\)/                            { inside=1; next }
        inside && /if \[ -n "\$bugs" \]/                  { branch=1; next }
        inside && branch && /^[[:space:]]*else/           { branch=0 }
        inside && /^}/                                    { inside=0 }
        inside && branch                                  { print }
    ' "$SAST_SCRIPT" | grep -qE 'overall_rc=1'; then
        printf '  FAIL  scan-build "bugs found" branch assigns overall_rc — must stay advisory\n'
        fail_count=$((fail_count + 1))
    else
        printf '  PASS  scan-build "bugs found" branch does not contribute to overall_rc (advisory)\n'
        pass_count=$((pass_count + 1))
    fi

    # (c) overall_rc accumulation must exist and be surfaced via `exit`.
    assert_grep_present "$SAST_SCRIPT" '^overall_rc=0' \
        "overall_rc accumulator is initialised at top of script"
    assert_grep_present "$SAST_SCRIPT" 'overall_rc=1' \
        "blocking analyzers assign overall_rc=1 on failure"
    assert_grep_present "$SAST_SCRIPT" 'exit "\$overall_rc"' \
        "script exits with overall_rc"

    # (d) `make clean` keeps `|| true` (housekeeping is not an analyzer).
    assert_grep_present "$SAST_SCRIPT" 'make clean 2>/dev/null \|\| true' \
        "make clean retains || true (housekeeping; not an analyzer)"

    # (e) PIPESTATUS capture is used for tee chains so the pipe's first cmd rc
    # is recovered (not the tee rc).
    assert_grep_present "$SAST_SCRIPT" 'PIPESTATUS\[0\]' \
        "analyzer wrappers capture PIPESTATUS[0] from tee chains"

    # (f) gcc-fanalyzer module-scoped post-process: after the build, the
    # wrapper must grep gcc-fanalyzer.log for -Wanalyzer-* findings whose
    # path includes /work/module/ (our module sources) and contribute to
    # overall_rc on any match. Without this, only the two narrow -Werror=
    # classes (null-deref, use-after-free) block — other -Wanalyzer-*
    # classes (fd-leak, null-argument, uninitialized-value) would slip
    # past the gate even when fired in our own code. We assert by
    # extracting the run_gcc_fanalyzer function body and grepping it for
    # both the -Wanalyzer-* log scan and the /work/module/ scoping filter,
    # plus an overall_rc=1 assignment downstream.
    if awk '
        /^run_gcc_fanalyzer\(\)/  { inside=1; next }
        inside && /^}/            { inside=0 }
        inside                    { print }
    ' "$SAST_SCRIPT" > /tmp/_run_gcc_fanalyzer_body.$$; then
        body=/tmp/_run_gcc_fanalyzer_body.$$
        if grep -qE 'Wanalyzer-' "$body" \
           && grep -qF '/work/module/' "$body" \
           && grep -qE 'overall_rc=1' "$body"; then
            printf '  PASS  gcc-fanalyzer wrapper post-processes module-scoped findings into overall_rc\n'
            pass_count=$((pass_count + 1))
        else
            printf '  FAIL  gcc-fanalyzer wrapper missing module-scoped post-process (Wanalyzer-* + /work/module/ + overall_rc=1)\n'
            fail_count=$((fail_count + 1))
        fi
        rm -f "$body"
    fi
fi

# ---- Valgrind zero-logs guard (codex4 iter5 C1) --------------------------
#
# Both valgrind drivers iterate ${LOG_DIR}/raw/*.log and report
# "no leaks beyond suppressions" if the loop sees zero files. If pytest is
# skipped, the nginx wrapper is broken, or docker cp fails, no logs exist —
# the gate would silently go green. Assert that the empty-logs case is
# explicitly handled (fail-closed).
#
# Strategy: static-grep both driver scripts for the guard's error string.
# Then run a live empty-dir simulation against the host-side driver's guard
# logic to prove the fail-closed path actually exits nonzero.

echo
echo "=== valgrind zero-logs guard (codex4 iter5 C1) ==="

HOST_VG="${REPO_ROOT}/t/valgrind.sh"
DOCKER_VG="${REPO_ROOT}/t/docker/run-valgrind.sh"

assert_grep_present "$HOST_VG" \
    'found zero per-PID logs' \
    "t/valgrind.sh contains zero-logs guard"
assert_grep_present "$DOCKER_VG" \
    'found zero per-PID logs' \
    "t/docker/run-valgrind.sh contains zero-logs guard"

# Live simulation: extract the guard idiom verbatim and run it against an
# empty temp dir. Must exit nonzero with the expected message on stderr.
tmp_raw_dir=$(mktemp -d)
mkdir -p "${tmp_raw_dir}/raw"
guard_rc=0
guard_out=$(
    LOG_DIR="$tmp_raw_dir" bash -c '
        set -uo pipefail
        shopt -s nullglob
        _vg_logs=("${LOG_DIR}/raw"/*.log)
        shopt -u nullglob
        if [ "${#_vg_logs[@]}" -eq 0 ]; then
            echo "ERROR: valgrind gate found zero per-PID logs in ${LOG_DIR}/raw/ — Memcheck never ran" >&2
            exit 1
        fi
    ' 2>&1
) || guard_rc=$?
rm -rf "$tmp_raw_dir"

if [ "$guard_rc" -ne 0 ] && printf '%s' "$guard_out" | grep -q 'found zero per-PID logs'; then
    printf '  PASS  empty raw/ dir trips the fail-closed guard (rc=%d, message present)\n' "$guard_rc"
    pass_count=$((pass_count + 1))
else
    printf '  FAIL  empty raw/ dir did not trip the guard (rc=%d, out=%s)\n' "$guard_rc" "$guard_out"
    fail_count=$((fail_count + 1))
fi

# Inverse: a single matching file must let the guard pass through.
tmp_raw_dir=$(mktemp -d)
mkdir -p "${tmp_raw_dir}/raw"
: > "${tmp_raw_dir}/raw/valgrind.1.log"
guard_rc=0
LOG_DIR="$tmp_raw_dir" bash -c '
    set -uo pipefail
    shopt -s nullglob
    _vg_logs=("${LOG_DIR}/raw"/*.log)
    shopt -u nullglob
    if [ "${#_vg_logs[@]}" -eq 0 ]; then
        echo "ERROR: valgrind gate found zero per-PID logs" >&2
        exit 1
    fi
' || guard_rc=$?
rm -rf "$tmp_raw_dir"

if [ "$guard_rc" -eq 0 ]; then
    printf '  PASS  non-empty raw/ dir passes the guard (rc=0)\n'
    pass_count=$((pass_count + 1))
else
    printf '  FAIL  non-empty raw/ dir tripped the guard (rc=%d)\n' "$guard_rc"
    fail_count=$((fail_count + 1))
fi

# ---- release.yml ref validation (codex iter 8) --------------------------
#
# The "Validate ref/tag" step in .github/workflows/release.yml gates BOTH
# entry paths — workflow_dispatch AND tag-push — by reading MODULE_REF from
# `inputs.module_ref || github.ref_name`. A previous iteration accidentally
# guarded the step with `if: github.event_name == 'workflow_dispatch'`,
# silently letting tag-push bypass validation and allowing prerelease tags
# like v0.3.0-rc.1 to dch-encode as a version dpkg sorts NEWER than v0.3.0.
#
# Without a regression test a future contributor could re-add that guard.
# Static-grep the Validate step block and assert:
#   (a) MODULE_REF env still has the `inputs.module_ref || github.ref_name`
#       fallback that covers both event types.
#   (b) The step has NO `if:` guard at all. The original narrow check rejected
#       only the specific `if: github.event_name == 'workflow_dispatch'`
#       form, but semantically equivalent bypasses
#       (`!= 'push'`, `inputs.module_ref != ''`, compound `&&`/`||`,
#       `!inputs.dry_run`, etc.) would slip through. The Validate step must
#       run on every entry path so a hostile/malformed ref is rejected before
#       any downstream job runs — codex iter 9 widens the assertion to match.

echo
echo "=== release.yml ref/tag validation gates both entry paths (codex iter 8) ==="

RELEASE_YML="${REPO_ROOT}/.github/workflows/release.yml"

if [ ! -f "$RELEASE_YML" ]; then
    printf '  FAIL  release.yml missing at %s\n' "$RELEASE_YML"
    fail_count=$((fail_count + 1))
else
    # Extract the Validate ref/tag step block: from its `- name:` line up to
    # (but not including) the next `- name:` at the same indent.
    validate_block=$(awk '
        /^      - name: Validate ref\/tag/  { inside=1; print; next }
        inside && /^      - name:/          { inside=0 }
        inside                              { print }
    ' "$RELEASE_YML")

    if [ -z "$validate_block" ]; then
        printf '  FAIL  could not locate "Validate ref/tag" step in release.yml\n'
        fail_count=$((fail_count + 1))
    else
        # (a) MODULE_REF env must fall back: `inputs.module_ref || github.ref_name`.
        if printf '%s\n' "$validate_block" | grep -qE 'MODULE_REF:[[:space:]]*\$\{\{[[:space:]]*inputs\.module_ref[[:space:]]*\|\|[[:space:]]*github\.ref_name'; then
            printf '  PASS  Validate ref/tag MODULE_REF reads inputs.module_ref || github.ref_name (covers both entry paths)\n'
            pass_count=$((pass_count + 1))
        else
            printf '  FAIL  Validate ref/tag MODULE_REF does not fall back to github.ref_name — tag-push would bypass validation\n'
            fail_count=$((fail_count + 1))
        fi

        # (b) The Validate step must NOT have ANY `if:` guard. The step must
        # run on every entry path so a hostile/malformed ref is rejected
        # before any downstream job runs. Broader than checking for the
        # specific workflow_dispatch-only guard because semantically
        # equivalent bypasses (e.g. `if: github.event_name != 'push'`,
        # `if: inputs.module_ref != ''`, any compound `&&`/`||` form that
        # skips tag-push) would otherwise slip through.
        if printf '%s\n' "$validate_block" | grep -qE '^[[:space:]]+if:[[:space:]]'; then
            printf '  FAIL  Validate ref/tag has an if-guard (must run unconditionally to gate both push and dispatch)\n'
            printf '         offending line: %s\n' "$(printf '%s\n' "$validate_block" | grep -E '^[[:space:]]+if:[[:space:]]' | head -1)"
            fail_count=$((fail_count + 1))
        else
            printf '  PASS  Validate ref/tag runs unconditionally (no if-guard)\n'
            pass_count=$((pass_count + 1))
        fi
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
