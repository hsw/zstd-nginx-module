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

echo "=== debian/rules two-module configure contract ==="

if [ ! -f "$RULES" ]; then
    echo "  FAIL  debian/rules missing at $RULES" >&2
    fail_count=$((fail_count + 1))
fi

# Exactly ONE --add-dynamic-module= invocation, pointing at the repo root
# ($(CURDIR)). The top-level config sources both filter/config and
# static/config, so a single configure invocation produces BOTH .so
# artefacts — that single root add is the packaging contract (per-subdir
# adds work too since the configs detect the add layout, but each would
# need its own configure+make pass for no benefit in the deb build).
# Mirrors t/docker/build-module.sh:84 (the proven test path).
assert_grep_present "$RULES" \
    '--add-dynamic-module=\$\(CURDIR\)[[:space:]]*$' \
    "debian/rules configure includes --add-dynamic-module=\$(CURDIR) (single flag, repo root)"

# Guard the single-root-add packaging contract: no --add-dynamic-module
# pointing at a /filter or /static subdir.
assert_grep_absent() {
    local file="$1"
    local pattern="$2"
    local label="$3"
    if [ ! -f "$file" ]; then
        printf '  FAIL  %s — file missing: %s\n' "$label" "$file"
        fail_count=$((fail_count + 1))
        return
    fi
    if grep -qE -e "$pattern" "$file"; then
        printf '  FAIL  %s — pattern unexpectedly present: %s\n' "$label" "$pattern"
        fail_count=$((fail_count + 1))
    else
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    fi
}
# Match only configure-line invocations (no leading `#` comment). Tab or
# leading whitespace + --add-dynamic-module= pointing at /filter or /static
# subdir, or referencing the old MODULE_FILTER/MODULE_STATIC Make variables.
assert_grep_absent "$RULES" \
    '^[[:space:]]+--add-dynamic-module=.*(MODULE_FILTER|MODULE_STATIC|/filter|/static)' \
    "debian/rules does NOT pass per-subdir --add-dynamic-module (single root add is the packaging contract)"

# Build-Depends must include libzstd-dev — the filter module links libzstd.
# debhelper-compat is the dh sequencer version pin. libzstd-dev is OUR
# addition over pkg-oss default.
assert_grep_present "$CONTROL_IN" \
    'Build-Depends:.*libzstd-dev' \
    "debian/control.in Build-Depends contains libzstd-dev"

# Build-Depends must NOT reference nginx-dev. Ubuntu's nginx-dev depends on
# Ubuntu's distro nginx (~1.24), which conflicts with the nginx.org mainline
# `nginx` package the CI build job installs. Instead, the build pulls the
# matching nginx source tarball into /usr/local/src/nginx/ and compiles
# against that — so nginx-dev is neither needed nor desired here. This
# assertion guards against a future re-scaffold from pkg-oss accidentally
# re-introducing the nginx-dev build-dep.
assert_grep_absent "$CONTROL_IN" \
    'Build-Depends:.*nginx-dev' \
    "debian/control.in Build-Depends does NOT reference nginx-dev"

# nginx configure script needs PCRE, OpenSSL, and zlib headers even when
# compiling --with-compat dynamic modules. Without these the dh_auto_configure
# step in debian/rules fails before we get anywhere near our module sources.
assert_grep_present "$CONTROL_IN" \
    'Build-Depends:.*libpcre2-dev' \
    "debian/control.in Build-Depends contains libpcre2-dev (nginx configure)"
assert_grep_present "$CONTROL_IN" \
    'Build-Depends:.*libssl-dev' \
    "debian/control.in Build-Depends contains libssl-dev (nginx configure)"
assert_grep_present "$CONTROL_IN" \
    'Build-Depends:.*zlib1g-dev' \
    "debian/control.in Build-Depends contains zlib1g-dev (nginx configure)"

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
echo "=== module enable maintainer scripts ==="

# nginx.org-native enable convention: these .debs target nginx.org mainline,
# whose stock nginx.conf sources only mime.types + conf.d/*.conf (never
# modules-enabled), so a modules-enabled symlink would silently load nothing.
# Instead, mirroring nginx.org's own Makefile.module-brotli MODULE_POST, each
# postinst prints a banner on `configure` instructing the operator to add the
# module's `load_module modules/...so;` line to /etc/nginx/nginx.conf and
# reload. No postrm is needed — there is no symlink to tear down.
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
    "filter package ships postinst (prints load_module enable banner)"
assert_file_exists "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    "static package ships postinst (prints load_module enable banner)"

# postinst MUST print the exact load_module line the operator needs to add to
# nginx.conf. Catches regressions where someone rewrites the script and drops
# or mistypes the banner instruction.
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    'load_module modules/ngx_http_zstd_filter_module\.so' \
    "filter postinst banner names load_module ngx_http_zstd_filter_module.so"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    'load_module modules/ngx_http_zstd_static_module\.so' \
    "static postinst banner names load_module ngx_http_zstd_static_module.so"

# Negative regression guard: the postinst must not reference the
# modules-enabled symlink mechanism at all (neither create nor clean up) — it
# is print-only. Forbid both `modules-enabled` and `ln -s` so a future pkg-oss
# re-scaffold can't silently bring back either the `ln -s .../modules-enabled`
# creation dance or any modules-enabled filesystem op (dead on nginx.org
# mainline, whose nginx.conf never sources modules-enabled).
assert_grep_absent "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    'modules-enabled|ln -s' \
    "filter postinst does NOT reference the modules-enabled symlink mechanism"
assert_grep_absent "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    'modules-enabled|ln -s' \
    "static postinst does NOT reference the modules-enabled symlink mechanism"

# Each postinst MUST keep the `configure` guard and the `#DEBHELPER#` token.
# The guard is the standard Debian maintainer-script arm (mirrors nginx.org's
# own nginx-module.postinst.in); #DEBHELPER# is where dh injects trigger code.
# A re-scaffold dropping either would silently change install behaviour, so
# assert both statically.
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    '"\$1" != "configure"' \
    "filter postinst keeps the configure guard"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    '"\$1" != "configure"' \
    "static postinst keeps the configure guard"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    '#DEBHELPER#' \
    "filter postinst keeps the #DEBHELPER# token"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    '#DEBHELPER#' \
    "static postinst keeps the #DEBHELPER# token"

# The old symlink-teardown *.postrm scripts were removed (no symlink to tear
# down). Guard against a pkg-oss re-scaffold reintroducing them.
assert_file_absent() {
    local file="$1"
    local label="$2"
    if [ ! -e "$file" ]; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — unexpectedly present: %s\n' "$label" "$file"
        fail_count=$((fail_count + 1))
    fi
}
assert_file_absent "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postrm" \
    "filter package ships NO postrm (no symlink to tear down)"
assert_file_absent "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postrm" \
    "static package ships NO postrm (no symlink to tear down)"

# Belt-and-suspenders on top of the static banner greps above: a static grep
# cannot catch an EMISSION regression — an inverted guard
# (`if [ "$1" = "configure" ]; then exit 0; fi`) or a heredoc-terminator typo
# keeps the load_module line in the FILE while the operator sees nothing on
# install. So EXECUTE each postinst in a throwaway sandbox and assert what
# actually reaches stdout: the banner on `configure`, and NOTHING on a
# non-configure arg. (The literal `#DEBHELPER#` line is a `#`-comment under
# `sh`, so running the script directly is harmless.)
# NOTE: the postinst is pure print-only — it performs NO filesystem writes on
# any argument (it only `cat`s a banner heredoc to stdout on `configure`). So
# executing it for real in the throwaway sandbox has no filesystem side effect
# at all; sandbox execution is fully safe.
#
# Hardening: an infrastructure failure (mktemp -d / cd / sh not running) must be
# a hard FAIL, never masquerade as a pass — otherwise the SILENT helper in
# particular goes false-green (a skipped run produces empty output). Each helper
# below verifies mktemp succeeded and yielded a real dir, that cd into it
# succeeded, and that the script actually executed (rc captured) before judging
# the captured stdout.
assert_postinst_emits_on_configure() {
    local script="$1"
    local pattern="$2"
    local label="$3"
    if [ ! -f "$script" ]; then
        printf '  FAIL  %s — file missing: %s\n' "$label" "$script"
        fail_count=$((fail_count + 1))
        return
    fi
    local tmpdir out rc
    tmpdir="$(mktemp -d)"
    if [ $? -ne 0 ] || [ -z "$tmpdir" ] || [ ! -d "$tmpdir" ]; then
        printf '  FAIL  %s — could not create sandbox tmpdir (mktemp -d failed)\n' "$label"
        fail_count=$((fail_count + 1))
        rm -rf "$tmpdir" 2>/dev/null
        return
    fi
    if ! cd "$tmpdir"; then
        printf '  FAIL  %s — could not cd into sandbox tmpdir: %s\n' "$label" "$tmpdir"
        fail_count=$((fail_count + 1))
        rm -rf "$tmpdir"
        return
    fi
    out="$(sh "$script" configure 2>/dev/null)"
    rc=$?
    cd "$REPO_ROOT" || true
    rm -rf "$tmpdir"
    if [ "$rc" -ne 0 ]; then
        printf '  FAIL  %s — configure run exited non-zero (rc=%d)\n' "$label" "$rc"
        fail_count=$((fail_count + 1))
        return
    fi
    if printf '%s\n' "$out" | grep -qE -e "$pattern"; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — configure run did not emit: %s\n' "$label" "$pattern"
        fail_count=$((fail_count + 1))
    fi
}
assert_postinst_silent_on_nonconfigure() {
    local script="$1"
    local label="$2"
    if [ ! -f "$script" ]; then
        printf '  FAIL  %s — file missing: %s\n' "$label" "$script"
        fail_count=$((fail_count + 1))
        return
    fi
    local tmpdir out rc
    tmpdir="$(mktemp -d)"
    if [ $? -ne 0 ] || [ -z "$tmpdir" ] || [ ! -d "$tmpdir" ]; then
        printf '  FAIL  %s — could not create sandbox tmpdir (mktemp -d failed)\n' "$label"
        fail_count=$((fail_count + 1))
        rm -rf "$tmpdir" 2>/dev/null
        return
    fi
    if ! cd "$tmpdir"; then
        printf '  FAIL  %s — could not cd into sandbox tmpdir: %s\n' "$label" "$tmpdir"
        fail_count=$((fail_count + 1))
        rm -rf "$tmpdir"
        return
    fi
    out="$(sh "$script" abort-upgrade 2>/dev/null)"
    rc=$?
    cd "$REPO_ROOT" || true
    rm -rf "$tmpdir"
    # Emptiness only counts as SILENT if the run actually reached and executed
    # the script with rc=0 — a skipped/failed run must not read as pass.
    if [ "$rc" -ne 0 ]; then
        printf '  FAIL  %s — non-configure run exited non-zero (rc=%d)\n' "$label" "$rc"
        fail_count=$((fail_count + 1))
        return
    fi
    if [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
        printf '  PASS  %s\n' "$label"
        pass_count=$((pass_count + 1))
    else
        printf '  FAIL  %s — non-configure run unexpectedly printed output: %s\n' "$label" "$out"
        fail_count=$((fail_count + 1))
    fi
}
assert_postinst_emits_on_configure "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    'load_module modules/ngx_http_zstd_filter_module\.so' \
    "filter postinst EMITS the load_module banner when run \`configure\`"
assert_postinst_emits_on_configure "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    'load_module modules/ngx_http_zstd_static_module\.so' \
    "static postinst EMITS the load_module banner when run \`configure\`"
assert_postinst_silent_on_nonconfigure "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.postinst" \
    "filter postinst is SILENT when run with a non-configure arg"
assert_postinst_silent_on_nonconfigure "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.postinst" \
    "static postinst is SILENT when run with a non-configure arg"

# .install files MUST reference the renamed mod-http-zstd-*.conf source path
# (Ubuntu convention) — not the old libnginx-mod-* package-named variant.
# This ships a copy-paste reference .conf carrying the load_module line to
# /usr/share/nginx/modules-available/, so an operator can copy it verbatim
# rather than retype the line the postinst banner prints.
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-filter.install" \
    '^debian/mod-http-zstd-filter\.conf usr/share/nginx/modules-available/' \
    "filter .install ships mod-http-zstd-filter.conf reference conf"
assert_grep_present "${DEBIAN_DIR}/libnginx-mod-http-zstd-static.install" \
    '^debian/mod-http-zstd-static\.conf usr/share/nginx/modules-available/' \
    "static .install ships mod-http-zstd-static.conf reference conf"

echo
echo "=== summary ==="
printf '  passed: %d\n' "$pass_count"
printf '  failed: %d\n' "$fail_count"

if [ "$fail_count" -gt 0 ]; then
    exit 1
fi

exit 0
