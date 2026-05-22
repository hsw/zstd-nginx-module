#!/bin/bash
# test-debian-rules.sh — static assertions on the committed debian/rules
# and debian/control.in that guard the two-module packaging contract.
#
# Mirrors t/test-gate-semantics.sh: pass/fail counter, prints summary,
# exits 0 (all green) / 1 (any failure). No Docker, no network — runs in
# milliseconds against the committed files. Catches regressions where
# someone re-scaffolds debian/rules from pkg-oss and drops the
# two-module configure customisation or the libzstd-dev build-dep.
#
# Run:   bash t/test-debian-rules.sh
# Exit:  0 = all assertions hold; 1 = any assertion failed.

# `-e` deliberately omitted — assertion helpers below capture grep
# non-matches as a fail counter increment rather than aborting the run, so
# all assertions report. Matches the convention used by
# t/test-gate-semantics.sh and the other harness-style self-tests under t/.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RULES="${REPO_ROOT}/debian/rules"
CONTROL_IN="${REPO_ROOT}/debian/control.in"
DEBIAN_DIR="${REPO_ROOT}/debian"

fail_count=0
pass_count=0

assert_grep_present() {
    local file="$1"
    local pattern="$2"
    local label="$3"
    if [ ! -f "$file" ]; then
        printf '  FAIL  %s — file missing: %s\n' "$label" "$file"
        fail_count=$((fail_count + 1))
        return
    fi
    # -E for extended regex, no -i: case-sensitive on purpose so we
    # don't false-match e.g. `zstd_inc` (a config-time directive) against
    # the `ZSTD_INC` env override assertion.
    if grep -qE -e "$pattern" "$file"; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — pattern not found: %s\n' "$label" "$pattern"
        fail_count=$((fail_count + 1))
    fi
}

# Same as assert_grep_present but joins shell line-continuations (`\<NL>`)
# before matching. Needed for postinst/postrm scripts where the `ln -s`
# invocation is wrapped across two lines for readability — grep is
# line-oriented and would otherwise miss the joined pattern.
assert_grep_present_joined() {
    local file="$1"
    local pattern="$2"
    local label="$3"
    if [ ! -f "$file" ]; then
        printf '  FAIL  %s — file missing: %s\n' "$label" "$file"
        fail_count=$((fail_count + 1))
        return
    fi
    # awk joins every `\\\n` continuation into a single line, then we grep
    # the flattened stream. Using awk (not sed) for portability — BSD sed
    # on macOS rejects the multi-line `:a;N;$!ba;s/...` idiom that GNU sed
    # accepts. macOS dev parity matters since contributors run host-side
    # tests outside Docker.
    if awk '/\\$/ { sub(/\\$/, ""); printf "%s", $0; next } { print }' "$file" \
       | grep -qE -e "$pattern"; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — pattern not found (after joining \\-NL): %s\n' "$label" "$pattern"
        fail_count=$((fail_count + 1))
    fi
}

echo "=== debian/rules two-module configure contract ==="

if [ ! -f "$RULES" ]; then
    echo "  FAIL  debian/rules missing at $RULES" >&2
    fail_count=$((fail_count + 1))
fi

# Both --add-dynamic-module= invocations must be present, one for each
# module subdir. pkg-oss build_module.sh emits a single --add-dynamic-module
# for one module per source tree; we ship two, so the configure step in
# debian/rules MUST be customised.
assert_grep_present "$RULES" \
    '--add-dynamic-module=.*(MODULE_FILTER|filter)' \
    "debian/rules configure includes --add-dynamic-module=...filter"

assert_grep_present "$RULES" \
    '--add-dynamic-module=.*(MODULE_STATIC|static)' \
    "debian/rules configure includes --add-dynamic-module=...static"

# Build-Depends must include libzstd-dev — the filter module links libzstd.
# nginx-dev provides the configure hooks; debhelper-compat is the dh
# sequencer version pin. libzstd-dev is OUR addition over pkg-oss default.
assert_grep_present "$CONTROL_IN" \
    'Build-Depends:.*libzstd-dev' \
    "debian/control.in Build-Depends contains libzstd-dev"

# ZSTD_INC / ZSTD_LIB env passthrough — operators must be able to override
# the libzstd include/lib paths at build time without editing debian/rules
# (e.g. to point at a vendored static libzstd.a). The rules file references
# both env vars; the exact mechanism (--with-cc-opt / --with-ld-opt
# augmentation) is implementation-detail-tolerant, but BOTH names must
# appear somewhere in the file.
assert_grep_present "$RULES" \
    'ZSTD_INC' \
    "debian/rules references ZSTD_INC env override"

assert_grep_present "$RULES" \
    'ZSTD_LIB' \
    "debian/rules references ZSTD_LIB env override"

echo
echo "=== module auto-enable maintainer scripts ==="

# Ubuntu/Debian's libnginx-mod-* convention: the .deb only ships the
# load_module snippet to /usr/share/nginx/modules-available/; postinst
# symlinks it into /etc/nginx/modules-enabled/50-<name>.conf to actually
# enable the module on first `configure`, and postrm tears down the symlink
# on remove/purge. Without these scripts a user gets installed-but-not-loaded
# modules (silent failure mode). Both packages must ship the pair.
assert_file_exists() {
    local file="$1"
    local label="$2"
    if [ -f "$file" ]; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — missing: %s\n' "$label" "$file"
        fail_count=$((fail_count + 1))
    fi
}

assert_file_exists "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    "filter package ships postinst (enables module-enabled symlink)"
assert_file_exists "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postrm" \
    "filter package ships postrm (removes module-enabled symlink)"
assert_file_exists "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    "static package ships postinst (enables module-enabled symlink)"
assert_file_exists "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postrm" \
    "static package ships postrm (removes module-enabled symlink)"

# postinst MUST symlink modules-available -> modules-enabled with 50- prefix
# (matches stock libnginx-mod-* numbering). Catches regressions where someone
# rewrites the script and drops the symlink step.
assert_grep_present_joined "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    'ln -s.*modules-available/mod-http-zstd-filter\.conf.*modules-enabled/50-mod-http-zstd-filter\.conf' \
    "filter postinst creates 50- prefixed modules-enabled symlink"
assert_grep_present_joined "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    'ln -s.*modules-available/mod-http-zstd-static\.conf.*modules-enabled/50-mod-http-zstd-static\.conf' \
    "static postinst creates 50- prefixed modules-enabled symlink"

# postrm MUST remove the symlink on remove|purge so a reinstall doesn't see
# a dangling symlink (rm -f is idempotent — safe).
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postrm" \
    'rm -f.*modules-enabled/50-mod-http-zstd-filter\.conf' \
    "filter postrm removes modules-enabled symlink"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postrm" \
    'rm -f.*modules-enabled/50-mod-http-zstd-static\.conf' \
    "static postrm removes modules-enabled symlink"

# .install files MUST reference the renamed mod-http-zstd-*.conf source path
# (Ubuntu convention) — not the old libnginx-mod-* package-named variant.
# The postinst symlink target is mod-http-zstd-*.conf, so the .install line
# determines what actually lands at /usr/share/nginx/modules-available/.
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.install" \
    '^debian/mod-http-zstd-filter\.conf usr/share/nginx/modules-available/' \
    "filter .install ships mod-http-zstd-filter.conf (matches postinst symlink target)"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.install" \
    '^debian/mod-http-zstd-static\.conf usr/share/nginx/modules-available/' \
    "static .install ships mod-http-zstd-static.conf (matches postinst symlink target)"

echo
echo "=== summary ==="
printf '  passed: %d\n' "$pass_count"
printf '  failed: %d\n' "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi

exit 0
