# Changelog

All notable changes to this fork of zstd-nginx-module are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This fork diverges from upstream [tokers/zstd-nginx-module](https://github.com/tokers/zstd-nginx-module) at commit `057a7d3`.

## [Unreleased]

### Fixed

- The Debian package now pins its nginx dependency patch-exact
  (`nginx (>= x.y.z), nginx (<< x.y.z+1)`) instead of per-minor. nginx
  refuses to load a dynamic module whose compiled-in version differs from
  the running binary, so a routine `apt upgrade` within a minor (e.g.
  1.29.5 → 1.29.6) could previously leave the module unloadable and
  hard-fail the next reload — a production outage while apt reported all
  deps satisfied. The tighter range holds nginx at the version the module
  was built against instead.
- Module packages no longer rely on the Debian `modules-enabled` symlink
  layout, which nginx.org mainline — the sole target of these `.deb`s —
  does not source from its `nginx.conf`, so the auto-created symlink
  silently loaded nothing. Each postinst now prints an nginx.org-style
  banner instructing the operator to add the `load_module modules/...so;`
  line to `/etc/nginx/nginx.conf` and reload. The now-obsolete `*.postrm`
  scripts (which only tore the symlink down) were removed.

## [0.4.0] - 2026-06-12

### Changed

- A response with a known `Content-Length: 0` is no longer compressed,
  regardless of `zstd_min_length` (including `zstd_min_length 0;`) — nothing
  is gained by compressing an empty body, and the known-CL auto-window path
  previously allocated a full baseline workspace for zero bytes (C12-2).
  Responses of *unknown* length (chunked) that turn out empty still emit a
  valid empty zstd frame by design, since `Content-Encoding` is committed at
  header time (nginx gzip parity).

### Fixed

- Filter body loop now recycles consumed input chain links via
  `ngx_free_chain` instead of dropping them, matching the nginx gzip and
  ngx_brotli siblings — long streaming responses no longer accumulate dead
  chain links in the request pool (N11-1).
- Both modules now set `h->next = NULL` on the pushed `Content-Encoding`
  header (guarded `#if nginx_version >= 1023000`, ngx_brotli parity) —
  `ngx_list_push` returns uninitialized memory and nginx >= 1.23 walks known
  headers via `ngx_table_elt_t.next` (N12-1, N15-1).
- UB-class cleanups in the filter: `%uz` for `size_t` in the debug compress
  traces (C06-1), `(ngx_int_t)` cast on `ZSTD_maxCLevel()` in the level-range
  config error (C06-2), `buffer_out.size` computed from `pos` not `start`
  (P12-1), and `$zstd_ratio` buffer sized for the 3-digit fraction (S01-1).
  Plus a micro-reorder of the `header_only` decline ahead of the content-type
  test (P02-1).
- Filter-only and static-only builds (`--add-dynamic-module=<repo>/filter` /
  `=<repo>/static`) doubled the module source path (`filter/filter/...`,
  `static/static/...`): both configs hardcoded `$ngx_addon_dir/<subdir>/`.
  The srcs assignments now detect the add layout, so direct subdir adds
  work alongside the repo root.
- `filter/config` routed `-DZSTD_STATIC_LINKING_ONLY` through
  `ngx_module_incs`, which nginx's `auto/make` rewrites token-by-token to
  `-I <token>` — the define never reached the compiler and a filter-only
  build failed at `make`. The macro is now defined (`#ifndef`-guarded) at
  the include site in the .c file, and the build system carries no `-D`
  flags at all (C11-1).
- Setting only one of `ZSTD_INC` / `ZSTD_LIB` used to expand a dangling
  `-I`/`-L` that silently swallowed the next flag (a `ZSTD_LIB`-only
  configure even passed). **Breaking:** the pair is now **both-or-none** —
  builds that previously set only one of the two variables now fail
  configure fast, with an error naming both variables (C11-2).
- `ngx_http_zstd_static_module` no longer probes for, links, or requires
  libzstd. Its config ran the full feature probe and hard-exited when
  libzstd was absent although the module uses no zstd symbol (N03-4), and
  in combined builds it inherited the filter's stale `ngx_module_libs`
  (including `-lzstd`) via cross-config variable leakage — both
  `ngx_module_incs` and `ngx_module_libs` are now explicitly empty (C11-4).
- The filter's module lib flags no longer duplicate the entire global
  `NGX_LD_OPT` (including the operator's `--with-ld-opt`) into the `.so`
  link line; `ngx_module_libs` now carries only the module's own zstd link
  flags (N03-3). New `t/test-build-isolation.sh` covers filter-only,
  static-only, combined-link hygiene, half-set env vars, and
  libzstd-absent builds.

## [0.3.0] - 2026-05-22

### Added

- GitHub Actions CI/CD: 3-variant test matrix on PR/push to `stable`;
  `.deb` packaging via committed `debian/` + `dpkg-buildpackage`; full-matrix
  release gate on `git tag v*`; `workflow_dispatch` for rebuilds against
  newer nginx mainline; dry-run mode for safe fork testing.
- `debian/prepare.sh` substitutes ABI-pin placeholders in `debian/control.in`
  per pkg-oss floor/ceiling convention (LOWER = `X.Y.0`, UPPER = `X.(Y+1).0`).
- `t/ci/install-nginx-mainline.sh` reusable nginx.org apt-repo bootstrap script.
- Self-tests: `t/test-debian-prepare.sh`, `t/test-debian-rules.sh`,
  `t/test-build-cache-env.sh`.
- `.github/dependabot.yml` enables weekly Dependabot updates for the
  `github-actions` ecosystem so action versions stay current.

### Changed

- GitHub Actions references switched from SHA pins back to version tags
  (`actions/checkout@v5`, `docker/setup-buildx-action@v4`,
  `actions/upload-artifact@v4`, `actions/download-artifact@v4`,
  `softprops/action-gh-release@v2`). Resolves the Node.js 20 deprecation
  warning on `actions/checkout@v4` + `docker/setup-buildx-action@v3` (both
  now Node 24). Dependabot keeps the tags current; supply-chain hardening
  moves from manual SHA-pinning to automated PR-based bumps.
- `ngx_http_zstd_static_module` now enables Range requests on `.zst`
  sidecars (sets `r->allow_ranges = 1` before `ngx_http_send_header`),
  matching nginx core `gzip_static`. Clients can now request byte ranges
  of precompressed `.zst` files (206 Partial Content / 416 / If-Range /
  Accept-Ranges: bytes).
- New `zstd_window_bits N;` directive (http / server / location). Sets an
  upper cap on the per-request `windowLog`. Range validated at config-parse
  time against the runtime `ZSTD_cParam_getBounds(ZSTD_c_windowLog)` (typically
  `[10, 31]` on 64-bit, `[10, 30]` on 32-bit). Default is unset (no cap).
  Recommended setting: `zstd_window_bits 23;` when serving Chrome clients
  at `zstd_comp_level >= 17`, since Chrome rejects frame `windowLog > 23`.
  See `README.md` for the memory ≈ window relationship table and migration
  notes from the 0.2.x exact-value variant of this directive (semantics
  differ — this release ships **cap semantics**, not exact value).

### Changed

- `t/build.sh` now uses `docker buildx build --load` unconditionally
  (replacing plain `docker build`); accepts `BUILDX_CACHE_FROM` /
  `BUILDX_CACHE_TO` env vars for GitHub Actions layer-cache reuse; adds
  `ZSTD_BUILD_DRYRUN=1` for dry-run testing of the resolved docker
  buildx argv without invoking docker. Empty array expansions use the
  `${arr[@]+"${arr[@]}"}` idiom for `set -u` survival.
- Per-request CStream auto-window. When the response `Content-Length` is
  known, the filter now derives `windowLog` / `hashLog` / `chainLog` per
  request via `ZSTD_getCParams(level, content_length, 0)` and sizes the
  per-request workspace via `ZSTD_estimateCStreamSize_usingCParams`. For the
  L1 production traffic profile (median body ~0.9 KiB, p70 < 16 KiB) this
  shrinks the per-request workspace from the ~5.5 MiB level=6 baseline to
  ~64–256 KiB — roughly a 20–80× reduction at the bump-allocator chunk
  level, freed eagerly via Direction A's pool-free path. Compression ratio
  is unaffected for any single response because the auto-derived window is
  always large enough to span the body. Chunked / unknown-`Content-Length`
  responses fall back to the level defaults and are byte-identical to the
  previous release — **fully backward-compatible**.
- Forward-compat guard. The auto-tune path validates that the
  per-request workspace estimate never exceeds the level-default baseline
  computed at config init (`ZSTD_estimateCStreamSize(level)`); on any
  violation the request transparently falls back to the level-default
  workspace. Defends against future libzstd heuristic regressions that
  could return larger cParams for the same level + srcSize.

### Fixed

- `ngx_http_zstd_static_module` `zstd_static` directive enum table was
  missing the `{ ngx_null_string, 0 }` terminator. nginx's
  `ngx_conf_set_enum_slot()` iterates until it sees a zero-length name; on
  an invalid token (e.g. `zstd_static bad;`) the loop ran past the array
  end into adjacent rodata, an OOB read with undefined behaviour per the
  nginx module ABI contract. nginx core `gzip_static` has the sentinel;
  we now match. The current image happens to surface a clean `invalid
  value "bad"` emerg, but the underlying read was unsafe. (codex4 P1.1.)
- `filter/config` and `static/config` explicit-path branch
  (`$ZSTD_INC` / `$ZSTD_LIB` set) previously tried `$ZSTD_LIB/libzstd.a`
  first regardless of `ngx_module_link`. For `--add-dynamic-module`
  builds against a distro libzstd this could re-introduce the
  non-PIC-archive link failure that the auto-discovery branch already
  guards against. The explicit-path branch now branches on
  `ngx_module_link = DYNAMIC` and prefers `-L$ZSTD_LIB -lzstd
  -Wl,-rpath,$ZSTD_LIB` for dynamic builds, falling back to the
  archive-first / shared-fallback two-step for static `--add-module`
  builds. Mirrors the auto-discovery branch behaviour. (codex4 P1.2.)
- `ngx_http_zstd_static_module` no longer poisons downstream gzip when the
  `.zst` sidecar is absent. `ngx_http_zstd_ok()` previously set
  `r->gzip_tested = 1; r->gzip_ok = 0;` before probing the file; on
  `NGX_ENOENT` the handler returned `NGX_DECLINED` with gzip eligibility
  already cleared, so clients sending `Accept-Encoding: gzip, zstd` lost
  gzip compression on every plain-file miss. The AE-acceptance predicate
  is now pure; gzip preemption is committed only after the sidecar is
  successfully opened and we are about to serve the precompressed payload.
  This applies to both `zstd_static on` and `zstd_static always` modes —
  `always` previously never invoked `ngx_http_zstd_ok()`, so the gzip
  flags were not set; they are now set at the commit point in both modes
  for parity (we always serve precompressed `.zst` and want to block
  downstream gzip re-compression). (codex3 P2.2.)
- `ngx_http_zstd_filter_module` dict inheritance no longer silently drops a
  configured `zstd_dict_file` when an intermediate config block disabled
  the filter. The `merge_loc_conf` level-match branch inherited
  `prev->dict` without checking whether the parent ever loaded one; a
  `http { zstd_dict_file …; server { zstd off; location /a { zstd on; } } }`
  shape produced `server->dict == NULL` (parent merged with `enable=0`),
  which the child then inherited. The level-match branch now requires
  `prev->dict != NULL` before inheriting, falling through to a fresh
  `ZSTD_createCDict_byReference` otherwise. (codex3 P2.1.)
- `ngx_http_zstd_filter_module` recycled output buffer no longer carries
  stale `b->flush` / `b->sync` / `b->last_buf` / `b->last_in_chain` from
  a prior use. After `ngx_chain_update_chains` returned the link to
  `ctx->free`, a subsequent reuse via `_get_buf` could promote a normal
  data emission into a spurious downstream flush (latency artefact) or a
  false end-of-stream marker.
- `ngx_http_zstd_static_module` now sets `b->sync` for the output buffer
  to match nginx core `gzip_static` (`b->sync = (b->last_buf || b->in_file) ? 0 : 1;`).
  Avoids edge cases on zero-byte `.zst` sidecars where neither `last_buf`
  nor `in_file` is set on the trailing buf.
- `$zstd_ratio` variable truncated on responses larger than ~4.29 MB. The
  ratio computation used 32-bit arithmetic, so `in_bytes * 1000` overflowed
  whenever `in_bytes > UINT32_MAX / 1000`, producing nonsense ratios for
  the very responses where ratio reporting matters most. Multiplication
  now widens to `uint64_t` before division.
- `$zstd_ratio` output buffer width on 64-bit. The variable scratch was
  sized for a 32-bit decimal; on a 64-bit platform a pathologically large
  numerator could exceed the buffer. Widened to `NGX_INT_T_LEN`.
- `zstd_min_length` directive accepted multi-argument configurations
  silently (the directive's `args` mask permitted variadic forms). The
  directive is now declared `NGX_CONF_TAKE1`, so `zstd_min_length 256 512;`
  is rejected at config-parse time as a typo / misuse rather than being
  parsed loosely.
- `merge_loc_conf` returned a bare `NULL` literal where the nginx ABI
  expects `NGX_CONF_OK` (which is `(char *) NULL`). No behavior change,
  but the typed return is correct per the nginx module contract and
  matches every other config callback in the module.

### Testing

- Valgrind release gate no longer reports fully-suppressed summaries
  as failures. The prior per-log regex matched any `ERROR SUMMARY:
  [1-9]` and flagged real-world logs whose only errors were suppressed
  by `valgrind.suppress`. The check now parses
  `ERROR SUMMARY: N errors from M contexts (suppressed: K)` and only
  fails when `N - K > 0`, plus a positive `definitely lost:` byte count.
  Shared helper extracted to `t/check-valgrind-log.sh` for DRY +
  testability between `t/valgrind.sh` and `t/docker/run-valgrind.sh`.
  (codex4 P1.3a.)
- SAST release gate no longer masks analyzer exit codes with blanket
  `|| true`. The five analyzer invocations in `t/docker/run-sast.sh`
  (`scan-build`, `clang-tidy`, `cppcheck`, `gcc -fanalyzer`,
  `flawfinder`) now propagate their exit codes into `overall_rc`,
  captured via `${PIPESTATUS[0]}` for `tee` chains. Policy split:
  `scan-build` remains **advisory** pending triage of its current 46
  findings; the other four are **blocking**. `|| true` retained only
  on housekeeping `make clean`. Static-grep regression assertions in
  `t/test-gate-semantics.sh` catch any future `|| true` re-introduction
  on a blocking analyzer line in milliseconds. (codex4 P1.3b.)
- New `t/regression/test_codex4_enum_sentinel.py` covers `zstd_static`
  enum parsing: off/on/always parametrized happy paths plus two
  invalid-token rejection cases (`zstd_static bad;` and
  `zstd_static maybe;`).
- New `t/test-explicit-paths.sh` end-to-end harness for the codex4 P1.2
  fix: builds the dynamic module with explicit `ZSTD_INC` / `ZSTD_LIB`
  set against the `ubuntu-24.04` image, asserts the link line uses
  `-L<lib> -lzstd -Wl,-rpath,<lib>`, and runs `nginx -t` against the
  loaded `.so`. Skipped automatically if the image is absent.
- New `t/test-gate-semantics.sh` + `t/check-valgrind-log.sh` exercise
  the Valgrind gate against seven canned fixtures covering the
  classification axes: clean-pass (`suppressed-only.log`), real-leak
  (`real-leak.log`, `comma-leak.log`), error-only (`error-no-leak.log`,
  `mixed.log`), suppressed-error + leak (`suppressed-error-with-leak.log`),
  and malformed input (`truncated.log`). Plus the SAST static-grep
  assertions on `t/docker/run-sast.sh`. Locks in the gate-semantics
  contract without requiring a full Docker/analyzer setup.
- New parallel matrix runner: `t/matrix-parallel.sh` + `t/docker/docker-bake.hcl`
  build and run the default 6-variant regression matrix concurrently
  instead of the sequential `t/build.sh` + `t/run.sh` driver pair.
  Observed ~6× wall-clock speedup on the local linux/amd64-via-Rosetta
  loop. Logs land alongside the existing sequential outputs in
  `tmp/run/<variant>/`. The sequential drivers remain the canonical
  reference; the parallel runner is an iteration-time optimization.

### Build

- Fix stray space in the `-Wl,-rpath, $ZSTD_LIB` linker flag emitted by
  `filter/config` and `static/config`. GNU ld tolerated the form as two
  separate flag words; BFD/lld and some cross-toolchains treat the
  comma-terminated form as canonical and rejected the space variant.

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
  bump pointer, mirroring the nginx gzip filter's allocator pattern
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
  slow-client window: delta-RSS 4148 -> 416 KiB and delta-VSZ 5396 -> 0 KiB
  (~10x reduction in workspace memory held after compression
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
For history before 0.3.0 see the git log on the `stable` branch -- notable
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
