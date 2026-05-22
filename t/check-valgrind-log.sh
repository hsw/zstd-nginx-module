#!/bin/sh
# check-valgrind-log.sh — classify a single valgrind --log-file output.
#
# Exit 0   : log is clean (no actionable errors / leaks). Fully-suppressed
#            "ERROR SUMMARY: N (suppressed: N)" lines and "definitely lost: 0
#            bytes" entries are considered clean.
# Exit 1   : log contains real (un-suppressed) errors or a non-zero
#            "definitely lost:" byte count. Findings are printed to stdout.
# Exit 2   : usage / file not readable.
#
# Used by t/valgrind.sh (host-side aggregator) and t/docker/run-valgrind.sh
# (in-container runner) so the gate semantics live in exactly one place and
# are exercised by t/test-gate-semantics.sh.
#
# Why awk and not grep:
#   - The legacy `grep -E 'ERROR SUMMARY: [1-9]'` matches ANY non-zero error
#     count, including fully-suppressed lines like
#     "ERROR SUMMARY: 1 errors from 1 contexts (suppressed: 1 from 1)".
#     The result is the gate red-flagging known/whitelisted noise.
#   - The detector below subtracts the `(suppressed: N)` count from the total
#     and only flags when (total - suppressed) > 0.

set -u

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <valgrind-log-file>" >&2
    exit 2
fi

f="$1"

if [ ! -r "$f" ]; then
    echo "$0: cannot read $f" >&2
    exit 2
fi

# - ERROR SUMMARY line: capture total and suppressed count.
# - definitely lost: NN bytes where NN > 0 — flag as a real leak.
#   NN may be comma-separated (valgrind prints "1,024 bytes" by default).
# - No ERROR SUMMARY line at all => the run did not complete (valgrind always
#   emits exactly one ERROR SUMMARY line when it terminates cleanly, even on
#   a zero-error run). This subsumes the earlier marker-allowlist approach:
#   any allowlist of "interesting" error tokens drifts as new categories
#   appear (Conditional jump / Use of uninitialised etc.), but the SUMMARY
#   line is canonical and stable. Missing-SUMMARY => truncated or killed.
findings=$(awk '
    # Match the canonical valgrind summary line. Tolerate the leading
    # "==PID==" prefix that valgrind prints with --log-file by anchoring
    # the field extraction off the "ERROR SUMMARY:" token rather than
    # raw field positions ($3 differs depending on whether the prefix is
    # present — capture by regex sub() to be prefix-agnostic).
    /ERROR SUMMARY: [0-9]+ errors? from [0-9]+ contexts? \(suppressed: [0-9]+/ {
        saw_summary = 1
        total_line = $0
        sub(/.*ERROR SUMMARY: /, "", total_line)
        total = total_line + 0
        supp_line = $0
        sub(/.*suppressed: /, "", supp_line)
        suppressed = supp_line + 0
        actionable = total - suppressed
        if (actionable > 0) {
            print "actionable:" actionable " errors in " FILENAME
        }
    }
    /definitely lost: [1-9][0-9,]* bytes/ {
        print "leak:" $0 " in " FILENAME
    }
    END {
        if (!saw_summary) {
            print "truncated:no ERROR SUMMARY in " FILENAME " (log truncated or valgrind killed)"
        }
    }
' "$f")

if [ -n "$findings" ]; then
    printf '%s\n' "$findings"
    exit 1
fi

exit 0
