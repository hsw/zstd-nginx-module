
/*
 * Copyright (C) Alex Zhang
 */


#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>


#define NGX_HTTP_ZSTD_STATIC_OFF        0
#define NGX_HTTP_ZSTD_STATIC_ON         1
#define NGX_HTTP_ZSTD_STATIC_ALWAYS     2


typedef struct {
    ngx_uint_t  enable;
} ngx_http_zstd_static_conf_t;


static ngx_conf_enum_t  ngx_http_zstd_static[] = {
    { ngx_string("off"), NGX_HTTP_ZSTD_STATIC_OFF },
    { ngx_string("on"), NGX_HTTP_ZSTD_STATIC_ON },
    { ngx_string("always"), NGX_HTTP_ZSTD_STATIC_ALWAYS },
};


static ngx_command_t  ngx_http_zstd_static_commands[] = {

    { ngx_string("zstd_static"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_enum_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_static_conf_t, enable),
      &ngx_http_zstd_static },

    ngx_null_command
};


static ngx_int_t ngx_http_zstd_static_handler(ngx_http_request_t *r);
static ngx_int_t ngx_http_zstd_accept_encoding(ngx_str_t *ae);
static ngx_int_t ngx_http_zstd_ok(ngx_http_request_t *r);
static void * ngx_http_zstd_static_create_loc_conf(ngx_conf_t *cf);
static char * ngx_http_zstd_static_merge_loc_conf(ngx_conf_t *cf, void *parent,
    void *child);
static ngx_int_t ngx_http_zstd_static_init(ngx_conf_t *cf);


static ngx_http_module_t  ngx_http_zstd_static_module_ctx = {
    NULL,                                     /* preconfiguration */
    ngx_http_zstd_static_init,                /* postconfiguration */

    NULL,                                     /* create main configuration */
    NULL,                                     /* init main configuration */

    NULL,                                     /* create server configuration */
    NULL,                                     /* merge server configuration */

    ngx_http_zstd_static_create_loc_conf,     /* create location configuration */
    ngx_http_zstd_static_merge_loc_conf,      /* merge location configuration */
};


ngx_module_t  ngx_http_zstd_static_module = {
    NGX_MODULE_V1,
    &ngx_http_zstd_static_module_ctx,       /* module context */
    ngx_http_zstd_static_commands,          /* module directives */
    NGX_HTTP_MODULE,                        /* module type */
    NULL,                                   /* init master */
    NULL,                                   /* init module */
    NULL,                                   /* init process */
    NULL,                                   /* init thread */
    NULL,                                   /* exit thread */
    NULL,                                   /* exit process */
    NULL,                                   /* exit master */
    NGX_MODULE_V1_PADDING
};


static ngx_int_t
ngx_http_zstd_static_handler(ngx_http_request_t *r)
{
    u_char                       *p;
    ngx_int_t                     rc;
    ngx_uint_t                    level;
    size_t                        root;
    ngx_str_t                     path;
    ngx_buf_t                    *b;
    ngx_log_t                    *log;
    ngx_table_elt_t              *h;
    ngx_chain_t                   out;
    ngx_open_file_info_t          of;
    ngx_http_core_loc_conf_t     *clcf;
    ngx_http_zstd_static_conf_t  *zscf;

    if (!(r->method & (NGX_HTTP_GET|NGX_HTTP_HEAD))) {
        return NGX_DECLINED;
    }

    if (r->uri.data[r->uri.len - 1] == '/') {
        return NGX_DECLINED;
    }

    zscf = ngx_http_get_module_loc_conf(r, ngx_http_zstd_static_module);

    if (zscf->enable == NGX_HTTP_ZSTD_STATIC_OFF) {
        return NGX_DECLINED;
    }

    if (zscf->enable == NGX_HTTP_ZSTD_STATIC_ON) {
        rc = ngx_http_zstd_ok(r);

    } else {
        rc = NGX_OK;
    }

    clcf = ngx_http_get_module_loc_conf(r, ngx_http_core_module);

    if (!clcf->gzip_vary && rc != NGX_OK) {
        return NGX_DECLINED;
    }

    log = r->connection->log;

    p = ngx_http_map_uri_to_path(r, &path, &root, sizeof(".zst") - 1);
    if (p == NULL) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    *p++ = '.';
    *p++ = 'z';
    *p++ = 's';
    *p++ = 't';
    *p = '\0';

    path.len = p - path.data;

    ngx_log_debug1(NGX_LOG_DEBUG_HTTP, log, 0,
                   "http filename: \"%s\"", path.data);

    ngx_memzero(&of, sizeof(ngx_open_file_info_t));

    of.read_ahead = clcf->read_ahead;
    of.directio = clcf->directio;
    of.valid = clcf->open_file_cache_valid;
    of.min_uses = clcf->open_file_cache_min_uses;
    of.errors = clcf->open_file_cache_errors;
    of.events = clcf->open_file_cache_events;

    if (ngx_http_set_disable_symlinks(r, clcf, &path, &of) != NGX_OK) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    if (ngx_open_cached_file(clcf->open_file_cache, &path, &of, r->pool)
        != NGX_OK)
    {
        switch (of.err) {

        case 0:
            return NGX_HTTP_INTERNAL_SERVER_ERROR;

        case NGX_ENOENT:
        case NGX_ENOTDIR:
        case NGX_ENAMETOOLONG:

            return NGX_DECLINED;

        case NGX_EACCES:
#if (NGX_HAVE_OPENAT)
        case NGX_EMLINK:
        case NGX_ELOOP:
#endif

            level = NGX_LOG_ERR;
            break;

        default:

            level = NGX_LOG_CRIT;
            break;
        }

        ngx_log_error(level, log, of.err,
                      "%s \"%s\" failed", of.failed, path.data);

        return NGX_DECLINED;
    }

    if (zscf->enable == NGX_HTTP_ZSTD_STATIC_ON) {
        r->gzip_vary = 1;

        if (rc != NGX_OK) {
            return NGX_DECLINED;
        }
    }

    ngx_log_debug1(NGX_LOG_DEBUG_HTTP, log, 0, "http static fd: %d", of.fd);

    if (of.is_dir) {
        ngx_log_debug0(NGX_LOG_DEBUG_HTTP, log, 0, "http dir");
        return NGX_DECLINED;
    }

#if !(NGX_WIN32) /* the not regular files are probably Unix specific */

    if (!of.is_file) {
        ngx_log_error(NGX_LOG_CRIT, log, 0,
                      "\"%s\" is not a regular file", path.data);

        return NGX_HTTP_NOT_FOUND;
    }

#endif

    r->root_tested = !r->error_page;

    /* Committed to serving the precompressed .zst sidecar — block the
     * downstream gzip filter from re-compressing it. Set here (after
     * every NGX_DECLINED / NGX_HTTP_NOT_FOUND branch above) so that a
     * missing sidecar does NOT poison gzip eligibility for clients
     * sending `AE: gzip, zstd` (P2.2 fix, docs/codex3.md). Applies to
     * both `zstd_static on` and `always` modes. */
    r->gzip_tested = 1;
    r->gzip_ok = 0;

    rc = ngx_http_discard_request_body(r);
    if (rc != NGX_OK) {
        return rc;
    }

    log->action = "sending response to client";

    r->headers_out.status = NGX_HTTP_OK;
    r->headers_out.content_length_n = of.size;
    r->headers_out.last_modified_time = of.mtime;

    if (ngx_http_set_etag(r) != NGX_OK) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    if (ngx_http_set_content_type(r) != NGX_OK) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    h = ngx_list_push(&r->headers_out.headers);
    if (h == NULL) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    h->hash = 1;
    ngx_str_set(&h->key, "Content-Encoding");
    ngx_str_set(&h->value, "zstd");
    r->headers_out.content_encoding = h;

    b = ngx_calloc_buf(r->pool);
    if (b == NULL) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    b->file = ngx_pcalloc(r->pool, sizeof(ngx_file_t));
    if (b->file == NULL) {
        return NGX_HTTP_INTERNAL_SERVER_ERROR;
    }

    r->allow_ranges = 1;

    rc = ngx_http_send_header(r);

    if (rc == NGX_ERROR || rc > NGX_OK || r->header_only) {
        return rc;
    }

    b->file_pos = 0;
    b->file_last = of.size;

    b->in_file = b->file_last ? 1 : 0;
    b->last_buf = (r == r->main) ? 1 : 0;
    b->last_in_chain = 1;
    b->sync = (b->last_buf || b->in_file) ? 0 : 1;

    b->file->fd = of.fd;
    b->file->name = path;
    b->file->log = log;
    b->file->directio = of.is_directio;

    out.buf = b;
    out.next = NULL;

    return ngx_http_output_filter(r, &out);
}


/*
 * Pure predicate: returns NGX_OK iff the client advertises a non-zero-q
 * "zstd" token in Accept-Encoding. No side effects on r->gzip_*; the
 * caller flips r->gzip_tested / r->gzip_ok only after committing to
 * serve the precompressed sidecar (P2.2 fix — see handler below). The
 * old version poisoned gzip eligibility here, which was visible to
 * clients sending `AE: gzip, zstd` when the .zst sidecar was absent and
 * the handler fell through to NGX_DECLINED.
 */
static ngx_int_t
ngx_http_zstd_ok(ngx_http_request_t *r)
{
    ngx_table_elt_t  *ae;

    if (r != r->main) {
        return NGX_DECLINED;
    }

    ae = r->headers_in.accept_encoding;
    if (ae == NULL) {
        return NGX_DECLINED;
    }

    if (ae->value.len < sizeof("zstd") - 1) {
        return NGX_DECLINED;
    }

    return ngx_http_zstd_accept_encoding(&ae->value);
}


/*
 * Bit-for-bit copy of filter/ngx_http_zstd_filter_module.c —
 * ngx_http_zstd_quantity. Keep in sync with that copy; see the longer
 * note above ngx_http_zstd_accept_encoding below.
 */
static ngx_uint_t
ngx_http_zstd_quantity(u_char *p, u_char *last)
{
    u_char      c;
    ngx_uint_t  n, q;

    /*
     * Parses a q-value per RFC 9110 section 12.4.2 / 5.3.1:
     *     qvalue = ( "0" [ "." 0*3DIGIT ] ) / ( "1" [ "." 0*3("0") ] )
     * Returns 0 for invalid/zero q-values, non-zero for any positive q.
     * The non-zero magnitude is not a faithful percentage — digit place
     * values are not scaled (matches upstream `ngx_http_gzip_quantity`
     * quirk). Callers must only branch on `q > 0`.
     */

    c = *p++;

    if (c != '0' && c != '1') {
        return 0;
    }

    q = (c - '0') * 100;

    if (p == last) {
        return q;
    }

    c = *p++;

    if (c == ',' || c == ' ' || c == '\t') {
        return q;
    }

    if (c != '.') {
        return 0;
    }

    n = 0;

    while (p < last) {
        c = *p++;

        if (c == ',' || c == ' ' || c == '\t') {
            break;
        }

        if (c >= '0' && c <= '9') {
            q += c - '0';
            n++;
            continue;
        }

        return 0;
    }

    if (q > 100 || n > 3) {
        return 0;
    }

    return q;
}


/*
 * Bounded RFC 9110-compliant Accept-Encoding parser. Iterates comma-
 * separated tokens, case-insensitively matches "zstd" with strict token
 * boundaries (comma, semicolon, whitespace, end-of-buffer), and honours
 * `;q=<value>` parameters: q=0 explicitly rejects the token and the loop
 * continues to the next token (RFC 9110 allows duplicate coding tokens
 * with conflicting q-values, last-accept-wins is approximated by
 * first-accept-wins here — sufficient for V1). Default (no q) = accept.
 *
 * Modelled on ngx_http_gzip_accept_encoding() at
 * tmp/src/nginx/src/http/ngx_http_core_module.c:2266-2330 with two
 * deviations: (1) iterate past q=0 to find another zstd token instead of
 * returning DECLINED on first match; (2) accept TAB as token whitespace
 * (RFC 9110 OWS).
 *
 * BIT-FOR-BIT COPY of ngx_http_zstd_accept_encoding from
 * filter/ngx_http_zstd_filter_module.c — kept in sync manually to avoid
 * RFC-handling drift (see codex review of fix/ae-parser on 2026-05-16:
 * an earlier hand-adapted static version missed OWS-before-`;` handling
 * and accepted `zstd ;q=0` incorrectly). When this needs to change,
 * update both copies in lockstep. Factoring into a shared header is V2
 * cleanup — see V2 follow-up `refactor/share-ae-parser` branch.
 *
 * Same goes for ngx_http_zstd_quantity above.
 */
static ngx_int_t
ngx_http_zstd_accept_encoding(ngx_str_t *ae)
{
    u_char      *p, *start, *last;
    ngx_uint_t   q;

    start = ae->data;
    last = start + ae->len;

    /*
     * `start` is the next byte from which to resume the case-insensitive
     * "zstd" search. It may point MID-TOKEN after a false-match advance
     * (e.g. past "zstdx" → 'x'), so the inner search loop validates the
     * left boundary via *(p - 1) before accepting a match. After q=0
     * rejection it points one byte past a ',', and on entry it points
     * to BOS.
     */
    for ( ;; ) {

        /* locate next case-insensitive "zstd" candidate with a valid left
         * boundary (BOS, comma, or whitespace) */

        for ( ;; ) {
            p = ngx_strcasestrn(start, "zstd", sizeof("zstd") - 2);
            if (p == NULL) {
                return NGX_DECLINED;
            }

            if (p == ae->data
                || *(p - 1) == ',' || *(p - 1) == ' ' || *(p - 1) == '\t')
            {
                break;
            }

            start = p + sizeof("zstd") - 1;
            if (start >= last) {
                return NGX_DECLINED;
            }
        }

        p += sizeof("zstd") - 1;

        /* right boundary: must be comma, semicolon, whitespace, or EOS;
         * otherwise this is "zstdx" / "zstd-future" etc. — skip past it
         * and resume the outer search */

        if (p == last) {
            return NGX_OK;
        }

        if (*p == ',') {
            return NGX_OK;
        }

        if (*p == ' ' || *p == '\t') {
            /* OWS before ',' or ';' or EOS */
            while (p < last && (*p == ' ' || *p == '\t')) {
                p++;
            }
            if (p == last || *p == ',') {
                return NGX_OK;
            }
            if (*p != ';') {
                /* unexpected token after whitespace — not our match */
                start = p;
                continue;
            }
            /* fall through to ';' handling */
        } else if (*p != ';') {
            /* not a token boundary — false match (e.g. "zstdx") */
            start = p;
            continue;
        }

        /* parameter section: ";" *( OWS ";" OWS parameter ) — we only
         * care about q= */

        p++;  /* skip ';' */

        while (p < last && (*p == ' ' || *p == '\t')) {
            p++;
        }

        if (p == last) {
            return NGX_OK;
        }

        if (*p != 'q' && *p != 'Q') {
            /* non-q parameter — RFC 9110 allows other params; treat as
             * accept and ignore them. Skip to next ',' boundary. */
            while (p < last && *p != ',') {
                p++;
            }
            return NGX_OK;
        }

        p++;  /* skip 'q' */

        while (p < last && (*p == ' ' || *p == '\t')) {
            p++;
        }

        if (p == last || *p++ != '=') {
            return NGX_DECLINED;
        }

        while (p < last && (*p == ' ' || *p == '\t')) {
            p++;
        }

        if (p == last) {
            return NGX_DECLINED;
        }

        q = ngx_http_zstd_quantity(p, last);

        if (q > 0) {
            return NGX_OK;
        }

        /* q == 0: token explicitly rejected; advance past this token to
         * the next comma and resume the outer search */

        while (p < last && *p != ',') {
            p++;
        }

        if (p == last) {
            return NGX_DECLINED;
        }

        start = p + 1;  /* skip comma */
        if (start >= last) {
            return NGX_DECLINED;
        }
    }
}


static void *
ngx_http_zstd_static_create_loc_conf(ngx_conf_t *cf)
{
    ngx_http_zstd_static_conf_t  *conf;

    conf = ngx_palloc(cf->pool, sizeof(ngx_http_zstd_static_conf_t));
    if (conf == NULL) {
        return NULL;
    }

    conf->enable = NGX_CONF_UNSET_UINT;

    return conf;
}


static char *
ngx_http_zstd_static_merge_loc_conf(ngx_conf_t *cf, void *parent, void *child)
{
    ngx_http_zstd_static_conf_t *prev = parent;
    ngx_http_zstd_static_conf_t *conf = child;

    ngx_conf_merge_uint_value(conf->enable, prev->enable,
                              NGX_HTTP_ZSTD_STATIC_OFF);

    return NGX_CONF_OK;
}


static ngx_int_t
ngx_http_zstd_static_init(ngx_conf_t *cf)
{
    ngx_http_handler_pt        *h;
    ngx_http_core_main_conf_t  *cmcf;

    cmcf = ngx_http_conf_get_module_main_conf(cf, ngx_http_core_module);

    h = ngx_array_push(&cmcf->phases[NGX_HTTP_CONTENT_PHASE].handlers);
    if (h == NULL) {
        return NGX_ERROR;
    }

    *h = ngx_http_zstd_static_handler;

    return NGX_OK;
}
