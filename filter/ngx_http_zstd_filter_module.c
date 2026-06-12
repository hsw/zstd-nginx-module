
/*
 * Copyright (C) Alex Zhang
 */


#include <ngx_config.h>
#include <ngx_core.h>
#include <ngx_http.h>

/*
 * ZSTD_STATIC_LINKING_ONLY is a header-visibility gate exposing zstd.h's
 * experimental section (ZSTD_customMem, ZSTD_getCParams, ...), NOT a link
 * mode. Defined at the include site so the module builds standalone with no
 * -D build flags; #ifndef-guarded so an operator's --with-cc-opt
 * -DZSTD_STATIC_LINKING_ONLY (= 1) doesn't trip -Werror redefinition.
 */
#ifndef ZSTD_STATIC_LINKING_ONLY
#define ZSTD_STATIC_LINKING_ONLY
#endif
#include <zstd.h>


#if ZSTD_VERSION_NUMBER < 10400
#error "libzstd 1.4.0 or later required for ZSTD_compressStream2"
#endif


/*
 * Direction A: alignment-overhead headroom added to
 * ZSTD_estimateCStreamSize(level). The bump allocator rounds each
 * sub-allocation up to NGX_ALIGNMENT, which can cost up to
 * (NGX_ALIGNMENT - 1) bytes per call. libzstd makes O(10)
 * sub-allocations during CStream init, but some configurations
 * (CDict refCDict copy-fallback under certain strategy combos,
 * unusual advanced params, libzstd version drift past the
 * estimateCStreamSize promise) can push customAlloc demand higher.
 * (64 * NGX_ALIGNMENT) gives a comfortable safety margin without
 * meaningfully growing per-request memory (1 KiB on 64-bit Linux).
 * The fallback branch in ngx_http_zstd_filter_alloc is the last
 * line of defence and now logs at ALERT so production ops notice.
 */
#define NGX_HTTP_ZSTD_WORKSPACE_HEADROOM  (64 * NGX_ALIGNMENT)


typedef struct {
    ngx_str_t                    dict_file;
} ngx_http_zstd_main_conf_t;


typedef struct {
    ngx_flag_t                   enable;
    ngx_int_t                    level;
    ssize_t                      min_length;

    /*
     * Auto-Window: per-request workspace shrink based on response
     * Content-Length. window_bits is an OPTIONAL operator-supplied
     * upper cap on libzstd's auto-derived windowLog (NGX_CONF_UNSET
     * means no cap). baseline_ws caches the level-default workspace
     * estimate computed at merge time — used both as the fallback
     * workspace size when auto-tune cannot run and as the
     * forward-compat ceiling that prevents future libzstd versions
     * from accidentally growing per-request memory.
     */
    ngx_int_t                    window_bits;
    size_t                       baseline_ws;

    ngx_hash_t                   types;

    ngx_bufs_t                   bufs;

    ngx_array_t                 *types_keys;

    ZSTD_CDict                  *dict;
} ngx_http_zstd_loc_conf_t;


typedef struct {
    ngx_chain_t                 *in;
    ngx_chain_t                 *free;
    ngx_chain_t                 *busy;
    ngx_chain_t                 *out;
    ngx_chain_t                **last_out;

    ngx_buf_t                   *in_buf;
    ngx_buf_t                   *out_buf;
    ngx_int_t                    bufs;

    ZSTD_inBuffer                buffer_in;
    ZSTD_outBuffer               buffer_out;

    ZSTD_CStream                *cstream;

    ngx_http_request_t          *request;

    size_t                       bytes_in;
    size_t                       bytes_out;

    /*
     * Direction A: single-chunk preallocated workspace for libzstd's
     * customAlloc callbacks. One ngx_palloc at CStream creation, bumped
     * forward by the customAlloc shim; freed immediately after
     * ZSTD_freeCStream returns so the workspace mmap'd pages return to
     * the kernel rather than waiting for r->pool teardown. Mirrors
     * nginx gzip's deflate_state preallocation pattern (see
     * ngx_http_gzip_filter_module.c lines 47-49, 615, 893, 925-971).
     */
    void                        *preallocated;  /* base pointer */
    char                        *free_mem;      /* next bump position */
    size_t                       allocated;     /* remaining bytes */

    /*
     * Sticky-finish flags (per-call-op pattern, modelled on ngx_brotli's
     * ngx_http_brotli_filter_module.c):
     *
     * - ctx->last  : set in add_data when consuming a buf with last_buf=1;
     *                drives op = ZSTD_e_end on subsequent compress calls
     *                until libzstd reports rc==0 (frame footer drained),
     *                then cleared after the terminal downstream buf is
     *                enqueued with b->last_buf=1.
     * - ctx->flush : set in add_data when consuming a buf with flush=1;
     *                drives op = ZSTD_e_flush on subsequent compress calls
     *                until libzstd reports rc==0 (internal buffer drained),
     *                then cleared after the downstream buf is enqueued
     *                with b->flush=1.
     *
     * The flags are sticky to guarantee flush/end propagation even when
     * libzstd swallows a small input chunk without producing output (rc==0
     * on first call). The previous action state machine (NGX_HTTP_ZSTD_
     * FILTER_COMPRESS/FLUSH/END + redo) failed in that case: it only
     * promoted COMPRESS->FLUSH when rc>0, leaving small flush'd chunks
     * stranded in libzstd's internal buffer. See PR #23 thread.
     */
    unsigned                     last:1;
    unsigned                     flush:1;
    unsigned                     done:1;
    unsigned                     nomem:1;

    /*
     * Auto-Window: snapshot of r->headers_out.content_length_n captured
     * in the header_filter BEFORE ngx_http_clear_content_length() zeroes
     * it (compression makes the on-wire length unknown). The body
     * filter's create_cstream reads this to derive cParams via
     * ZSTD_getCParams(level, content_length, 0). -1 means "unknown"
     * (chunked / upstream didn't set it).
     */
    off_t                        content_length_n;
} ngx_http_zstd_ctx_t;


typedef struct {
    ngx_conf_post_handler_pt  post_handler;
} ngx_http_zstd_comp_level_bounds_t;


static ngx_http_output_header_filter_pt  ngx_http_next_header_filter;
static ngx_http_output_body_filter_pt  ngx_http_next_body_filter;

static ngx_str_t  ngx_http_zstd_ratio = ngx_string("zstd_ratio");


static ngx_int_t ngx_http_zstd_header_filter(ngx_http_request_t *r);
static ngx_int_t ngx_http_zstd_body_filter(ngx_http_request_t *r,
    ngx_chain_t *in);
static ngx_int_t ngx_http_zstd_filter_add_data(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx);
static ngx_int_t ngx_http_zstd_filter_get_buf(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx);
static ZSTD_CStream *ngx_http_zstd_filter_create_cstream(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx);
static ngx_int_t ngx_http_zstd_filter_compress(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx);
static ngx_int_t ngx_http_zstd_accept_encoding(ngx_str_t *ae);
static ngx_int_t ngx_http_zstd_ok(ngx_http_request_t *r);
static ngx_int_t ngx_http_zstd_filter_init(ngx_conf_t *cf);
static void * ngx_http_zstd_create_main_conf(ngx_conf_t *cf);
static char *ngx_http_zstd_init_main_conf(ngx_conf_t *cf, void *conf);
static void *ngx_http_zstd_create_loc_conf(ngx_conf_t *cf);
static char *ngx_http_zstd_merge_loc_conf(ngx_conf_t *cf, void *parent,
    void *child);
static ngx_int_t ngx_http_zstd_add_variables(ngx_conf_t *cf);
static ngx_int_t ngx_http_zstd_ratio_variable(ngx_http_request_t *r,
    ngx_http_variable_value_t *vv, uintptr_t data);
static void * ngx_http_zstd_filter_alloc(void *opaque, size_t size);
static void ngx_http_zstd_filter_free(void *opaque, void *address);
static ngx_int_t ngx_http_zstd_filter_release_workspace(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx, ZSTD_CStream *cstream, ngx_uint_t err_level);
static char *ngx_http_zstd_comp_level(ngx_conf_t *cf, void *post, void *data);
static char *ngx_http_zstd_window_bits(ngx_conf_t *cf, void *post, void *data);
static char *ngx_conf_zstd_set_num_slot_with_negatives(ngx_conf_t *cf,
    ngx_command_t *cmd, void *conf);
static void ngx_http_zstd_cdict_cleanup(void *data);
static void ngx_http_zstd_filter_cleanup(void *data);


static ngx_http_zstd_comp_level_bounds_t  ngx_http_zstd_comp_level_bounds = {
    ngx_http_zstd_comp_level
};


static ngx_conf_post_t  ngx_http_zstd_window_bits_post = {
    ngx_http_zstd_window_bits
};


static ngx_command_t  ngx_http_zstd_filter_commands[] = {

    { ngx_string("zstd"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_HTTP_LIF_CONF
      |NGX_CONF_FLAG,
      ngx_conf_set_flag_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_loc_conf_t, enable),
      NULL },

    { ngx_string("zstd_comp_level"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_zstd_set_num_slot_with_negatives,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_loc_conf_t, level),
      &ngx_http_zstd_comp_level_bounds },

    { ngx_string("zstd_types"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_1MORE,
      ngx_http_types_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_loc_conf_t, types_keys),
      &ngx_http_html_default_types[0] },

    { ngx_string("zstd_buffers"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE2,
      ngx_conf_set_bufs_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_loc_conf_t, bufs),
      NULL },

    { ngx_string("zstd_min_length"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_size_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_loc_conf_t, min_length),
      NULL },

    { ngx_string("zstd_dict_file"),
      NGX_HTTP_MAIN_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_str_slot,
      NGX_HTTP_MAIN_CONF_OFFSET,
      offsetof(ngx_http_zstd_main_conf_t, dict_file),
      NULL },

    { ngx_string("zstd_window_bits"),
      NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_num_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_zstd_loc_conf_t, window_bits),
      &ngx_http_zstd_window_bits_post },

    ngx_null_command
};


static ngx_http_module_t  ngx_http_zstd_filter_module_ctx = {
    ngx_http_zstd_add_variables,            /* preconfiguration */
    ngx_http_zstd_filter_init,              /* postconfiguration */

    ngx_http_zstd_create_main_conf,         /* create main configuration */
    ngx_http_zstd_init_main_conf,           /* init main configuration */

    NULL,                                   /* create server configuration */
    NULL,                                   /* merge server configuration */

    ngx_http_zstd_create_loc_conf,          /* create location configuration */
    ngx_http_zstd_merge_loc_conf,           /* merge location configuration */
};


ngx_module_t  ngx_http_zstd_filter_module = {
    NGX_MODULE_V1,
    &ngx_http_zstd_filter_module_ctx,       /* module context */
    ngx_http_zstd_filter_commands,          /* module directives */
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
ngx_http_zstd_header_filter(ngx_http_request_t *r)
{
    ngx_table_elt_t           *h;
    ngx_http_zstd_loc_conf_t  *zlcf;
    ngx_http_zstd_ctx_t       *ctx;

    zlcf = ngx_http_get_module_loc_conf(r, ngx_http_zstd_filter_module);

    /* header_only early (gzip tests it last): skip type-hash lookup
     * when header_only is pre-set (subrequests, internal responses);
     * plain HEAD is marked header_only later, by the core filter */

    if (!zlcf->enable
        || r->header_only
        || (r->headers_out.status != NGX_HTTP_OK
            && r->headers_out.status != NGX_HTTP_FORBIDDEN
            && r->headers_out.status != NGX_HTTP_NOT_FOUND)
        || (r->headers_out.content_encoding
            && r->headers_out.content_encoding->value.len)
        || r->headers_out.content_length_n == 0
        || (r->headers_out.content_length_n != -1
            && r->headers_out.content_length_n < zlcf->min_length)
        || ngx_http_test_content_type(r, &zlcf->types) == NULL)
    {
        return ngx_http_next_header_filter(r);
    }

    r->gzip_vary = 1;

    if (ngx_http_zstd_ok(r) != NGX_OK) {
        return ngx_http_next_header_filter(r);
    }

    ctx = ngx_pcalloc(r->pool, sizeof(ngx_http_zstd_ctx_t));
    if (ctx == NULL) {
        return NGX_ERROR;
    }

    ngx_http_set_ctx(r, ctx, ngx_http_zstd_filter_module);

    ctx->request = r;
    ctx->last_out = &ctx->out;

    /*
     * Snapshot the upstream-declared Content-Length BEFORE we call
     * ngx_http_clear_content_length() below (compression makes the
     * on-wire length unknown). The body filter's create_cstream uses
     * this to derive auto-window cParams via ZSTD_getCParams(level,
     * content_length, 0). Chunked / unknown-size responses leave the
     * field at -1 here so the body filter's auto-tune skip path
     * triggers correctly.
     */
    ctx->content_length_n = r->headers_out.content_length_n;

    h = ngx_list_push(&r->headers_out.headers);
    if (h == NULL) {
        return NGX_ERROR;
    }

    h->hash = 1;
#if (nginx_version >= 1023000)
    h->next = NULL;
#endif
    ngx_str_set(&h->key, "Content-Encoding");
    ngx_str_set(&h->value, "zstd");
    r->headers_out.content_encoding = h;

    r->main_filter_need_in_memory = 1;

    ngx_http_clear_content_length(r);
    ngx_http_clear_accept_ranges(r);
    ngx_http_weak_etag(r);

    return ngx_http_next_header_filter(r);
}


static ngx_int_t
ngx_http_zstd_body_filter(ngx_http_request_t *r, ngx_chain_t *in)
{
    ngx_int_t             flush_busy, rc;
    ngx_chain_t          *cl;
    ngx_http_zstd_ctx_t  *ctx;


    ctx = ngx_http_get_module_ctx(r, ngx_http_zstd_filter_module);

    if (ctx == NULL || ctx->done || r->header_only) {
        return ngx_http_next_body_filter(r, in);
    }

    ngx_log_debug0(NGX_LOG_DEBUG_HTTP, r->connection->log, 0,
                   "http zstd filter");

    if (ctx->cstream == NULL) {
        ctx->cstream = ngx_http_zstd_filter_create_cstream(r, ctx);
        if (ctx->cstream == NULL) {
            goto failed;
        }
    }

    if (in) {
        if (ngx_chain_add_copy(r->pool, &ctx->in, in) != NGX_OK) {
            goto failed;
        }

        r->connection->buffered |= NGX_HTTP_GZIP_BUFFERED;
    }

    if (ctx->nomem) {

        /* flush busy buffers */

        if (ngx_http_next_body_filter(r, NULL) == NGX_ERROR) {
            goto failed;
        }

        cl = NULL;

        ngx_chain_update_chains(r->pool, &ctx->free, &ctx->busy, &cl,
                                (ngx_buf_tag_t) &ngx_http_zstd_filter_module);

        flush_busy = 0;
        ctx->nomem = 0;

    } else {
        flush_busy = ctx->busy ? 1 : 0;
    }

    for ( ;; ) {

        /* cycle while we can write to a client */

        for ( ;; ) {

            rc = ngx_http_zstd_filter_add_data(r, ctx);

            if (rc == NGX_DECLINED) {
                break;
            }

            if (rc == NGX_AGAIN) {
                continue;
            }

            rc = ngx_http_zstd_filter_get_buf(r, ctx);

            if (rc == NGX_ERROR) {
                goto failed;
            }

            if (rc == NGX_DECLINED) {
                break;
            }

            rc = ngx_http_zstd_filter_compress(r, ctx);

            if (rc == NGX_ERROR) {
                goto failed;
            }

            if (rc == NGX_OK) {
                break;
            }

            /* rc == NGX_AGAIN */
        }

        if (ctx->out == NULL && !flush_busy) {
            return ctx->busy ? NGX_AGAIN : NGX_OK;
        }

        rc = ngx_http_next_body_filter(r, ctx->out);

        if (rc == NGX_ERROR) {
            goto failed;
        }

        ngx_chain_update_chains(r->pool, &ctx->free, &ctx->busy, &ctx->out,
                                (ngx_buf_tag_t) &ngx_http_zstd_filter_module);

        ctx->last_out = &ctx->out;
        ctx->nomem = 0;
        flush_busy = 0;

        if (ctx->done) {
            if (ngx_http_zstd_filter_release_workspace(r, ctx, ctx->cstream,
                                                      NGX_LOG_ALERT)
                != NGX_OK)
            {
                rc = NGX_ERROR;
            }
            return rc;
        }
    }

failed:

    ctx->done = 1;
    (void) ngx_http_zstd_filter_release_workspace(r, ctx, ctx->cstream,
                                                  NGX_LOG_ALERT);
    return NGX_ERROR;
}


static ngx_int_t
ngx_http_zstd_filter_compress(ngx_http_request_t *r, ngx_http_zstd_ctx_t *ctx)
{
    size_t             rc, pos_in, pos_out;
    ngx_chain_t       *cl;
    ngx_buf_t         *b;
    ngx_uint_t         has_bytes, has_signal;
    ZSTD_EndDirective  op;

    /*
     * Per-call-op pattern (ngx_brotli model): derive the libzstd endOp
     * directive from the sticky-finish flags set in add_data when the
     * upstream buf had last_buf=1 / flush=1. last takes precedence over
     * flush (terminal finish overrides intermediate flush).
     */
    if (ctx->last) {
        op = ZSTD_e_end;

    } else if (ctx->flush) {
        op = ZSTD_e_flush;

    } else {
        op = ZSTD_e_continue;
    }

    ngx_log_debug8(NGX_LOG_DEBUG_HTTP, r->connection->log, 0,
                   "zstd compress in: src:%p pos:%uz size:%uz, "
                   "dst:%p pos:%uz size:%uz flush:%d last:%d",
                   ctx->buffer_in.src, ctx->buffer_in.pos, ctx->buffer_in.size,
                   ctx->buffer_out.dst, ctx->buffer_out.pos,
                   ctx->buffer_out.size, ctx->flush, ctx->last);

    pos_in = ctx->buffer_in.pos;
    pos_out = ctx->buffer_out.pos;

    rc = ZSTD_compressStream2(ctx->cstream, &ctx->buffer_out,
                              &ctx->buffer_in, op);

    if (ZSTD_isError(rc)) {
        ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                      "ZSTD_compressStream2(%s) failed: %s",
                      (op == ZSTD_e_end) ? "end"
                        : (op == ZSTD_e_flush) ? "flush" : "continue",
                      ZSTD_getErrorName(rc));

        return NGX_ERROR;
    }

    ngx_log_debug6(NGX_LOG_DEBUG_HTTP, r->connection->log, 0,
                   "zstd compress out: src:%p pos:%uz size:%uz, "
                   "dst:%p pos:%uz size:%uz",
                   ctx->buffer_in.src, ctx->buffer_in.pos, ctx->buffer_in.size,
                   ctx->buffer_out.dst, ctx->buffer_out.pos,
                   ctx->buffer_out.size);

    /*
     * Skip the pointer arithmetic when the compressor didn't consume / emit
     * anything. `in_buf->pos` may be NULL on a flush-only call (sentinel
     * buffer with no payload); C says NULL+0 is undefined behaviour even
     * when the delta is zero, which UBSan reports. Same logic for
     * `out_buf->last` on a continue op that produced no output. V1 fix
     * d6c73fd; the V2 refactor regressed it.
     */
    if (ctx->buffer_in.pos != pos_in) {
        ctx->in_buf->pos += ctx->buffer_in.pos - pos_in;
    }
    if (ctx->buffer_out.pos != pos_out) {
        ctx->out_buf->last += ctx->buffer_out.pos - pos_out;
    }

    /*
     * Zero-progress guard (see ngx_brotli's body-filter loop): in
     * ZSTD_e_continue mode the encoder is required to make forward
     * progress (per ZSTD_compressStream2 contract in zstd.h). If it
     * consumed no input and produced no output, treat this as a fatal
     * tripwire to avoid spinning the body-filter loop indefinitely.
     * Flush/end ops may legitimately produce no progress after the
     * buffer is fully drained (rc==0), so guard only the continue case.
     */
    if (op == ZSTD_e_continue
        && ctx->buffer_in.pos == pos_in
        && ctx->buffer_out.pos == pos_out
        && rc == 0)
    {
        ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                      "zstd compress made no progress, aborting");
        return NGX_ERROR;
    }

    /*
     * Unified post-compress handling. Two orthogonal axes decide what
     * we emit downstream:
     *
     *   has_signal -- is there a finish-op boundary to deliver? Yes iff
     *                 rc == 0 and op is ZSTD_e_flush / ZSTD_e_end
     *                 (libzstd reports the op as fully drained).
     *
     *   has_bytes  -- are there compressed bytes in out_buf?
     *
     * Cases:
     *   * !has_signal && !has_bytes -- return NGX_AGAIN. Either rc > 0
     *     (encoder still has bytes pending; outer loop will call back
     *     with a fresh out_buf via get_buf) or op was ZSTD_e_continue
     *     with rc == 0 (input consumed without spill; outer loop will
     *     call back with the next chain link). The zero-progress guard
     *     above already caught the pathological no-progress continue.
     *   * has_bytes (with or without signal) -- enqueue ctx->out_buf,
     *     attaching b->flush=1 / b->last_buf=1 if has_signal.
     *   * has_signal && !has_bytes -- enqueue a *separate* zero-size
     *     sync/flush buf (NOT ctx->out_buf, which is a recycled
     *     temporary; the write filter rejects zero-size temporary bufs
     *     via ngx_buf_special; see ngx_buf.h). Without this branch the
     *     flush/end boundary is invisible to the writer -- the
     *     production bug class this module's V2 refactor targets.
     */
    has_bytes  = (ngx_buf_size(ctx->out_buf) != 0);
    has_signal = (rc == 0 && (op == ZSTD_e_flush || op == ZSTD_e_end));

    if (!has_bytes && !has_signal) {
        return NGX_AGAIN;
    }

    cl = ngx_alloc_chain_link(r->pool);
    if (cl == NULL) {
        return NGX_ERROR;
    }

    if (has_bytes) {
        b = ctx->out_buf;
        ctx->bytes_out += ngx_buf_size(b);

    } else {
        /*
         * Zero-size signal-only buf. No memory flags; the writer's
         * ngx_buf_special() accepts (flush || last_buf || sync) only
         * when !in_memory && !in_file (see ngx_buf.h).
         */
        b = ngx_calloc_buf(r->pool);
        if (b == NULL) {
            return NGX_ERROR;
        }

        b->sync = 1;
    }

    /*
     * Shared finalize: when libzstd has fully drained the current
     * finish op (has_signal), attach the matching downstream marker
     * and clear the sticky flag so add_data can dequeue the next
     * chain link on the subsequent outer-loop pass.
     */
    if (has_signal) {
        r->connection->buffered &= ~NGX_HTTP_GZIP_BUFFERED;

        if (op == ZSTD_e_end) {
            b->last_buf = 1;
            ctx->done = 1;
            ctx->last = 0;

        } else {
            b->flush = 1;
            ctx->flush = 0;
        }
    }

    cl->next = NULL;
    cl->buf = b;

    *ctx->last_out = cl;
    ctx->last_out = &cl->next;

    if (has_bytes) {
        /*
         * Force the next iteration's ngx_http_zstd_filter_get_buf to
         * allocate (or recycle from ctx->free) a fresh out_buf: we've
         * just handed ctx->out_buf to the downstream chain, so the
         * partially-written buf MUST NOT be reused. _get_buf gates on
         * `buffer_out.pos < buffer_out.size`; zeroing forces the
         * "needs new buf" branch.
         */
        ngx_memzero(&ctx->buffer_out, sizeof(ZSTD_outBuffer));
    }

    return ctx->done ? NGX_OK : NGX_AGAIN;
}


static ngx_int_t
ngx_http_zstd_filter_add_data(ngx_http_request_t *r, ngx_http_zstd_ctx_t *ctx)
{
    ngx_chain_t  *cl;

    /*
     * Sticky-flag head-of-line: while ctx->last or ctx->flush is set we
     * have an in-flight finish op (ZSTD_e_end / ZSTD_e_flush) that has
     * not yet drained (rc > 0 from the previous compress call). Stay on
     * the current buffer_in (typically empty) so the caller re-enters
     * filter_compress to drain libzstd's internal buffer.
     */
    if (ctx->buffer_in.pos < ctx->buffer_in.size
        || ctx->flush
        || ctx->last)
    {
        return NGX_OK;
    }

    ngx_log_debug1(NGX_LOG_DEBUG_HTTP, r->connection->log, 0,
                   "zstd in: %p", ctx->in);

    if (ctx->in == NULL) {
        return NGX_DECLINED;
    }

    cl = ctx->in;
    ctx->in_buf = cl->buf;
    ctx->in = cl->next;
    ngx_free_chain(r->pool, cl);

    /*
     * Drop empty non-control bufs (brotli pattern, lines 478-487). An
     * empty buf with neither last_buf nor flush carries no information
     * but would otherwise still drive a useless ZSTD_e_continue call.
     * Return NGX_AGAIN so the outer add_data loop pulls the next link.
     */
    if (ngx_buf_size(ctx->in_buf) == 0
        && !ctx->in_buf->last_buf
        && !ctx->in_buf->flush)
    {
        return NGX_AGAIN;
    }

    /*
     * Set sticky flags immediately on consuming the buf. last_buf takes
     * precedence: a buf with both flags set transitions us directly to
     * the terminal end-of-stream op.
     */
    if (ctx->in_buf->last_buf) {
        ctx->last = 1;

    } else if (ctx->in_buf->flush) {
        ctx->flush = 1;
    }

    ctx->buffer_in.src = ctx->in_buf->pos;
    ctx->buffer_in.pos = 0;
    ctx->buffer_in.size = ngx_buf_size(ctx->in_buf);

    ctx->bytes_in += ngx_buf_size(ctx->in_buf);

    return NGX_OK;
}


static ngx_int_t
ngx_http_zstd_filter_get_buf(ngx_http_request_t *r, ngx_http_zstd_ctx_t *ctx)
{
    ngx_chain_t               *cl;
    ngx_http_zstd_loc_conf_t  *zlcf;

    if (ctx->buffer_out.pos < ctx->buffer_out.size) {
        return NGX_OK;
    }

    zlcf = ngx_http_get_module_loc_conf(r, ngx_http_zstd_filter_module);

    if (ctx->free) {
        cl = ctx->free;
        ctx->free = ctx->free->next;
        ctx->out_buf = cl->buf;
        ngx_free_chain(r->pool, cl);

        ctx->out_buf->flush = 0;
        ctx->out_buf->sync = 0;
        ctx->out_buf->last_buf = 0;
        ctx->out_buf->last_in_chain = 0;

    } else if (ctx->bufs < zlcf->bufs.num) {
        ctx->out_buf = ngx_create_temp_buf(r->pool, zlcf->bufs.size);
        if (ctx->out_buf == NULL) {
            return NGX_ERROR;
        }

        ctx->out_buf->tag = (ngx_buf_tag_t) &ngx_http_zstd_filter_module;
        ctx->out_buf->recycled = 1;
        ctx->bufs++;

    } else {
        ctx->nomem = 1;
        return NGX_DECLINED;
    }

    ctx->buffer_out.dst = ctx->out_buf->pos;
    ctx->buffer_out.pos = 0;
    ctx->buffer_out.size = ctx->out_buf->end - ctx->out_buf->pos;

    return NGX_OK;
}


static ZSTD_CStream *
ngx_http_zstd_filter_create_cstream(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx)
{
    size_t                      rc, ws_size, est;
    ZSTD_CStream               *cstream;
    ZSTD_customMem              cmem;
    ngx_pool_cleanup_t         *cln;
    ngx_http_zstd_loc_conf_t   *zlcf;
    ZSTD_compressionParameters  cparams;
    ngx_int_t                   apply_auto;
    unsigned long long          src_size;
    off_t                       log_cl;

    /*
     * Zero-init cparams + est so a strict toolchain at -O0 (coverage /
     * ASan builds) can't trip -Wmaybe-uninitialized on the conditional
     * apply_auto / cparams.windowLog reads below. The semantic guard is
     * apply_auto; these initializers are belt-and-braces.
     */
    ngx_memzero(&cparams, sizeof(cparams));
    est = 0;
    log_cl = (off_t) -1;
    src_size = ZSTD_CONTENTSIZE_UNKNOWN;

    zlcf = ngx_http_get_module_loc_conf(r, ngx_http_zstd_filter_module);

    /*
     * Auto-Window: shrink the per-request workspace when the response
     * Content-Length is known OR an operator cap is configured. The
     * derived cParams are applied to the CCtx via ZSTD_CCtx_setParameter
     * AFTER reset + refCDict / compressionLevel (see below). The
     * forward-compat guard refuses any cParams whose estimated workspace
     * exceeds zlcf->baseline_ws (the level-default cached at merge
     * time) — defends against future libzstd heuristic regressions that
     * could grow per-request memory.
     *
     * Activates when EITHER:
     *   (a) ctx->content_length_n >= 0 — body size known; used as the
     *       srcSize hint. In practice always >= 1: a known C-L == 0 is
     *       declined upstream in the header filter (C12-2), and
     *       ZSTD_getCParams(level, 0, 0) would treat srcSize == 0 as
     *       UNKNOWN (level-default cParams, e.g. windowLog 19 — not a
     *       WINDOWLOG_MIN clamp) anyway. Chunked-empty behaviour is
     *       documented in t/regression/test_empty_body.py;
     *   (b) zlcf->window_bits != NGX_CONF_UNSET — operator wants the
     *       cap to apply regardless of transfer encoding (e.g.
     *       Chrome-compat windowLog<=23 for chunked responses too).
     *       srcSize hint is ZSTD_CONTENTSIZE_UNKNOWN so libzstd uses
     *       level defaults that we then clamp.
     *
     * Otherwise (chunked / unknown C-L without operator cap): skip the
     * auto-tune path entirely. ws_size stays at the level-default
     * baseline and no setParameter calls are issued — status quo for
     * chunked responses.
     */
    ws_size = zlcf->baseline_ws;
    apply_auto = 0;

    /*
     * Dict + auto-window are mutually exclusive: when zstd_dict_file is
     * configured the CDict's baked cParams (chosen by libzstd at
     * ZSTD_createCDict_byReference time, tuned for the dict) drive the
     * compression. setParameter(windowLog/hashLog/chainLog) AFTER
     * refCDict would override that tuning, risking ratio regressions and
     * mis-sized workspace estimates (ZSTD_estimateCStreamSize_usingCParams
     * does not account for the dict-load overhead, which lands in the
     * same customAlloc bump chunk). Keep the level-default baseline and
     * skip cParams setParameter entirely on the dict path. Note the
     * skipped log line for observability.
     */
    if (zlcf->dict != NULL) {
        ngx_log_error(NGX_LOG_INFO, r->connection->log, 0,
                      "zstd auto-window: skipped (dict configured), "
                      "ws=baseline=%uz",
                      zlcf->baseline_ws);

    } else if (ctx->content_length_n >= 0
        || zlcf->window_bits != NGX_CONF_UNSET)
    {
        if (ctx->content_length_n >= 0) {
            src_size = (unsigned long long) ctx->content_length_n;
            log_cl = ctx->content_length_n;
        } else {
            src_size = ZSTD_CONTENTSIZE_UNKNOWN;
            log_cl = (off_t) -1;
        }

        cparams = ZSTD_getCParams((int) zlcf->level, src_size, 0);

        if (zlcf->window_bits != NGX_CONF_UNSET
            && (ngx_int_t) cparams.windowLog > zlcf->window_bits)
        {
            cparams.windowLog = (unsigned) zlcf->window_bits;
            /*
             * Do NOT manually re-derive hashLog/chainLog after capping
             * windowLog: libzstd applies an en-masse cParams adjust at
             * ZSTD_resetCCtx_internal time (called lazily on the first
             * compressStream2), which clamps hashLog/chainLog against
             * the new windowLog in one pass — see the setParameter
             * order rationale block around the windowLog/hashLog/
             * chainLog ZSTD_CCtx_setParameter calls below, plus
             * ZSTD_adjustCParams_internal in
             * tmp/src/zstd/lib/compress/zstd_compress.c lines
             * 1568-1583. Re-deriving them here would drift between
             * libzstd versions.
             */
        }

        est = ZSTD_estimateCStreamSize_usingCParams(cparams);
        if (!ZSTD_isError(est) && est <= zlcf->baseline_ws) {
            ws_size = est;
            apply_auto = 1;

            /*
             * Emit at NGX_LOG_INFO so the regression matrix (which
             * runs the non-debug nginx-mainline build from nginx.org)
             * can observe the chosen cParams via a configurable
             * error_log file. Tests grep this exact prefix; do not
             * change without updating t/regression/test_auto_window.py.
             * Production deployments that don't want per-request info
             * lines can raise error_log to `notice` or above.
             */
            ngx_log_error(NGX_LOG_INFO, r->connection->log, 0,
                          "zstd auto-window: cl=%O wlog=%ud hlog=%ud "
                          "clog=%ud ws=%uz baseline=%uz",
                          log_cl,
                          cparams.windowLog, cparams.hashLog,
                          cparams.chainLog, est, zlcf->baseline_ws);
        } else {
            /*
             * Forward-compat fallback: triggered when
             * est > zlcf->baseline_ws (auto-tune would GROW the
             * workspace vs the level baseline cached at merge time) or
             * the estimator itself errored on the derived cParams. With
             * the libzstd versions pinned by our matrix this branch is
             * unreachable — ZSTD_adjustCParams_internal only downsizes
             * cParams relative to level defaults. The branch defends
             * against a hypothetical future libzstd heuristic regression
             * that returns LARGER cParams for the same level+srcSize.
             * On fallback ws_size stays at baseline_ws and apply_auto
             * stays 0, so the CStream initialises with level defaults
             * — identical to pre-auto-tune behaviour. Emit at WARN so
             * a sustained stream of these lines is operator-visible
             * (would indicate a libzstd version-drift requiring action).
             */
            if (ZSTD_isError(est)) {
                ngx_log_error(NGX_LOG_WARN, r->connection->log, 0,
                              "zstd auto-window: forward-compat fallback "
                              "(cl=%O est=ERROR(name=%s) baseline=%uz "
                              "isError=1), using level defaults",
                              log_cl,
                              ZSTD_getErrorName(est), zlcf->baseline_ws);
            } else {
                ngx_log_error(NGX_LOG_WARN, r->connection->log, 0,
                              "zstd auto-window: forward-compat fallback "
                              "(cl=%O est=%uz baseline=%uz isError=0), "
                              "using level defaults",
                              log_cl,
                              est, zlcf->baseline_ws);
            }
        }
    } else {
        ngx_log_error(NGX_LOG_INFO, r->connection->log, 0,
                      "zstd auto-window: skipped (no content_length_n, "
                      "no cap), ws=baseline=%uz",
                      zlcf->baseline_ws);
    }

    ws_size += NGX_HTTP_ZSTD_WORKSPACE_HEADROOM;

    ctx->preallocated = ngx_palloc(r->pool, ws_size);
    if (ctx->preallocated == NULL) {
        return NULL;
    }
    ctx->free_mem = ctx->preallocated;
    ctx->allocated = ws_size;

    cmem.customAlloc = ngx_http_zstd_filter_alloc;
    cmem.customFree = ngx_http_zstd_filter_free;
    cmem.opaque = ctx;

    cstream = ZSTD_createCStream_advanced(cmem);
    if (cstream == NULL) {
        ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                      "ZSTD_createCStream_advanced() failed");

        return NULL;
    }

    /*
     * Safety net for the abort path: if the request finalizes before
     * ctx->done flips (client RST, upstream error, finalize from another
     * module mid-compression), this cleanup handler runs ZSTD_freeCStream
     * on the still-alive CStream. Mirrors the CDict cleanup pattern from
     * commit 3a2c597. The fast/done path nulls ctx->cstream after
     * ZSTD_freeCStream, so the handler is a no-op in that case and
     * cannot double-free.
     */
    cln = ngx_pool_cleanup_add(r->pool, 0);
    if (cln == NULL) {
        (void) ngx_http_zstd_filter_release_workspace(r, ctx, cstream,
                                                     NGX_LOG_ALERT);
        return NULL;
    }
    cln->handler = ngx_http_zstd_filter_cleanup;
    cln->data = ctx;

    /*
     * ctx->cstream is still NULL here; the caller assigns the return
     * value. The cleanup handler checks ctx->cstream != NULL, so any
     * teardown before assignment is a no-op.
     */

    /*
     * Modern (libzstd >= 1.4.0) init sequence: explicit session-only
     * reset followed by either a CDict ref or an explicit compression
     * level parameter. Replaces the legacy ZSTD_initCStream /
     * ZSTD_initCStream_usingCDict path; equivalent on a freshly created
     * CCtx (no advanced parameters set), differing only in the frame
     * header windowSize encoding choice which has no decoder impact.
     */
    rc = ZSTD_CCtx_reset(cstream, ZSTD_reset_session_only);
    if (ZSTD_isError(rc)) {
        ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                      "ZSTD_CCtx_reset() failed: %s",
                      ZSTD_getErrorName(rc));

        goto failed;
    }

    if (zlcf->dict) {
        rc = ZSTD_CCtx_refCDict(cstream, zlcf->dict);
        if (ZSTD_isError(rc)) {
            ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                          "ZSTD_CCtx_refCDict() failed: %s",
                          ZSTD_getErrorName(rc));

            goto failed;
        }

    } else {
        rc = ZSTD_CCtx_setParameter(cstream, ZSTD_c_compressionLevel,
                                    (int) zlcf->level);
        if (ZSTD_isError(rc)) {
            ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                          "ZSTD_CCtx_setParameter(compressionLevel) "
                          "failed: %s",
                          ZSTD_getErrorName(rc));

            goto failed;
        }
    }

    /*
     * Auto-Window: apply the auto-derived cParams. Order: windowLog
     * first (others depend on it), then hashLog, then chainLog.
     * libzstd's per-param validation accepts intermediate states
     * because cParams clamping happens en-masse at
     * ZSTD_resetCCtx_internal time (called lazily on the first
     * compressStream2), not at each setParameter call. The customAlloc
     * workspace allocation happens lazily at reset time too, AFTER
     * these setParameter calls — so the cParams set here drive the
     * actual workspace demand, matching our ws_size estimate.
     */
    if (apply_auto) {
        rc = ZSTD_CCtx_setParameter(cstream, ZSTD_c_windowLog,
                                    (int) cparams.windowLog);
        if (ZSTD_isError(rc)) {
            ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                          "ZSTD_CCtx_setParameter(windowLog=%ud) failed: %s",
                          cparams.windowLog, ZSTD_getErrorName(rc));
            goto failed;
        }
        rc = ZSTD_CCtx_setParameter(cstream, ZSTD_c_hashLog,
                                    (int) cparams.hashLog);
        if (ZSTD_isError(rc)) {
            ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                          "ZSTD_CCtx_setParameter(hashLog=%ud) failed: %s",
                          cparams.hashLog, ZSTD_getErrorName(rc));
            goto failed;
        }
        rc = ZSTD_CCtx_setParameter(cstream, ZSTD_c_chainLog,
                                    (int) cparams.chainLog);
        if (ZSTD_isError(rc)) {
            ngx_log_error(NGX_LOG_ALERT, r->connection->log, 0,
                          "ZSTD_CCtx_setParameter(chainLog=%ud) failed: %s",
                          cparams.chainLog, ZSTD_getErrorName(rc));
            goto failed;
        }
    }

    return cstream;

failed:

    /*
     * Disarm the registered cleanup handler before the helper frees
     * cstream: handler==NULL is defined as no-op (see
     * src/core/ngx_palloc.c:ngx_destroy_pool). Explicit disarm avoids
     * any dependence on ctx->cstream observation order.
     */
    cln->handler = NULL;

    (void) ngx_http_zstd_filter_release_workspace(r, ctx, cstream,
                                                 NGX_LOG_ALERT);
    return NULL;
}


static ngx_uint_t
ngx_http_zstd_quantity(u_char *p, u_char *last)
{
    u_char      c;
    ngx_uint_t  n, q;

    /*
     * Parses a q-value per RFC 9110 section 12.4.2 / 5.3.1:
     *     qvalue = ( "0" [ "." 0*3DIGIT ] ) / ( "1" [ "." 0*3("0") ] )
     * Returns 0 for invalid/zero q-values, non-zero for any positive q.
     * The non-zero magnitude is not a faithful percentage -- digit place
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
 * first-accept-wins here -- sufficient for V1). Default (no q) = accept.
 *
 * Modelled on nginx's ngx_http_gzip_accept_encoding() in
 * ngx_http_core_module.c with two
 * deviations: (1) iterate past q=0 to find another zstd token instead of
 * returning DECLINED on first match; (2) accept TAB as token whitespace
 * (RFC 9110 OWS).
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
     * (e.g. past "zstdx" -> 'x'), so the inner search loop validates the
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
         * otherwise this is "zstdx" / "zstd-future" etc. -- skip past it
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
                /* unexpected token after whitespace -- not our match */
                start = p;
                continue;
            }
            /* fall through to ';' handling */
        } else if (*p != ';') {
            /* not a token boundary -- false match (e.g. "zstdx") */
            start = p;
            continue;
        }

        /* parameter section: ";" *( OWS ";" OWS parameter ) -- we only
         * care about q= */

        p++;  /* skip ';' */

        while (p < last && (*p == ' ' || *p == '\t')) {
            p++;
        }

        if (p == last) {
            return NGX_OK;
        }

        if (*p != 'q' && *p != 'Q') {
            /* non-q parameter -- RFC 9110 allows other params; treat as
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

    if (ngx_http_zstd_accept_encoding(&ae->value) != NGX_OK) {
        return NGX_DECLINED;
    }


    r->gzip_tested = 1;
    r->gzip_ok = 0;

    return NGX_OK;
}


static void *
ngx_http_zstd_create_main_conf(ngx_conf_t *cf)
{
    ngx_http_zstd_main_conf_t  *zmcf;

    zmcf = ngx_pcalloc(cf->pool, sizeof(ngx_http_zstd_main_conf_t));
    if (zmcf == NULL) {
        return NULL;
    }

    return zmcf;
}


static char *
ngx_http_zstd_init_main_conf(ngx_conf_t *cf, void *conf)
{
    ngx_http_zstd_main_conf_t *zmcf = conf;

    if (zmcf->dict_file.len == 0) {
        return NGX_CONF_OK;
    }

    if (ngx_conf_full_name(cf->cycle, &zmcf->dict_file, 1) != NGX_OK) {
        return NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}


static void *
ngx_http_zstd_create_loc_conf(ngx_conf_t *cf)
{
    ngx_http_zstd_loc_conf_t  *conf;

    conf = ngx_pcalloc(cf->pool, sizeof(ngx_http_zstd_loc_conf_t));
    if (conf == NULL) {
        return NULL;
    }

    /*
     * set by ngx_pcalloc():
     *
     *    conf->bufs.num = 0;
     *    conf->types = { NULL };
     *    conf->types_keys = NULL;
     *    conf->dict = NULL;
     *    conf->baseline_ws = 0;
     */

    conf->enable = NGX_CONF_UNSET;
    conf->level = NGX_CONF_UNSET;
    conf->min_length = NGX_CONF_UNSET;
    conf->window_bits = NGX_CONF_UNSET;

    return conf;
}


static char *
ngx_http_zstd_merge_loc_conf(ngx_conf_t *cf, void *parent, void *child)
{
    ngx_http_zstd_loc_conf_t *prev = parent;
    ngx_http_zstd_loc_conf_t *conf = child;

    ngx_fd_t                    fd;
    size_t                      size;
    size_t                      est;
    ssize_t                     n;
    char                       *rc;
    u_char                     *buf;
    ngx_file_info_t             info;
    ngx_http_zstd_main_conf_t  *zmcf;
    ngx_pool_cleanup_t         *cln;

    rc = NGX_CONF_OK;
    buf = NULL;
    fd = NGX_INVALID_FILE;

    ngx_conf_merge_value(conf->enable, prev->enable, 0);
    ngx_conf_merge_value(conf->level, prev->level, 1);
    ngx_conf_merge_value(conf->min_length, prev->min_length, 20);
    /*
     * The NGX_CONF_UNSET sentinel intentionally propagates to runtime
     * so create_cstream can distinguish "no operator cap configured"
     * (skip windowLog setParameter) from "cap set" (clamp wlog at
     * runtime). A numeric merge default here would force the cap
     * everywhere and lose the dict-path / no-cap branch in
     * ngx_http_zstd_filter_create_cstream.
     */
    ngx_conf_merge_value(conf->window_bits, prev->window_bits, NGX_CONF_UNSET);

    /*
     * Cache the level-default workspace estimate. Used by the
     * Auto-Window path (Task 2) as both the fallback workspace size
     * (when content_length_n is unknown and no operator cap is set)
     * AND the forward-compat ceiling that refuses any auto-derived
     * cParams whose estimated workspace exceeds the level-default
     * baseline. Computed once at config time so the per-request hot
     * path never re-estimates. On ZSTD_isError we fall back to the
     * level=1 baseline and warn — should be unreachable with a
     * sane level already validated by ngx_http_zstd_comp_level()
     */
    est = ZSTD_estimateCStreamSize((int) conf->level);
    if (ZSTD_isError(est)) {
        ngx_conf_log_error(NGX_LOG_WARN, cf, 0,
                           "ZSTD_estimateCStreamSize(%i) failed: %s, "
                           "falling back to level=1 baseline",
                           conf->level, ZSTD_getErrorName(est));
        /* Level 1 matches the merge_value default at conf->level above. */
        est = ZSTD_estimateCStreamSize(1);
        if (ZSTD_isError(est)) {
            /* Extremely unlikely; refuse rather than store 0. */
            ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                               "ZSTD_estimateCStreamSize(1) failed: %s",
                               ZSTD_getErrorName(est));
            return NGX_CONF_ERROR;
        }
    }
    conf->baseline_ws = est;

    if (ngx_http_merge_types(cf, &conf->types_keys, &conf->types,
                             &prev->types_keys, &prev->types,
                             ngx_http_html_default_types))
    {
        return NGX_CONF_ERROR;
    }

    /*
     * No ngx_conf_merge_ptr_value(conf->dict, ...) here: conf->dict
     * is initialised to NULL by ngx_pcalloc (not NGX_CONF_UNSET_PTR),
     * so the macro would never copy from the parent. The real
     * dict-merge runs below as three mutually exclusive paths:
     *   1. parent loaded a dict AND level matches → reuse parent CDict
     *      via assignment `conf->dict = prev->dict`;
     *   2. parent loaded a dict but level differs → load a fresh dict
     *      from file (separate CDict pinned to this level);
     *   3. parent->dict == NULL (parent block was `zstd off` and never
     *      loaded the dict, even though zstd_dict_file is configured
     *      at main-conf) → load a fresh dict from file regardless of
     *      level match. Without this third path the configured dict
     *      is silently dropped on the http→server(off)→location(on)
     *      shape (codex3 P2.1).
     */
    ngx_conf_merge_bufs_value(conf->bufs, prev->bufs,
                              (128 * 1024) / ngx_pagesize, ngx_pagesize);

    zmcf = ngx_http_conf_get_module_main_conf(cf, ngx_http_zstd_filter_module);

    if (conf->enable && zmcf->dict_file.len > 0) {

        if (conf->level == prev->level && prev->dict != NULL) {
            conf->dict = prev->dict;

        } else {
            /*
             * Either compression level differs from the outer block, or
             * the outer block never loaded the dict (prev->dict == NULL
             * because parent was `zstd off`). In both cases we load a
             * fresh CDict from the configured file.
             */

            fd = ngx_open_file(zmcf->dict_file.data, NGX_FILE_RDONLY,
                               NGX_FILE_OPEN, 0);

            if (fd == NGX_INVALID_FILE) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, ngx_errno,
                                   ngx_open_file_n " \"%V\" failed",
                                   &zmcf->dict_file);

                return NGX_CONF_ERROR;
            }

            if (ngx_fd_info(fd, &info) == NGX_FILE_ERROR) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, ngx_errno,
                                   ngx_fd_info_n " \"%V\" failed",
                                   &zmcf->dict_file);

                rc = NGX_CONF_ERROR;
                goto close;
            }

            size = ngx_file_size(&info);
            /*
             * `buf` must outlive `conf->dict`: ZSTD_createCDict_byReference
             * below stores a non-owning pointer to these bytes inside the
             * CDict, so libzstd will read from `buf` for the entire lifetime
             * of the dict (i.e. until our pool cleanup runs ZSTD_freeCDict).
             * Both allocations live on cf->pool and cleanups run before
             * palloc'd memory is released, so the lifetime contract is held.
             * Do not move `buf` to a shorter-lived pool without also
             * removing `_byReference` (use ZSTD_createCDict instead, which
             * copies the dict bytes internally).
             */
            buf = ngx_palloc(cf->pool, size);
            if (buf == NULL) {
                rc = NGX_CONF_ERROR;
                goto close;
            }

            n = ngx_read_fd(fd, (void *) buf, size);
            if (n < 0) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, ngx_errno,
                                   ngx_read_fd_n " %V\" failed",
                                   &zmcf->dict_file);

                rc = NGX_CONF_ERROR;
                goto close;

            } else if ((size_t) n != size) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, ngx_errno,
                                   ngx_read_fd_n "\"%V incomplete\"",
                                   &zmcf->dict_file);

                rc = NGX_CONF_ERROR;
                goto close;
            }

            conf->dict = ZSTD_createCDict_byReference(buf, size, conf->level);
            if (conf->dict == NULL) {
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                                   "ZSTD_createCDict_byReference() failed");
                rc = NGX_CONF_ERROR;
                goto close;
            }

            /*
             * Register a pool cleanup so the CDict is freed when the
             * configuration cycle is destroyed (e.g. on `nginx -s reload`).
             * Without this, every reload that re-parses zstd_dict_file
             * leaks one CDict allocation for the lifetime of the master
             * process. Quantification deferred to V2 (ASan in CI).
             */
            cln = ngx_pool_cleanup_add(cf->pool, 0);
            if (cln == NULL) {
                ZSTD_freeCDict(conf->dict);
                conf->dict = NULL;
                ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                                   "ngx_pool_cleanup_add() failed");
                rc = NGX_CONF_ERROR;
                goto close;
            }

            cln->handler = ngx_http_zstd_cdict_cleanup;
            cln->data = conf->dict;
        }
    }

close:

    if (fd != NGX_INVALID_FILE && ngx_close_file(fd) == NGX_FILE_ERROR) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, ngx_errno,
                           ngx_close_file_n " \"%V\" failed",
                           &zmcf->dict_file);

        rc = NGX_CONF_ERROR;
    }

    return rc;
}


static ngx_int_t
ngx_http_zstd_filter_init(ngx_conf_t *cf)
{
    ngx_http_next_header_filter = ngx_http_top_header_filter;
    ngx_http_top_header_filter = ngx_http_zstd_header_filter;

    ngx_http_next_body_filter = ngx_http_top_body_filter;
    ngx_http_top_body_filter = ngx_http_zstd_body_filter;

    return NGX_OK;
}


static void *
ngx_http_zstd_filter_alloc(void *opaque, size_t size)
{
    ngx_http_zstd_ctx_t  *ctx = opaque;
    void                 *p;
    size_t                aligned;

    /*
     * Direction A: bump allocator backed by ctx->preallocated. libzstd's
     * sub-allocations are sub-buffers of the workspace estimate; aligning
     * each request up to NGX_ALIGNMENT (gzip filter mimic, gzip uses
     * 8-byte alignment) keeps subsequent returned pointers aligned for
     * the strictest type libzstd's internals use. If the bump exceeds
     * the budget (libzstd version drift past ZSTD_estimateCStreamSize's
     * promise, CDict refCDict copy-fallback, unusual advanced params),
     * fall back to a pool allocation and log at ALERT. The fallback
     * chunk lands on r->pool->large and is NOT freed by the eager
     * ngx_pfree(preallocated) on the done/failed/cleanup paths, so the
     * Direction A early-release goal is partially defeated for that
     * request. ALERT level is chosen so production ops alert by default;
     * a sustained ALERT stream means the headroom in
     * NGX_HTTP_ZSTD_WORKSPACE_HEADROOM should be raised, or a per-ctx
     * fallback tracking list added (see plan-followup).
     * test_workspace_no_fallback asserts the log line stays absent under
     * the supported config matrix.
     */
    aligned = ngx_align(size, NGX_ALIGNMENT);
    if (aligned <= ctx->allocated) {
        p = ctx->free_mem;
        ctx->free_mem += aligned;
        ctx->allocated -= aligned;

        ngx_log_debug3(NGX_LOG_DEBUG_HTTP, ctx->request->connection->log, 0,
                       "zstd alloc (bump): %p, size:%uz aligned:%uz",
                       p, size, aligned);

        return p;
    }

    ngx_log_error(NGX_LOG_ALERT, ctx->request->connection->log, 0,
                  "zstd workspace exhausted, fallback (req=%uz remaining=%uz)",
                  size, ctx->allocated);

    p = ngx_palloc(ctx->request->pool, size);

    ngx_log_debug2(NGX_LOG_DEBUG_HTTP, ctx->request->connection->log, 0,
                   "zstd alloc (fallback): %p size:%uz", p, size);

    return p;
}


static ngx_int_t
ngx_http_zstd_add_variables(ngx_conf_t *cf)
{
    ngx_http_variable_t  *v;

    v = ngx_http_add_variable(cf, &ngx_http_zstd_ratio,
                              NGX_HTTP_VAR_NOCACHEABLE);
    if (v == NULL) {
        return NGX_ERROR;
    }

    v->get_handler = ngx_http_zstd_ratio_variable;

    return NGX_OK;
}


static ngx_int_t
ngx_http_zstd_ratio_variable(ngx_http_request_t *r,
    ngx_http_variable_value_t *vv, uintptr_t data)
{
    ngx_uint_t            ratio_int, ratio_frac;
    ngx_http_zstd_ctx_t  *ctx;

    ctx = ngx_http_get_module_ctx(r, ngx_http_zstd_filter_module);
    if (ctx == NULL || !ctx->done || ctx->bytes_out == 0) {
        vv->not_found = 1;
        return NGX_OK;
    }

    vv->data = ngx_pnalloc(r->pool, NGX_INT_T_LEN + 4);
    if (vv->data == NULL) {
        return NGX_ERROR;
    }

    ratio_int = (ngx_uint_t) ctx->bytes_in / ctx->bytes_out;
    ratio_frac = (ngx_uint_t) ((uint64_t) ctx->bytes_in * 1000
                                / ctx->bytes_out % 1000);

    vv->len = ngx_sprintf(vv->data, "%ui.%03ui", ratio_int, ratio_frac)
              - vv->data;

    vv->valid = 1;
    vv->no_cacheable = 1;

    return NGX_OK;
}


static void
ngx_http_zstd_filter_free(void *opaque, void *address)
{
    /*
     * Direction A: deliberate no-op (matches nginx gzip filter, see
     * ngx_http_gzip_filter_free in ngx_http_gzip_filter_module.c).
     * The bump allocator hands out sub-buffers of ctx->preallocated;
     * tracking per-sub-buffer frees and trying to compact / reuse them
     * would replicate a malloc inside r->pool with no measurable benefit
     * (libzstd doesn't churn intermediate allocations in quantities that
     * justify the bookkeeping). The whole workspace is released in one
     * ngx_pfree call after ZSTD_freeCStream returns.
     */
#if (NGX_DEBUG)

    ngx_http_zstd_ctx_t *ctx = opaque;

    ngx_log_debug1(NGX_LOG_DEBUG_HTTP, ctx->request->connection->log, 0,
                   "zstd free (no-op): %p", address);

#endif
}


static ngx_int_t
ngx_http_zstd_filter_release_workspace(ngx_http_request_t *r,
    ngx_http_zstd_ctx_t *ctx, ZSTD_CStream *cstream, ngx_uint_t err_level)
{
    size_t      rv;
    ngx_int_t   rc;

    /*
     * Direction A teardown sequence shared by body_filter done/failed,
     * create_cstream cleanup-add-NULL, and create_cstream failed: label.
     *
     * Ordering rationale: ZSTD_freeCStream may invoke our customFree
     * callback during internal teardown, so ctx->preallocated MUST
     * remain valid until after ZSTD_freeCStream returns. Nulling
     * ctx->cstream BEFORE ngx_pfree lets the pool-cleanup safety net
     * handler treat the request as fast-path-done and skip a second
     * ZSTD_freeCStream call. Tolerates cstream == NULL and
     * ctx->preallocated == NULL (partial teardown).
     */
    rc = NGX_OK;

    if (cstream != NULL) {
        rv = ZSTD_freeCStream(cstream);
        if (ctx->cstream == cstream) {
            ctx->cstream = NULL;
        }
        if (ZSTD_isError(rv)) {
            ngx_log_error(err_level, r->connection->log, 0,
                          "ZSTD_freeCStream() failed: %s",
                          ZSTD_getErrorName(rv));
            rc = NGX_ERROR;
        }
    }

    if (ctx->preallocated != NULL) {
        ngx_pfree(r->pool, ctx->preallocated);
        ctx->preallocated = NULL;
    }

    return rc;
}


static char *
ngx_http_zstd_comp_level(ngx_conf_t *cf, void *post, void *data)
{
    ngx_int_t  *np = data;
    ngx_int_t   min_level;

    /*
     * ZSTD_minCLevel() requires libzstd >= 1.3.6; the file-level
     * #error guard enforces >= 1.4.0, so this call is unconditionally
     * available.
     */
    min_level = (ngx_int_t) ZSTD_minCLevel();

    if (*np == 0 || *np < min_level || *np > ZSTD_maxCLevel()) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                           "zstd compress level must between %i and %i "
                           "excluding 0",
                           min_level, (ngx_int_t) ZSTD_maxCLevel());

        return NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}


static char *
ngx_http_zstd_window_bits(ngx_conf_t *cf, void *post, void *data)
{
    ngx_int_t   *np = data;
    ngx_int_t    lower, upper;
    ZSTD_bounds  bounds;

    /*
     * Query the runtime libzstd for the supported windowLog range.
     * Using ZSTD_cParam_getBounds (stable API since 1.4.0) instead of
     * the compile-time ZSTD_WINDOWLOG_{MIN,MAX} macros so dynamic-link
     * builds report the bounds of the actually-loaded library, and so
     * the 32-bit vs 64-bit upper-bound difference is reported faithfully
     * without our own sizeof(size_t) branch. On the unlikely event that
     * the runtime query fails we fall back to the compile-time macros
     * (defensive; unreachable with stable libzstd).
     */
    bounds = ZSTD_cParam_getBounds(ZSTD_c_windowLog);
    if (ZSTD_isError(bounds.error)) {
        lower = ZSTD_WINDOWLOG_MIN;
        upper = ZSTD_WINDOWLOG_MAX;
    } else {
        lower = bounds.lowerBound;
        upper = bounds.upperBound;
    }

    if (*np < lower || *np > upper) {
        ngx_conf_log_error(NGX_LOG_EMERG, cf, 0,
                           "zstd_window_bits must be between %i and %i",
                           lower, upper);
        return NGX_CONF_ERROR;
    }

    return NGX_CONF_OK;
}


static char *
ngx_conf_zstd_set_num_slot_with_negatives(ngx_conf_t *cf,
    ngx_command_t *cmd, void *conf)
{
    char  *p = conf;

    ngx_int_t        *np;
    ngx_str_t        *value;
    ngx_conf_post_t  *post;


    np = (ngx_int_t *) (p + cmd->offset);

    if (*np != NGX_CONF_UNSET) {
        return "is duplicate";
    }

    value = cf->args->elts;

    if (*(value[1].data) == '-') {
        /* Parse ignoring the leading '-' character */
        *np = ngx_atoi(value[1].data + 1, value[1].len - 1);

        /*
         * NGX_ERROR is -1 so we need to check for that before making the
         * parsed result negative
         */
        if (*np == NGX_ERROR) {
            return "invalid number";
        }

        *np = -*np;
    } else {
        *np = ngx_atoi(value[1].data, value[1].len);

        if (*np == NGX_ERROR) {
            return "invalid number";
        }
    }

    if (cmd->post) {
        post = cmd->post;
        return post->post_handler(cf, post, np);
    }

    return NGX_CONF_OK;
}


static void
ngx_http_zstd_cdict_cleanup(void *data)
{
    /*
     * `data` is `cln->data` from ngx_pool_cleanup_add, set to a non-NULL
     * conf->dict at registration (see ngx_http_zstd_dict_file). NULL is
     * impossible by construction, and ZSTD_freeCDict(NULL) is documented
     * as a no-op anyway, so no NULL guard is needed.
     */
    ZSTD_freeCDict(data);
}


static void
ngx_http_zstd_filter_cleanup(void *data)
{
    /*
     * Direction A abort-path safety net: pool teardown is the only call
     * site guaranteed to fire once ctx is installed, so this handler
     * runs ZSTD_freeCStream when the body filter never reached its
     * done/failed branches (upstream finalize, downstream filter
     * rejection, client RST mid-stream, etc.). The fast path nulls
     * ctx->cstream before pool teardown so this becomes a no-op.
     *
     * No ngx_pfree on ctx->preallocated here: ngx_http_free_request
     * (src/http/ngx_http_request.c) sets `r->pool = NULL` BEFORE
     * calling ngx_destroy_pool ("to increase probability to catch
     * double close of request"), so by the time this pool cleanup
     * fires, ctx->request->pool is NULL and the chunk pointer needed
     * by ngx_pfree to walk pool->large is unreachable. The chunk gets
     * reclaimed microseconds later by ngx_destroy_pool's own
     * large-list walk; the abort-path symmetry with the fast path's
     * eager ngx_pfree is intentionally NOT replicated here. See
     * .claude/CLAUDE.md "Filter pipeline" for the full rationale.
     */
    ngx_http_zstd_ctx_t  *ctx = data;

    if (ctx->cstream != NULL) {
        ZSTD_freeCStream(ctx->cstream);
        ctx->cstream = NULL;
    }
}
