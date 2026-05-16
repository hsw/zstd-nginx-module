"""Pytest port of t/regression/config-validation.sh.

Edge-case coverage for zstd directive parsing — `nginx -t` is enough; no
running nginx required. Each case renders a config with one or two extra
directives and asserts that nginx parses (or rejects) it.

Cases mirror the .sh original 1:1 plus the two added during the coverage
extension push (negative-malformed and duplicate-directive).
"""

import pytest

from conftest import nginx_t


# (label, directives, should_pass).
# should_pass=True  → nginx -t must exit 0.
# should_pass=False → nginx -t must exit non-zero.
CASES = [
    # comp_level: bounds-checked at config time by ngx_http_zstd_comp_level().
    ("comp-level-zero-rejected",          "zstd_comp_level 0;",          False),
    ("comp-level-too-high-rejected",      "zstd_comp_level 999999;",     False),
    ("comp-level-invalid-rejected",       "zstd_comp_level nope;",       False),
    # Negative malformed: trips the negative-branch "invalid number" return
    # (filter line ~1017) — not reachable from the plain "nope" case which
    # goes through the positive-branch return at line ~1024.
    ("comp-level-neg-malformed-rejected", "zstd_comp_level -abc;",       False),
    # Duplicate directive: setter's *np != NGX_CONF_UNSET guard (line ~1004-1006).
    ("comp-level-duplicate-rejected",     "zstd_comp_level 3; zstd_comp_level 5;", False),
    # zstd_dict_file: rejects missing files at config time.
    ("dict-missing-rejected", "zstd_dict_file /var/fixtures/config-validation/missing.dict;", False),
    # zstd_min_length: must reject non-size input.
    ("min-length-invalid-rejected",       "zstd_min_length not-a-size;", False),
]


@pytest.mark.parametrize(
    "label,directives,should_pass",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_directive_parse(label, directives, should_pass):
    rc, output = nginx_t(extra_directives=directives)
    if should_pass:
        assert rc == 0, (
            f"[{label}] nginx -t failed but should have passed:\n{output}"
        )
    else:
        assert rc != 0, (
            f"[{label}] nginx -t passed but should have failed:\n{output}"
        )


def test_dict_readable_accepted(valid_dict):
    """Separate from CASES because it depends on the valid_dict fixture
    (path baked at fixture-setup time)."""
    rc, output = nginx_t(extra_directives=f"zstd_dict_file {valid_dict};")
    assert rc == 0, f"nginx -t failed for readable dict at {valid_dict}:\n{output}"


def test_negative_comp_level_accepted(libzstd_supports_negative_levels):
    """Skipped on older libzstd (< 1.5) where ZSTD_minCLevel() is 1, so the
    bounds check at config time rejects negative levels regardless of parser."""
    if not libzstd_supports_negative_levels:
        pytest.skip("libzstd too old for stable negative-level coverage")
    rc, output = nginx_t(extra_directives="zstd_comp_level -1;")
    assert rc == 0, f"nginx -t rejected zstd_comp_level -1 on libzstd 1.5+:\n{output}"
