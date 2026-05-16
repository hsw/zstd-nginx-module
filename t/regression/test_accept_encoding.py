"""Pytest port of t/regression/accept-encoding.sh — POC for the regression
suite migration. Same five cases as the .sh original (Task 5 RFC 9110 parser),
plus the wildcard case.

The .sh version uses curl -sSI (HEAD) against /text — 180-byte deterministic
text/plain body, large enough to compress, small enough that timing isn't a
concern. We use requests.head() for the same effect.
"""

import pytest

from conftest import http_request

URL_PATH = "/text"

# (label, accept_encoding_value, expect_zstd_response).
CASES = [
    # q=0 means "do NOT accept" per RFC 9110 §12.5.3 — must not be served zstd.
    ("q=0",            "zstd;q=0",          False),
    # Multiple tokens — must pick up the zstd token regardless of position.
    ("multi-token",    "gzip, zstd",        True),
    # Substring match guard — "zstdx" is a different token, not zstd.
    ("false-prefix",   "zstdx",             False),
    # Case-insensitive match — uppercase is a valid HTTP encoding name.
    ("case-insens",    "ZSTD",              True),
    # q-value preference — explicit q=1 for zstd beats q=0.5 for br.
    ("q-wins",         "br;q=0.5, zstd;q=1", True),
    # Wildcard MUST NOT trigger zstd — parser is token-explicit, mirrors gzip.
    ("wildcard-only",  "*",                 False),
]


@pytest.mark.parametrize(
    "label,accept_encoding,expect_zstd",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_accept_encoding(nginx, label, accept_encoding, expect_zstd):
    """Send HEAD with the given Accept-Encoding; assert Content-Encoding
    matches (or doesn't match) "zstd"."""
    r, _ = http_request(nginx, URL_PATH, method="HEAD", accept_encoding=accept_encoding)
    ce = r.headers.get("Content-Encoding", "")
    if expect_zstd:
        assert ce == "zstd", (
            f"[{label}] AE={accept_encoding!r}: expected Content-Encoding=zstd, "
            f"got {ce!r}; all headers: {dict(r.headers)}"
        )
    else:
        assert ce != "zstd", (
            f"[{label}] AE={accept_encoding!r}: must NOT serve zstd, "
            f"got Content-Encoding={ce!r}; all headers: {dict(r.headers)}"
        )
