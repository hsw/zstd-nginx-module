"""Codex4 P1.1: zstd_static enum table must have ngx_null_string sentinel.

Without the sentinel, `ngx_conf_set_enum_slot()` reads past the end of the
array on any value that is not one of {off, on, always} — undefined behaviour
(potential OOB read, may segv, may silently accept garbage).

LIMITATION — this test is NOT deterministic proof of the fix.
================================================================
The test brackets the correct *observable* behaviour: invalid tokens must be
rejected cleanly with `invalid value "<token>"` and a non-zero exit. It does
NOT prove the OOB itself. Without the sentinel, the OOB read is UB and could
manifest as:
  - a clean rejection (heap byte after the array happens to NOT match the
    token → test passes spuriously),
  - a segv (test fails for the right reason but unreliably),
  - silent acceptance of the bogus token (test fails with a different,
    misleading assertion error).

The REAL guard against this regression is the AddressSanitizer variant
(`ubuntu-24.04-asan`) running this same test file. ASan's redzones turn the
OOB into a deterministic abort with a stack trace. The non-ASan matrix
variants are best-effort coverage — they catch the regression most of the
time but cannot be relied on alone.

If this test ever needs to be "promoted" to deterministic proof, the
approach is to run it only on the ASan variant and skip elsewhere — NOT to
add brittle heuristics here.
"""

import subprocess

import pytest

from conftest import (
    CONF_PATH,
    _nginx_has_compat,
    render_template,
)


VALID_TOKENS = ["off", "on", "always"]


def _load_modules_str() -> str:
    """Load both filter and static modules on --with-compat builds, else
    empty (static builds already have them linked in)."""
    if _nginx_has_compat():
        return (
            "load_module modules/ngx_http_zstd_filter_module.so;\n"
            "load_module modules/ngx_http_zstd_static_module.so;"
        )
    return ""


def _nginx_t_with_static(extra_directives: str) -> tuple[int, str]:
    """Like conftest.nginx_t but ensures the static module is loaded."""
    render_template(
        extra_directives=extra_directives,
        load_modules=_load_modules_str(),
    )
    r = subprocess.run(
        ["nginx", "-c", str(CONF_PATH), "-t"],
        capture_output=True, text=True, check=False,
    )
    return r.returncode, (r.stdout + r.stderr)


@pytest.mark.parametrize("token", VALID_TOKENS, ids=VALID_TOKENS)
def test_valid_zstd_static_values(token):
    """Happy-path: each documented enum token must parse cleanly."""
    rc, output = _nginx_t_with_static(f"zstd_static {token};")
    assert rc == 0, (
        f"nginx -t rejected valid zstd_static value '{token}':\n{output}"
    )


def test_invalid_zstd_static_value_rejected():
    """Invalid token must be rejected with `invalid value "bad"` and non-zero
    exit. The sentinel is what makes this rejection well-defined — without it,
    ngx_conf_set_enum_slot reads past the array end (UB)."""
    rc, output = _nginx_t_with_static("zstd_static bad;")
    assert rc != 0, (
        f"nginx -t accepted invalid zstd_static value 'bad':\n{output}"
    )
    assert 'invalid value "bad"' in output, (
        f"expected 'invalid value \"bad\"' in nginx -t output, got:\n{output}"
    )


def test_invalid_zstd_static_other_token_rejected():
    """A different invalid token to exercise the same code path with a value
    that cannot be confused with any prefix of the valid tokens."""
    rc, output = _nginx_t_with_static("zstd_static maybe;")
    assert rc != 0, (
        f"nginx -t accepted invalid zstd_static value 'maybe':\n{output}"
    )
    assert 'invalid value "maybe"' in output, (
        f"expected 'invalid value \"maybe\"' in nginx -t output, got:\n{output}"
    )
