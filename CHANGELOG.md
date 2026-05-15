# Changelog

All notable changes to this fork of zstd-nginx-module are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This fork diverges from upstream [tokers/zstd-nginx-module](https://github.com/tokers/zstd-nginx-module) at commit `057a7d3`.

## [0.2.0] - 2026-05-15

### Fixed

- HTTP/2 silent truncation at the 131072-byte boundary for inputs larger than `ZSTD_CStreamInSize()` (cherry-picked from upstream PR #49).
- Infinite loop when an upstream returns an abnormal `Content-Length` (cherry-picked from upstream PR #23).
- Accept-Encoding parser: replaced the unsafe substring match with an RFC 9110-compliant bounded parser supporting q-values, OWS, and continuation past `zstd;q=0` to later acceptable tokens (closes upstream #46). False-match tokens like `zstdx`, `zstd-future`, and `xzstd` are now rejected.
- HEAD requests now set `Content-Encoding: zstd` and `Vary: Accept-Encoding` for parity with GET; previously the headers were missing because the header filter short-circuited on `header_only`.
- CDict cleanup handler registered to prevent a one-shot leak on `nginx -s reload` when `zstd_dict_file` is configured.
- Static `.zst`-serving module: bounded stop-character check in Accept-Encoding parsing rejects `zstdx` / `zstd-future` false matches. (The static module intentionally does not honour `q=0` — the filter module is the authoritative parser; see the divergence note in `static/ngx_http_zstd_static_module.c`.)
- Build: prefer the shared `libzstd` (`-lzstd`) over `-l:libzstd.a` so the produced dynamic module is loadable on Debian and Ubuntu, whose distro static archives are not built with `-fPIC` (closes upstream #9, #16, #37). Both the filter and static module configs apply this preference.
- Static-build filter ordering: zstd now registers after brotli in `HTTP_FILTER_MODULES` so the runtime chain is `zstd > br > gzip` regardless of `--add-module` order. Previously the order was indeterminate.
- Trivial cleanups: C99 `//` comments replaced with `/* */`, `ZSTD_getErrorName(rc)` → `ZSTD_getErrorName(rv)` typo fixed, `ZSTD_minCLevel()` guarded for libzstd < 1.3.6, rpath spacing in build glue, static module H1 parser line-folding correctness.
- Accept-Encoding short-circuit removed from `ngx_http_zstd_ok`: the bounded parser is now always invoked, so `zstd;q=0` is correctly rejected even when the raw header bytes contain the substring `zstd`.

### Added

- `zstd_max_length <size>` directive — skip compression when the response `Content-Length` exceeds the threshold. Mirrors `zstd_min_length` as an upper bound.
- `zstd_bypass <variable> ...` directive — skip compression when any predicate variable evaluates truthy. Evaluated very early in the header filter. Same semantics as `gzip_disable` / `proxy_cache_bypass`.
- `zstd_window_bits <10..27>` directive — override the ZSTD `windowLog` parameter. Requires libzstd 1.4+; older builds parse the directive but treat it as a no-op. Larger windows yield better compression ratio at the cost of more memory per stream. Has no effect when `zstd_dict_file` is configured — the CDict is created with `conf->level` and libzstd derives the window from the dict; combining the two directives is unsupported in V1.
- Docker-based `linux/amd64` regression test infrastructure across five variants: Ubuntu 22.04, 24.04, 24.04 shared-only, 26.04, and 24.04 + brotli. Bash driver scripts at `t/build.sh` and `t/run.sh`. Regression scripts in `t/regression/` cover H2 truncation, the infinite-loop guard, the Accept-Encoding matrix, HEAD parity, all three new directives, filter priority, and CDict reload. Verified locally on 22.04, 24.04, 24.04 shared-only, and 24.04 + brotli (all green); 26.04 Dockerfile written but not smoked end-to-end yet (re-pin once the Ubuntu base tag is on Docker Hub).
- SAST docker variant (`t/docker/Dockerfile.sast` + `t/sast.sh`): scan-build, clang-tidy, cppcheck, gcc-fanalyzer, flawfinder. Run `bash t/sast.sh` for all tools or `bash t/sast.sh cppcheck` for one.
- Valgrind memcheck docker variant (`t/docker/Dockerfile.valgrind` + `t/valgrind.sh`): per-request leak detection with the standard OpenSSL/nginx suppression file. Runs against accept-encoding + dict-reload regressions.
- ASan/UBSan docker variant (`t/docker/Dockerfile.asan` + `t/asan.sh`): Clang `-fsanitize=address,undefined` build, runs the existing regression suite against an instrumented nginx. Catches use-after-free, heap overflows, signed-overflow UB.

### Notes (operator-visible behavior changes from upstream tokers)

- `Accept-Encoding: zstd;q=0` is now correctly rejected per RFC 9110. Upstream silently accepted it because the prefix-match short-circuited before any q-value parsing. Operators relying on RFC-conformant `q=0` reject behavior will now see it work.
- HEAD requests now carry `Content-Encoding: zstd` and `Vary: Accept-Encoding`. Tooling that inspects these headers on HEAD (cache validators, CDN probes, monitoring) now sees the same headers as on GET.
- Static-build filter chain on hosts compiled with both brotli and zstd: `Accept-Encoding: gzip, br, zstd` now consistently yields `Content-Encoding: zstd`. Previously the result depended on `--add-module` ordering and could surface as `Content-Encoding: br`.

### Attribution

- PR #49 (HTTP/2 truncation fix): Tom Taylor &lt;me@tommytaylor.co.uk&gt;, cherry-picked from `t0mtaylor/zstd-nginx-module`.
- PR #23 (infinite-loop fix): drawing &lt;cppbreak@qq.com&gt;, cherry-picked from upstream `tokers/zstd-nginx-module`.

[0.2.0]: https://github.com/shomenkow/zstd-nginx-module/releases/tag/v0.2.0
