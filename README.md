# Name
zstd-nginx-module - Nginx module for the [Zstandard compression](https://facebook.github.io/zstd/).

This is a fork of [tokers/zstd-nginx-module](https://github.com/tokers/zstd-nginx-module) hardened for production use. See [CHANGELOG.md](CHANGELOG.md) and [Migration notes](#migration-notes-from-upstream-tokers) for the full list of differences from upstream.

# Table of Contents

* [Name](#name)
* [Status](#status)
* [Synopsis](#synopsis)
* [Installation](#installation)
* [Directives](#directives)
  * [ngx_http_zstd_filter_module](#ngx_http_zstd_filter_module)
    * [zstd_dict_file](#zstd_dict_file)
    * [zstd](#zstd)
    * [zstd_comp_level](#zstd_comp_level)
    * [zstd_min_length](#zstd_min_length)
    * [zstd_types](#zstd_types)
    * [zstd_buffers](#zstd_buffers)
    * [zstd_bypass](#zstd_bypass)
    * [zstd_max_length](#zstd_max_length)
    * [zstd_window_bits](#zstd_window_bits)
  * [ngx_http_zstd_static_module](#ngx_http_zstd_static_module)
    * [zstd_static](#zstd_static)
* [Variables](#variables)
  * [ngx_http_zstd_filter_module](#ngx_http_zstd_filter_module-1)
    * [$zstd_ratio](#zstd_ratio)
* [Production tuning](#production-tuning)
* [Filter priority](#filter-priority)
* [Migration notes (from upstream tokers @ 057a7d3)](#migration-notes-from-upstream-tokers)
* [Author](#author)
* [License](#license)

# Status

This Nginx module is production-ready in this fork. Issues and PRs are welcome.

# Synopsis

```nginx

# specify the dictionary
zstd_dict_file /path/to/dict;

server {
    listen 127.0.0.1:8080;
    server_name localhost;

    location / {
        # enable zstd compression
        zstd on;
        zstd_min_length 256; # no less than 256 bytes
        zstd_comp_level 3; # set the level to 3

        proxy_pass http://foo.com;
    }
}

server {
    listen 127.0.0.1:8081;
    server_name localhost;

    location / {
        zstd_static on;
        root html;
    }
}
```

# Installation

To use theses modules, configure your nginx branch with `--add-module=/path/to/zstd-nginx-module`. Several points should be taken care of.

* You can set environment variables `ZSTD_INC` and `ZSTD_LIB` to specify the path to `zstd.h` and the path to zstd shared library respectively.
* The shared `libzstd` (`-lzstd`) is preferred over the static archive so the resulting dynamic module is loadable on distributions whose packaged static archives are not compiled with `-fPIC` (Debian, Ubuntu). If you require static linking, point `ZSTD_LIB` at a `-fPIC`-built static archive.
* System's zstd bundle will be linked if `ZSTD_INC` and `ZSTD_LIB` are not specified.
* Both `ngx_http_zstd_static_module` and `ngx_http_zstd_filter_module` will be configured.

# Directives

## ngx_http_zstd_filter_module

The `ngx_http_zstd_filter_module` module is a filter that compresses responses using the _"zstd"_ method. This often helps to reduce the size of transmitted data by half or even more.

### zstd_dict_file

**Syntax:** *zstd_dict_file /path/to/dict;*  
**Default:** *-*  
**Context:** *http*  

Specifies the external dictionary.

**WARNING:** Be careful! The content-coding registration only specifies a means to signal the use of the zstd format, and does not additionally specify any mechanism for advertising/negotiating/synchronizing the use of a specific dictionary between client and server. Use the `zstd_dict_file` only if you can insure that both ends _(server and client)_ are capable of using the same dictionary (e.g. advertise with a HTTP header). See https://github.com/tokers/zstd-nginx-module/issues/2 for the details.

### zstd

**Syntax:** *zstd on | off;*  
**Default:** *zstd off;*  
**Context:** *http, server, location, if in location*

Enables or disables zstd compression for response.

### zstd_comp_level

**Syntax:** *zstd_comp_level level;*  
**Default:** *zstd_comp_level 1;*  
**Context:** *http, server, location*

Sets a zstd compression level of a response. Acceptable values are in the range from 1 to `ZSTD_maxCLevel()`.

### zstd_min_length

**Syntax:** *zstd_min_length length;*  
**Default:** *zstd_min_length 20;*  
**Context:** *http, server, location*

Sets the minimum length of a response that will be compressed by zstd. The length is determined only from the `Content-Length` response header field.

### zstd_types

**Syntax:** *zstd_types mime-type ...;*  
**Default:** *zstd_types text/html;*  
**Context:** *http, server, location*

Enables zstd of responses for the specified MIME types in addition to `text/html`. The special value `*` matches any MIME type.

### zstd_buffers

**Syntax:** *zstd_buffers number size;*  
**Default:** *zstd_buffers 32 4k | 16 8k;*  
**Context:** *http, server, location*

Sets the number and size of buffers used to compress a response. By default the buffer size is equal to one memory page. This is either 4K or 8K, depending on a platform.

### zstd_bypass

**Syntax:** *zstd_bypass variable ...;*  
**Default:** *-*  
**Context:** *http, server, location*

Skips zstd compression of a response when at least one of the listed variables evaluates to a non-empty value that is not "0". Same semantics as the standard `gzip_disable` and `proxy_cache_bypass` directives. The predicates are evaluated very early in the header filter, before MIME-type and length checks.

Example:

```nginx
map $http_user_agent $skip_zstd {
    default          0;
    ~MSIE\ [4-6]\.   1;
}

zstd on;
zstd_bypass $skip_zstd $arg_nozstd;
```

### zstd_max_length

**Syntax:** *zstd_max_length size;*  
**Default:** *-*  
**Context:** *http, server, location*

Sets the maximum length of a response that will be compressed by zstd. Responses whose `Content-Length` exceeds the threshold are passed through uncompressed. This mirrors `zstd_min_length` as an upper bound and is useful when very large responses are better served as-is (already-compressed media, large archives, or when CPU budget matters more than transfer size). When the directive is not configured there is no upper bound.

### zstd_window_bits

**Syntax:** *zstd_window_bits 10..27;*  
**Default:** *-*  
**Context:** *http, server, location*

Overrides the ZSTD `windowLog` parameter for the compressor. Acceptable values are 10 through 27 inclusive. When the directive is not configured the libzstd default is used.

Larger window sizes typically yield a better compression ratio at the cost of more memory per active stream — roughly `2^windowLog` bytes per concurrent compression. Useful for long, highly-repetitive payloads such as JSON-RPC batches, log streams, or HTML dominated by repeated boilerplate.

Requires libzstd 1.4 or newer. When the module is built against an older libzstd the directive is parsed and accepted but ignored at runtime, so configuration remains portable across hosts.

## ngx_http_zstd_static_module

The `ngx_http_zstd_static_module` module allows sending precompressed files with the `.zst` filename extension instead of regular files.

### zstd_static

**Syntax:**	*zstd_static on | off | always;*  
**Default:** *zstd_static off;*  
**Context:** *http, server, location*  

Enables ("on") or disables ("off") checking the existence of precompressed files. The following directives are also taken into account: `gzip_vary`.

With the _"always"_ value, "zstd" file is used in all cases, without checking if the client supports it.


# Variables

## ngx_http_zstd_filter_module

### $zstd_ratio

Achieved compression ratio, computed as the ratio between the original and compressed response sizes.

# Production tuning

A few recommendations distilled from running this module behind public traffic:

* **`zstd_min_length 256`** for public web origins that do not use a shared dictionary. Below ~256 bytes the framing overhead dominates and compression rarely shrinks the payload. The upstream default of `20` is too aggressive for general use.
* **`zstd_window_bits 17`** (128 KiB window) for cache-friendly workloads with long, repetitive payloads (JSON APIs, HTML with shared boilerplate, log streams). Larger windows hit diminishing returns and cost memory per concurrent stream.
* **`zstd_comp_level 1`** is the CPU-vs-ratio sweet spot for on-the-fly compression. Level 1 is typically within a few percent of higher levels on web payloads while costing a fraction of the CPU. Higher levels make sense for precompressed `.zst` assets served via `zstd_static`, not for live compression.

# Filter priority

When multiple compression filters are present, nginx executes them in a chain. This module is ordered so that the runtime preference is:

```
zstd > br > gzip
```

That is, if a client sends `Accept-Encoding: gzip, br, zstd`, the response is compressed with zstd; if only `gzip, br` is sent, brotli is used; gzip is the last fallback.

* **Static builds** (`--add-module=/path/to/zstd-nginx-module`) handle the ordering automatically. The module's `config` script inserts `ngx_http_zstd_filter_module` after `ngx_http_brotli_filter_module` in `HTTP_FILTER_MODULES` so the chain is deterministic regardless of `--add-module` ordering on the configure command line.
* **Dynamic builds** (`load_module ...;` in `nginx.conf`) require the operator to load brotli before zstd so the filter chain ends up in the correct order:

  ```nginx
  load_module modules/ngx_http_brotli_filter_module.so;
  load_module modules/ngx_http_brotli_static_module.so;
  load_module modules/ngx_http_zstd_filter_module.so;
  load_module modules/ngx_http_zstd_static_module.so;
  ```

# Migration notes (from upstream tokers @ 057a7d3)

If you are migrating from upstream `tokers/zstd-nginx-module`, three behaviors changed in ways that are observable to clients and tooling.

**HEAD request parity.** Upstream's header filter short-circuited on `r->header_only` before it appended `Content-Encoding: zstd` and `Vary: Accept-Encoding`. As a result HEAD responses were missing both headers even when an equivalent GET would have been zstd-compressed. This fork sets the headers before the short-circuit, so HEAD and GET now advertise the same encoding. Tooling that inspects HEAD responses (cache validators, CDN probes, health monitors) will start to see `Content-Encoding: zstd` and `Vary: Accept-Encoding` where it previously saw neither.

**Accept-Encoding q-value handling.** Upstream matched `Accept-Encoding` with an unbounded substring search, which silently accepted `zstd;q=0` (a client signal that zstd is *not* acceptable per RFC 9110 section 12.5.3) and could also match false tokens like `zstdx` or `zstd-future`. This fork ships a bounded RFC 9110-compliant parser that respects q-values, OWS, and the token boundary. Clients that explicitly send `zstd;q=0` will now correctly receive an uncompressed (or alternately-compressed) response. Clients sending an unrelated token containing `zstd` no longer get a malformed zstd response.

**Static-build filter ordering.** Upstream relied on the `--add-module` order to position the zstd filter in the chain. On builds that combined `ngx_brotli` and this module, that meant the runtime preference depended on the configure command line and frequently surfaced as `Content-Encoding: br` instead of `zstd` for `Accept-Encoding: gzip, br, zstd`. This fork inserts the zstd filter after brotli in `HTTP_FILTER_MODULES` from the `config` script, so static builds consistently yield `Content-Encoding: zstd`. Dynamic-module operators should review the `load_module` order documented in [Filter priority](#filter-priority).

# Author

Alex Zhang (张超) zchao1995@gmail.com, UPYUN Inc. (original upstream)
Sergei Khomenkov &lt;shomenkow@gmail.com&gt; (this fork)

# License

This Nginx module is licensed under [BSD 2-Clause License](LICENSE).
