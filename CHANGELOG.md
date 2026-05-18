# Changelog

All notable changes to this fork of zstd-nginx-module are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This fork diverges from upstream [tokers/zstd-nginx-module](https://github.com/tokers/zstd-nginx-module) at commit `057a7d3`.

## [0.3.0] - 2026-05-18

### Changed

- Migrated the streaming compression path from the legacy three-function
  API (`ZSTD_compressStream` + `ZSTD_flushStream` + `ZSTD_endStream`
  dispatched via internal action state machine) to the unified
  `ZSTD_compressStream2(cctx, &out, &in, op)` call. The `ZSTD_EndDirective`
  is now derived per-call from the current input buf's `last_buf` /
  `flush` flags, modelled on `ngx_brotli`'s per-call-op pattern.
  Functionally identical to the legacy API on single-thread builds
  (`nbWorkers=0`); compressed-byte output may differ by one byte at the
  frame-header `windowSize` field but decoded payload is byte-identical.
  The `ngx_http_zstd_filter_action_e` enum and `ctx->action` /
  `ctx->redo` state-machine fields are gone; their job is now done by
  per-call op derivation plus sticky `ctx->last` / `ctx->flush` flags
  cleared on libzstd-drained (`rc == 0`).
- Modernized the `ZSTD_CCtx` setup path. Dropped the legacy
  `ZSTD_initCStream` / `ZSTD_initCStream_usingCDict` branches under
  `#if ZSTD_VERSION_NUMBER < 10500`. The post-allocation init sequence
  now uniformly uses `ZSTD_CCtx_reset(ZSTD_reset_session_only)` followed
  by either `ZSTD_CCtx_refCDict` (when `zstd_dict_file` is configured)
  or `ZSTD_CCtx_setParameter(ZSTD_c_compressionLevel, ...)`. This
  eliminates the `-Wdeprecated-declarations` warning emitted by libzstd
  headers that mark `ZSTD_initCStream_usingCDict` deprecated.

### Fixed

- Production worker freeze when `zstd on;` is enabled on a `proxy_pass`
  to a streaming upstream emitting small frames with `b->flush=1`
  (Varnish chunked transfers, Server-Sent Events, `Connection: Upgrade`
  framings). Reported on the upstream `tokers/zstd-nginx-module#23`
  thread by mklooss (2025-03-20, Varnish chunked), Stensel8 (2026-01-11,
  HomeAssistant WebSocket), and lowkeypriority (2026-02-02). Root cause:
  the V1 action state machine only promoted `COMPRESS -> FLUSH` when
  libzstd reported pending output (`rc > 0`). For small flush'd chunks
  where libzstd swallowed input without emitting (`rc == 0`), no
  `ZSTD_flushStream()` was issued -- bytes stayed buffered until the
  upstream window closed. The per-call-op refactor closes this class of
  bug end-to-end because flush propagation no longer depends on
  libzstd's internal accumulation state. A defense-in-depth
  zero-progress guard (`buffer_in.pos` unchanged + `buffer_out.pos`
  unchanged + `rc == 0` + `op == ZSTD_e_continue` -> `NGX_ERROR`)
  modelled on `ngx_brotli` is included to catch any future regression
  that would loop without making progress.

### Testing

- Added `chunked-off-tiny` sub-test to `t/regression/test_proxy_flush.py`
  reproducing the production freeze pattern (50 chunks x 200 bytes, 50ms
  gap, `proxy_buffering off`, TTFB budget 400ms). The sub-test was
  landed as `pytest.mark.xfail(strict=True)` in the test-first commit
  and unmarked in the bug-fix commit, so the fix is validated end-to-end
  against the production-shape reproducer. On `stable` baseline the case
  hangs past 10s `ReadTimeoutError`; post-fix it completes within the
  TTFB budget.

### Performance

- Early-release CStream workspace via bump allocator (Direction A).
  `ngx_http_zstd_filter_create_cstream` now performs a single
  `ngx_palloc(r->pool, ZSTD_estimateCStreamSize(level) + headroom)`
  and serves libzstd's `customAlloc` callback from that chunk via a
  bump pointer — mirroring the nginx gzip filter's allocator pattern
  (`ngx_http_gzip_filter_module.c:615, 893`). After
  `ZSTD_freeCStream` returns in the `ctx->done` branch the workspace
  is released eagerly via `ngx_pfree(r->pool, ctx->preallocated)`;
  since the workspace exceeds glibc's mmap threshold (~128 KiB) the
  free returns the pages to the kernel via `munmap` immediately
  rather than waiting on `r->pool` teardown. A pool cleanup handler
  registered alongside the CStream is the abort-path safety net:
  if the request finalizes before `ctx->done` flips (client RST,
  upstream error, finalize-from-another-module), the handler runs
  `ZSTD_freeCStream` on the still-alive CStream, mirroring the
  CDict cleanup pattern from `3a2c597`. Measured impact on the
  slow-client window: ΔRSS 4148 → 416 KiB and ΔVSZ 5396 → 0 KiB
  (≈10× reduction in workspace memory held after compression
  completes). Commits: `7da62a9` (memory-observation regression
  test), `2410bba` (implementation).

### Build

- Minimum libzstd raised to 1.4.0. `ZSTD_compressStream2` requires
  v1.4.0+ (released Aug 2019). An explicit compile-time guard now lives
  at the top of `filter/ngx_http_zstd_filter_module.c`:
  `#error "libzstd 1.4.0 or later required for ZSTD_compressStream2"`
  when `ZSTD_VERSION_NUMBER < 10400`. Ubuntu 20.04+ and any current LTS
  Linux distro is unaffected; CentOS 7 and Ubuntu 18.04 system libzstd
  packages no longer suffice (build against a bundled libzstd 1.4+ if
  those targets remain required). The static module
  (`static/ngx_http_zstd_static_module.c`) does not link libzstd and is
  unaffected.

## [Pre-0.3.0 stable]

The `stable` branch carried no `CHANGELOG.md` prior to the 0.3.0 entry above.
For history before 0.3.0 see the git log on the `stable` branch — notable
fixes already shipped there include:

- HTTP/2 silent truncation at the 131072-byte boundary (cherry-pick of upstream PR #49).
- Infinite loop on abnormal upstream `Content-Length` (cherry-pick of upstream PR #23).
- RFC 9110-compliant Accept-Encoding parser (rejects `zstdx`, honours `q=0`).
- CDict cleanup handler registered to prevent a per-reload leak when `zstd_dict_file` is set.
- Build glue prefers shared `libzstd` for dynamic-module compatibility.
- HTTP/2 / docker-based pytest regression harness (`t/regression/`, `t/build.sh`, `t/run.sh`)
  with ASan/UBSan (`t/asan.sh`), Valgrind (`t/valgrind.sh`), and SAST (`t/sast.sh`) variants.

The hypothetical `0.2.0` tag described an earlier branch that diverged from `stable`
and shipped additional directives (`zstd_max_length`, `zstd_bypass`, `zstd_window_bits`);
those directives are NOT present on `stable` / this 0.3.0 release.

[0.3.0]: https://github.com/shomenkow/zstd-nginx-module/releases/tag/0.3.0
