# Name
zstd-nginx-module - Nginx module for the [Zstandard compression](https://facebook.github.io/zstd/).

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
    * [zstd_window_bits](#zstd_window_bits)
    * [zstd_types](#zstd_types)
    * [zstd_buffers](#zstd_buffers)
  * [ngx_http_zstd_static_module](#ngx_http_zstd_static_module)
    * [zstd_static](#zstd_static)
* [Variables](#variables)
  * [ngx_http_zstd_filter_module](#ngx_http_zstd_filter_module)
    * [$zstd_ratio](#$zstd_ratio)
* [Author](#author)

# Status

This Nginx module is currently considered experimental. Issues and PRs are welcome if you encounter any problems.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## Requirements

- libzstd 1.4.0 or newer. The streaming compression filter uses
  `ZSTD_compressStream2` (introduced August 2019). Ubuntu 20.04+ and
  Debian 11+ ship a compatible package. The static-only module
  (`ngx_http_zstd_static_module`) does not link libzstd and is unaffected.

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
* auto-discovery prefers the **shared** libzstd (required for `--add-dynamic-module`, since static archives are usually not built with `-fPIC`). A static archive (`libzstd.a`) is tried as a fallback only for static `--add-module` builds. When `ZSTD_INC`/`ZSTD_LIB` are set explicitly, the branch depends on build mode: static `--add-module` builds try `$ZSTD_LIB/libzstd.a` first then fall back to the shared library, whereas `--add-dynamic-module` builds skip the archive entirely and link the shared library (so the resulting `.so` does not pull in non-PIC `.a` objects and break with `R_X86_64_PC32` relocation errors). The module includes `zstd.h` with `ZSTD_STATIC_LINKING_ONLY` to expose experimental APIs (header visibility only — not a link mode).
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

#### Accept-Encoding handling

The Accept-Encoding header is parsed per RFC 9110. `zstd;q=0` explicitly disables zstd for that request and the parser continues scanning for another acceptable token. Optional whitespace (OWS) is tolerated around `;` (e.g. `zstd ;q=0`, `zstd;\tq=0`). Token matching is strict: false-prefix names like `zstdx` no longer match. Matching is case-insensitive (both the encoding token and the `q=` parameter name). The wildcard `*` is intentionally not honoured — matching upstream nginx gzip semantics. Both `zstd` and `zstd_static` use the same parser.

### zstd_comp_level

**Syntax:** *zstd_comp_level level;*  
**Default:** *zstd_comp_level 1;*  
**Context:** *http, server, location*

Sets a zstd compression level of a response. Acceptable values are in the range from 1 to `ZSTD_maxCLevel()`.

When the response `Content-Length` is known, the filter now derives per-request
`windowLog` / `hashLog` / `chainLog` from `ZSTD_getCParams(level, content_length, 0)`
and sizes the per-request workspace accordingly. The effective compression level
is unchanged — only the auxiliary parameter set is right-sized to the body.
For chunked or unknown-length responses the level defaults still apply (see
[`zstd_window_bits`](#zstd_window_bits) if you also want to bound memory for
those responses).

### zstd_min_length

**Syntax:** *zstd_min_length length;*  
**Default:** *zstd_min_length 20;*  
**Context:** *http, server, location*

Sets the minimum length of a response that will be compressed by zstd. The length is determined only from the `Content-Length` response header field.

A response with a known `Content-Length` of `0` is never compressed, regardless
of this setting (including `zstd_min_length 0;`) — compressing an empty body
only produces a pointless empty zstd frame. Responses of *unknown* length
(e.g. chunked) are not subject to this directive; an unknown-length response
that turns out to be empty still produces a valid empty zstd frame, because
`Content-Encoding` is committed before any body bytes arrive (same behaviour
as nginx gzip).

### zstd_window_bits

**Syntax:** *zstd_window_bits N;*  
**Default:** *-* (unset; effective `windowLog` selected by libzstd from `Content-Length` and `zstd_comp_level`)  
**Context:** *http, server, location*

Caps the per-request `windowLog` derived from `Content-Length`. The accepted
range is what `ZSTD_cParam_getBounds(ZSTD_c_windowLog)` returns from the
linked libzstd — typically `[10, 31]` on 64-bit, `[10, 30]` on 32-bit.
Unset = no cap; auto-window picks a snug fit for each response.

Setting `zstd_window_bits 23;` is recommended when `zstd_comp_level >= 17`
so responses stay within the [RFC 8878] (§3.1.1.1.2, §5) decoder default
`Maximum_Window_Size` of 8 MiB. Lower values trade compression ratio for
memory; useful on memory-constrained workers.

Approximate workspace per request at `zstd_comp_level 6`:

| `windowLog` | window  | workspace per request |
|---|---|---|
| 10 | 1 KiB   | ~64 KiB   |
| 14 | 16 KiB  | ~256 KiB  |
| 17 | 128 KiB | ~1 MiB    |
| 20 | 1 MiB   | ~5.5 MiB  |
| 23 | 8 MiB   | ~40 MiB   |

Memory is per concurrent in-flight compression; chunked responses use the
level default unless capped. Dict-configured locations skip auto-window.

[RFC 8878]: https://www.rfc-editor.org/rfc/rfc8878

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

## ngx_http_zstd_static_module

The `ngx_http_zstd_static_module` module allows sending precompressed files with the `.zst` filename extension instead of regular files.

### zstd_static

**Syntax:**	*zstd_static on | off | always;*  
**Default:** *zstd_static off;*  
**Context:** *http, server, location*  

Enables ("on") or disables ("off") checking the existence of precompressed files. The following directives are also taken into account: `gzip_vary`.

With the _"always"_ value, "zstd" file is used in all cases, without checking if the client supports it.

Range requests on `.zst` sidecars are supported: when `zstd_static` serves a `.zst` file the handler sets `r->allow_ranges = 1` before sending headers, so nginx core's range filter delivers 206 Partial Content / 416 / `Accept-Ranges: bytes` / `If-Range` exactly as it would for an uncompressed static file. Matches `gzip_static` behaviour.


# Variables

## ngx_http_zstd_filter_module

### $zstd_ratio

Achieved compression ratio, computed as the ratio between the original and compressed response sizes.

Auto-tune from `Content-Length` does not degrade `$zstd_ratio`:
`ZSTD_getCParams(level, srcSize, 0)` always returns
`windowLog >= ceil(log2(srcSize))`, so all back-references within the
body remain reachable and the ratio matches the status-quo level-default
configuration for any single response. Ratio CAN degrade only when the
operator sets [`zstd_window_bits`](#zstd_window_bits) below
`ceil(log2(response_size))`; that is an explicit, documented memory-vs-ratio
trade-off.

# Author

Alex Zhang (张超) zchao1995@gmail.com, UPYUN Inc.

# License

This Nginx module is licensed under [BSD 2-Clause License](LICENSE).
