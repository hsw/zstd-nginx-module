"""Pytest port of t/regression/accept-encoding.sh — POC for the regression
suite migration. Same five cases as the .sh original (Task 5 RFC 9110 parser),
plus the wildcard case.

The .sh version uses curl -sSI (HEAD) against /text — 180-byte deterministic
text/plain body, large enough to compress, small enough that timing isn't a
concern. We use requests.head() for the same effect.

TODO (follow-up branch `test/ae-parser-coverage`): expand coverage. The
review-phase-1 testing reviewer flagged 14 gaps; the categories to fill in
a dedicated coverage pass are:

  * q-value parser branches — only q=0 and q=1 are exercised today; the
    parser has ~12 distinct branches (q=0.x for x in 1..9, fractional
    digits 1..3, q=1.0, q=1.000, malformed q=, q=., q=2, etc.).
  * OWS variants around `;` — the exact bug fixed by commit 32e7f75
    (`zstd ;q=0`, `zstd; q=0`, `zstd\\t;q=0`) has no regression test.
  * Non-q parameter branch (e.g. `zstd;foo=bar`) is entirely untested.
  * Case variation in the parameter name (`Q=`, `q=`).
  * Multi-token combinations (`zstd;q=0, zstd`, `zstd, zstd;q=0`,
    `zstdx, zstd`).
  * Boundary/truncation inputs (`zstd;`, `zstd;q`, `zstd;q=`).
  * Filter/static parity — no shared CASES table across both pytest
    files; drift risk if a fix lands in one and not the other.
  * False-prefix variants (`notzstd`, `xzstd`, `zstd-future`).

Out of scope for this iteration: log-content assertions, ZSTD_VERSION
fallback path, CDict cleanup ASan reload-loop, body-bytes roundtrip,
Vary header assertions — see review report for the full T1–T14 list.
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
