#!/usr/bin/env python3
"""XFreeFa proxy v3.1 — fast no-VPN backend for the Persian XFree app.

v3.2 (2026-11-22):
  * GOOGLE LOGIN FIX: googleURL/appleURL (the OAuth redirect_uri
    the "Continue with Google/Apple" button sends) are NEVER
    rewritten to the proxy origin. v3.1 rewrote them, so Google
    rejected login with 400 redirect_uri_mismatch — proved live:
    the proxy redirect_uri 302s to Google's /signin/oauth/error
    (redirect_uri_mismatch, naming the proxy URL); the
    www.xfree.com redirect_uri 302s to the Google sign-in page.
    The callback itself still lands on the proxy (the /g/ relay
    rewrites Google's Location to proxy-relative, and the app
    intercepts any direct navigation there), so the callback
    page stays same-origin and its postMessage reaches the login
    modal (cross-origin messages are ignored by the modal).
  * SPEED: pool acquire no longer spins up to 5s waiting for a
    free slot under a ~30-request burst — an ephemeral connection
    is opened at once instead (a fresh TLS handshake ~0.4s beats
    waiting seconds). Video streaming chunks 1 MB -> 2 MB
    (fewer write syscalls on the 0.1-CPU free instance). The
    server was already threaded (ThreadingHTTPServer, backlog
    128) — verified, no change needed there.
  * APP (WebActivity.java): video detail pages prefetch the first
    1 MB (unmetered networks only) and serve it from memory when
    the player asks; OAuth callback navigations are intercepted
    to the proxy; the login popup uses the same intercepting
    client as the main WebView, so its cookies flow back.

v3.1 (2026-11-22):
  * MOBILE LAYOUT: the proxy used to fetch the site with a DESKTOP
    Chrome UA, so the site served its DESKTOP layout (no hamburger,
    no mobile bottom nav, no top carousel, wide grid) — the app on a
    phone showed a different site than the user's browser. Now the
    client's real User-Agent is forwarded (mobile WebView UA), so the
    site serves its MOBILE layout. Page cache is keyed by UA class.
  * VIDEO: Range streams now use the pooled keep-alive connection to
    cdn.xfree.com (was: a fresh TLS handshake per Range request via
    urllib) with 1 MB chunks (was 256 KB).
  * GOOGLE/APPLE LOGIN: all OAuth/asset hosts are relayed under /g/
    (accounts.google.com, oauth2.googleapis.com, apis.google.com,
    ssl/fonts.gstatic.com, translate.google.com, appleid.*, ...)
    because they load DIRECT in the WebView and are filtered in Iran.
  * PERSIAN UI: the site has NO fa locale (11 languages: en/de/es/
    fr/it/pt/cs/pl/ko/ja/zh — verified via /api/settings), so the
    Persian screen in the user's browser is Chrome's page
    translation. The proxy now injects Google Translate Element
    (auto fa) into HTML pages, so the app shows the site in Persian
    like the user's translated browser view.
  * /upload 404 and /settings 302->/login are the SITE's own answers
    for anonymous users, not proxy gaps (verified live).


Runs on Render free tier (or anywhere with Python 3, zero dependencies —
only the standard library, so there is nothing to pip-install).

Why this exists: xfree.com / thumbs.xfree.com / cdn.xfree.com are filtered
in Iran, so the phone cannot reach them directly with VPN off. The phone
talks ONLY to this service (onrender.com) and this service — running
outside Iran — fetches the bytes and streams them back.

v3.0 SPEED (measured 2026-11-22, warm proxy):
  BEFORE:  / 2.44s (568 KB uncompressed), /playlist 1.23s, /search 1.80s,
           /_nuxt/ bundle 0.8-1.15s EACH, thumb 0.68s. Every request did
           a fresh TCP+TLS handshake to upstream (urllib opens a new
           connection per request) and upstream sent pages uncompressed.
  AFTER:   keep-alive connection pool (up to 6 reused TLS connections per
           upstream host), upstream gzip (568 KB page -> ~130 KB on the
           wire), in-memory cache for anonymous HTML pages (120s) and
           /_nuxt/ assets (3600s), Jina removed from the hot path
           (it was unreachable from Render and wasted time on every
           API JSON body >= 1000 bytes — looks_like_page() is False for
           JSON, so the old code fired a doomed Jina fetch on API calls).
           First byte of a cached homepage is served from memory.

Endpoints (all GET unless noted):
  /health            -> "ok" (light wake probe; root / is the site now)
  /                  reverse proxy: the real xfree.com homepage
  /page?path=/...    site page HTML, direct-first with Jina fallback
                     (legacy; the app uses the reverse proxy now)
  /img?u=<url>       thumbnail from thumbs.xfree.com (pooled, cached headers)
  /static?u=<url>    static.xfree.com asset; .js/.css come from the FAST
                     www.xfree.com /_nuxt/ path instead (static host is
                     ~25s/file from Render's IP, www is ~0.8s)
  /video?u=<mp4 url> video from cdn.xfree.com, streamed, HTTP Range
                     passthrough (206) so seeking/resume work
  /x-scraper/<path>  relay to scraper.xfree.com (site's scraper API —
                     the Nuxt config points the browser straight at
                     scraper.xfree.com, which is filtered in Iran)
  /x-upload/<path>   relay to upload.xfree.com/media-api (video upload —
                     same leak: the site POSTs uploads there directly)
  /* (any other path) reverse proxy for the whole site:
                     GET /_nuxt/..., /api/..., /video?id=..., /playlist,
                     /search, /<username> ... forwarded to
                     https://www.xfree.com + path, Cookie / apiversion /
                     Set-Cookie forwarded, absolute www/thumbs/cdn/
                     static/scraper/upload URLs rewritten to this proxy,
                     redirects rewritten to this origin. POSTs to /api/*
                     forwarded the same way (login, follow, ...).
  POST /auth         JSON relay for login/signup (v2.4, unchanged)
  GET  /auth?url=... cookie-authenticated GET relay (v2.4, unchanged)
  POST /upload?url=<upload host> raw multipart relay (v2.3, unchanged)
  GET  /diag?url=... diagnostic only (upstream status/headers)

Security: only these hosts are allowed (anything else -> 403), so the
proxy cannot be abused as an open proxy:
  thumbs.xfree.com, cdn.xfree.com, www.xfree.com, xfree.com,
  static.xfree.com, scraper.xfree.com, upload.xfree.com, r.jina.ai

Limits (honest): Render free sleeps after ~15 min idle; the first
request after sleep takes ~30-60s to wake the service (the app loads
the site immediately and auto-retries; the keep-alive cron pings every
12 min 07:00-23:00 Tehran, so daytime opens are warm). Render free also
has monthly hours/bandwidth limits — fine for personal use.
"""

import gzip
import http.client
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dub as dub_mod  # v3.2: live Persian dubbing (stdlib only, no keys)

ALLOWED_HOSTS = {
    "thumbs.xfree.com",
    "cdn.xfree.com",
    "www.xfree.com",
    "xfree.com",
    "static.xfree.com",
    "scraper.xfree.com",
    "upload.xfree.com",
    "r.jina.ai",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# v3.1: fallback UA when the client sends none — the user's phone
# (Redmi Note 13, mobile Chrome). The proxy forwards the CLIENT's
# real UA whenever one is present (see Handler.client_ua).
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Redmi Note 13) "
             "AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/131.0.0.0 Mobile Safari/537.36")

# v3.1: external hosts the site (and its login flows) call DIRECTLY
# in the browser. They are filtered in Iran, so the proxy relays
# them under /g/<host>/<path> and rewrites their URLs in pages.
RELAY_HOSTS = {
    "accounts.google.com", "oauth2.googleapis.com",
    "www.googleapis.com", "apis.google.com",
    "ssl.gstatic.com", "fonts.gstatic.com",
    "www.google.com", "lh3.googleusercontent.com",
    "translate.google.com", "translate.googleapis.com",
    "www.googletagmanager.com", "challenges.cloudflare.com",
    "appleid.cdn-apple.com", "appleid.apple.com", "idmsa.apple.com",
    "content.xfree.com", "mtm.ntl.cloud",
}

# v3.6: Google Translate Element injection REMOVED (user correction
# 2026-11-22, screenshot 1000300657): it produced the Chrome
# translate bar/popup the user rejected — subtitles must be the
# bottom-of-video caption overlay only, never page translation.
# The site now shows in its own language; Persian comes only from
# the subtitle overlay. The constant is kept as an empty string so
# old references fail loudly instead of silently reappearing.
TRANSLATE_INJECT = ""

PROXY_VERSION = "3.10"
PROXY_BASE = "https://xfree-fa-proxy.onrender.com"

# v3.2: every spelling of the two OAuth callback URLs that can
# appear in served HTML/JS/JSON, with a placeholder each.
# googleURL/appleURL in the site's Nuxt state ARE these URLs — the
# redirect_uri the "Continue with Google/Apple" button sends to
# Google/Apple. They must stay exactly
# https://www.xfree.com/api/external/<google|apple> (registered in
# xfree.com's OAuth clients). Longest/most specific forms first:
# the "//..." form is a substring of the "https://..." form, so
# protecting the full form first keeps protocol-relative hits real.
_OAUTH_CALLBACK_PROTECT = [
    ("https://www.xfree.com/api/external/google",
     "__XF_OAUTH_GOOGLE_CB__"),
    ("https://www.xfree.com/api/external/apple",
     "__XF_OAUTH_APPLE_CB__"),
    ("http://www.xfree.com/api/external/google",
     "__XF_OAUTH_GOOGLE_CB_HTTP__"),
    ("http://www.xfree.com/api/external/apple",
     "__XF_OAUTH_APPLE_CB_HTTP__"),
    ("//www.xfree.com/api/external/google",
     "__XF_OAUTH_GOOGLE_CB_PROTO__"),
    ("//www.xfree.com/api/external/apple",
     "__XF_OAUTH_APPLE_CB_PROTO__"),
    ("https:\\u002F\\u002Fwww.xfree.com\\u002Fapi\\u002Fexternal\\u002Fgoogle",
     "__XF_OAUTH_GOOGLE_CB_ESC__"),
    ("https:\\u002F\\u002Fwww.xfree.com\\u002Fapi\\u002Fexternal\\u002Fapple",
     "__XF_OAUTH_APPLE_CB_ESC__"),
    ("https:\\/\\/www.xfree.com\\/api\\/external\\/google",
     "__XF_OAUTH_GOOGLE_CB_SLASH__"),
    ("https:\\/\\/www.xfree.com\\/api\\/external\\/apple",
     "__XF_OAUTH_APPLE_CB_SLASH__"),
]

# v1.9: 900s TTL — the keep-alive pings /page every 12 min, so the
# cache must outlive the ping interval or the home page is cold again.
_PAGE_CACHE = {}  # (path, want_md) -> (body_bytes, content_type, at)
_PAGE_CACHE_TTL = 900
_PAGE_CACHE_LOCK = threading.Lock()

CHUNK = 2 * 1024 * 1024  # v3.2: 2 MB writes for video streaming
                      # (was 1 MB — fewer write syscalls per MB
                      # on the 0.1-CPU Render free instance)

# v3.10 (ChatGPT priority 1): video Range streams are written in
# 64 KB chunks — the first bytes reach the player ~0.05 s after
# the upstream responds instead of after a full 2 MB read, so a
# slow phone starts playback sooner. The body is NEVER read into
# memory whole: pool_stream + stream_response_pooled stream it.
VIDEO_CHUNK = 64 * 1024

# ----------------------------------------------------------------------
# v3.0: keep-alive upstream connection pool.
# The old code used urllib.request.urlopen() per request: every single
# subresource (15 JS bundles, dozens of thumbs) paid a fresh TCP+TLS
# handshake to upstream (~0.4-0.7s each on this host). A page load is
# ~30 upstream requests, so handshakes alone cost seconds. The pool
# keeps up to 6 reusable TLS connections per host (what browsers do);
# a reused connection answers in ~0.1-0.2s. Connections are NOT shared
# between threads: each pooled slot has its own lock.
# ----------------------------------------------------------------------
class ConnPool:
    MAX_PER_HOST = 6

    def __init__(self):
        self._guard = threading.Lock()
        self._pools = {}  # host -> list of [conn, lock]

    def _new_conn(self, host, timeout):
        """Direct TLS, unless this machine routes egress through an
        HTTP proxy (https_proxy env — this dev VM does; Render does
        not). http.client ignores env proxies, unlike urllib, so
        tunnel explicitly with CONNECT; the tunnel itself is
        keep-alive, so pooling still works through it."""
        proxy = (os.environ.get("https_proxy")
                 or os.environ.get("HTTPS_PROXY") or "")
        if proxy:
            p = urllib.parse.urlparse(proxy)
            if p.scheme in ("http", "https") and p.hostname:
                # HTTPSConnection (not HTTPConnection): connect() does
                # the CONNECT tunnel AND then wraps the socket in TLS.
                # Plain HTTPConnection would send HTTP to port 443
                # ("plain HTTP request was sent to HTTPS port").
                conn = http.client.HTTPSConnection(
                    p.hostname, p.port or 3128, timeout=timeout)
                headers = {}
                if p.username:
                    import base64
                    cred = base64.b64encode(
                        ("%s:%s" % (
                            urllib.parse.unquote(p.username),
                            urllib.parse.unquote(p.password or ""))).encode()
                    ).decode()
                    headers["Proxy-Authorization"] = "Basic " + cred
                conn.set_tunnel(host, 443, headers=headers)
                return conn
        return http.client.HTTPSConnection(host, timeout=timeout)

    def acquire(self, host, timeout):
        """Return (conn, slot). slot is None for an ephemeral conn
        (created when all pooled slots are busy — closed after use).
        v3.2: NO 5s spin waiting for a free slot. A page load fires
        ~30 parallel subrequests against 6 pooled slots, so most
        requests used to poll for up to 5s before getting an
        ephemeral connection — a fresh TLS handshake (~0.4s) is
        far cheaper than waiting seconds for a pooled slot."""
        with self._guard:
            pool = self._pools.setdefault(host, [])
            for slot in pool:
                if slot[1].acquire(blocking=False):
                    return slot[0], slot
            if len(pool) < self.MAX_PER_HOST:
                conn = self._new_conn(host, timeout)
                lock = threading.Lock()
                lock.acquire()
                slot = [conn, lock]
                pool.append(slot)
                return conn, slot
        return self._new_conn(host, timeout), None

    def release(self, host, conn, slot, broken=False):
        if slot is None:
            try:
                conn.close()
            except Exception:
                pass
            return
        if broken:
            try:
                conn.close()
            except Exception:
                pass
            with self._guard:
                pool = self._pools.get(host, [])
                if slot in pool:
                    pool.remove(slot)
            try:
                slot[1].release()
            except Exception:
                pass
            return
        try:
            slot[1].release()
        except Exception:
            pass


POOL = ConnPool()


def _header_get(headers, name):
    """Case-insensitive lookup in a [(name, value)] list. Returns ''."""
    low = name.lower()
    for k, v in headers:
        if k.lower() == low:
            return v
    return ""


def _header_all(headers, name):
    low = name.lower()
    return [v for k, v in headers if k.lower() == low]


def pool_request(host, method, path, headers, body=None, timeout=15):
    """One upstream request over a pooled keep-alive connection.
    Returns (status, [(name, value), ...], body_bytes). Upstream gzip
    is requested and transparently decompressed (the old code fetched
    pages uncompressed: 568 KB per homepage). Retries once on a stale
    pooled connection (server closed it while idle)."""
    headers = dict(headers)
    headers.setdefault("Accept-Encoding", "gzip")
    last_exc = None
    for _attempt in range(2):
        conn, slot = POOL.acquire(host, timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            hdrs = resp.getheaders()
            POOL.release(host, conn, slot)
            enc = _header_get(hdrs, "Content-Encoding")
            if enc == "gzip" and data:
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
                hdrs = [(k, v) for k, v in hdrs
                        if k.lower() != "content-encoding"]
                hdrs = [(k, v) for k, v in hdrs
                        if k.lower() != "content-length"]
                hdrs.append(("Content-Length", str(len(data))))
            elif enc == "deflate" and data:
                try:
                    import zlib
                    data = zlib.decompress(data)
                except Exception:
                    pass
                hdrs = [(k, v) for k, v in hdrs
                        if k.lower() not in ("content-encoding",
                                             "content-length")]
                hdrs.append(("Content-Length", str(len(data))))
            return status, hdrs, data
        except Exception as exc:  # stale pooled conn, reset, timeout...
            last_exc = exc
            POOL.release(host, conn, slot, broken=True)
    raise last_exc


def pool_stream(host, method, path, headers, timeout=30):
    """Like pool_request but STREAMING: returns
    (status, headers, resp, token). The caller streams resp.read()
    itself and then calls release_stream(token, broken=...).
    Video Range requests used to open a FRESH TLS connection to
    cdn.xfree.com per request via urllib (0.4-0.7s handshake each,
    and the player fires many Range requests) — that was the video
    lag. A pooled connection answers in ~0.1s. Retries once on a
    stale pooled connection."""
    headers = dict(headers)
    headers["Accept-Encoding"] = "identity"  # never gzip video
    last_exc = None
    for _attempt in range(2):
        conn, slot = POOL.acquire(host, timeout)
        try:
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.getheaders(), resp, (host, conn, slot)
        except Exception as exc:
            last_exc = exc
            POOL.release(host, conn, slot, broken=True)
    raise last_exc


def release_stream(token, broken=False):
    host, conn, slot = token
    POOL.release(host, conn, slot, broken=broken)


# ----------------------------------------------------------------------
# v3.0: response cache for anonymous GETs.
# The homepage / playlist / search HTML is identical for every logged-out
# visitor and turns over slowly; serving it from memory makes the cold
# open ~instant once the proxy is awake. NEVER cached: anything with a
# Cookie (logged-in / session), POSTs, /api/rs/next (cursor), /api/user/me.
# ----------------------------------------------------------------------
_RP_CACHE = {}  # key -> (status, headers, body, at)
_RP_CACHE_LOCK = threading.Lock()
_RP_CACHE_RULES = [  # (path prefix / exact, ttl seconds)
    ("/_nuxt/", 3600),          # hashed filenames = immutable
    ("/api/post/", 300),        # video metadata, near-static
    ("/api/tag/", 300),
    ("/api/playlist/", 300),
    ("/api/user/", 120),        # public profiles (not /api/user/me)
    ("/api/rs/homepage", 30),   # anonymous recommendation feed
]


def _cache_ttl(path):
    for prefix, ttl in _RP_CACHE_RULES:
        if path.startswith(prefix):
            return ttl
    return 0


def _cache_get(key):
    with _RP_CACHE_LOCK:
        hit = _RP_CACHE.get(key)
        if hit is not None and time.time() - hit[3] < hit[4]:
            return hit
        if hit is not None:
            del _RP_CACHE[key]
    return None


def _cache_put(key, status, headers, body, ttl):
    if ttl <= 0 or len(body) > 2 * 1024 * 1024:
        return
    # NEVER cache Set-Cookie: the homepage sets a per-visitor
    # anonymous JWT; caching it would hand every visitor the SAME
    # anonymous session.
    headers = [(k, v) for k, v in headers if k.lower() != "set-cookie"]
    with _RP_CACHE_LOCK:
        if len(_RP_CACHE) > 200:  # bound memory on a 512 MB instance
            _RP_CACHE.clear()
        _RP_CACHE[key] = (status, headers, body, time.time(), ttl)


def fetch_stream(url, headers, timeout=30):
    """Open an upstream URL, return the response object (caller streams it).
    Used ONLY for video Range streams (one long-lived connection per
    stream — pooling would block the slot for the whole playback)."""
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def looks_like_page(body):
    """True if a fetched body is the real site page, not a Cloudflare
    challenge / block page. A challenge page must never be cached or
    returned — it would poison the cache for 15 minutes."""
    if body is None or len(body) < 1000:
        return False
    low = body[:2000].lower()
    if b"just a moment" in low or b"cf-challenge" in low \
            or b"attention required" in low or b"captcha" in low:
        return False
    return True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # v3.0: a page load fires ~30 parallel subrequests; the default
    # listen backlog (5) queued/dropped them under load.
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep Render logs readable
        print("proxy:", fmt % args, flush=True)

    # -- helpers ---------------------------------------------------------
    def client_ua(self):
        """v3.1: forward the CLIENT's real User-Agent upstream. The
        proxy used to send a hardcoded DESKTOP Chrome UA, so the site
        served its desktop layout (no hamburger, no mobile bottom
        nav, no top carousel) even though the app runs on a phone.
        The WebView sends a mobile UA; forwarding it makes the site
        serve its mobile layout — the exact screen in the user's
        screenshot. Falls back to a mobile UA when the client sent
        none (curl, keep-alive pings)."""
        ua = self.headers.get("User-Agent") or ""
        return ua if ua.startswith("Mozilla/") else MOBILE_UA

    def send_text(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def stream_response(self, resp, extra_headers=()):
        """Stream an upstream response to the client, chunk by chunk."""
        status = resp.status
        self.send_response(status)
        for name in ("Content-Type", "Content-Length", "Content-Range",
                     "Accept-Ranges", "Cache-Control", "Content-Disposition"):
            value = resp.headers.get(name)
            if value:
                self.send_header(name, value)
        # v1.4: thumbnails never change — let clients/CDNs cache them a day
        if self.path.startswith("/img") and not resp.headers.get("Cache-Control"):
            self.send_header("Cache-Control", "public, max-age=86400")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client (player) stopped / seeked away — normal

    def stream_response_pooled(self, resp, token):
        """Stream a pooled http.client response (video Range) to the
        client, then release the pooled connection. v3.1: the old
        video path used urllib (fresh TLS per Range request); pooled
        streaming reuses the cdn connection and writes 64 KB chunks
        (v3.10: VIDEO_CHUNK, down from 1 MB, so first bytes reach the
        player sooner). A client that stops early marks the slot
        broken so the next request gets a fresh connection."""
        status = resp.status
        self.send_response(status)
        for name in ("Content-Type", "Content-Length", "Content-Range",
                     "Accept-Ranges", "Cache-Control", "Content-Disposition"):
            value = resp.headers.get(name)
            if value:
                self.send_header(name, value)
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            release_stream(token, broken=False)
            return
        broken = False
        try:
            while True:
                chunk = resp.read(VIDEO_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            broken = True  # player stopped / seeked away — normal
        except Exception:
            broken = True
        finally:
            try:
                resp.close()
            except Exception:
                pass
            release_stream(token, broken=broken)

    def upstream_headers(self, extra=None):
        headers = {"User-Agent": self.client_ua()}
        for name in ("Range", "If-Range"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        if extra:
            headers.update(extra)
        return headers

    def allowed(self, url):
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except Exception:
            return False
        return host in ALLOWED_HOSTS

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        self.handle_all()

    def do_HEAD(self):
        self.handle_all()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        # v3.5 ROOT FIX: /dub/* must be routed BEFORE the body is
        # read. do_POST used to read rfile here and then
        # dub_mod.handle_dub() read rfile AGAIN — the second read
        # blocked forever on a keep-alive connection (body already
        # consumed), so POST /dub/stt and /dub/translate hung until
        # timeout while GET /dub/health worked fine. Local smoke
        # tests passed because dub.py's standalone server calls
        # handle_dub directly without pre-reading. handle_dub reads
        # the body itself, so it must run first.
        if parsed.path.startswith("/dub/"):
            if dub_mod.handle_dub(self):
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024 * 1024:
            self.send_text(413, "body too large")
            return
        raw = self.rfile.read(length) if length > 0 else b""

        if parsed.path.startswith("/g/"):
            self.handle_generic_relay(parsed, raw,
                                      self.headers.get("Content-Type"))
            return
        if parsed.path == "/auth":
            self.handle_auth(raw)
            return
        if parsed.path == "/upload":
            self.handle_upload(query, raw)
            return
        # v3.0: scraper / upload relays (any method, any path under
        # the prefix) — the site calls these hosts directly, which is
        # filtered in Iran; the rewritten JS calls the proxy instead.
        if parsed.path.startswith("/x-scraper/"):
            self.handle_host_relay("scraper.xfree.com", "/x-scraper",
                                   parsed, raw,
                                   self.headers.get("Content-Type"))
            return
        if parsed.path.startswith("/x-upload/"):
            self.handle_host_relay("upload.xfree.com", "/x-upload",
                                   parsed, raw,
                                   self.headers.get("Content-Type"))
            return
        # v2.6: any other POST (e.g. /api/login, /api/follow) goes
        # through the reverse proxy to the real site.
        self.handle_reverse_proxy(parsed, raw,
                                  self.headers.get("Content-Type"))

    def do_PUT(self):
        self._do_write_method()

    def do_DELETE(self):
        self._do_write_method()

    def do_PATCH(self):
        self._do_write_method()

    def _do_write_method(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/g/"):
            self.handle_generic_relay(parsed, raw,
                                      self.headers.get("Content-Type"))
            return
        if parsed.path.startswith("/x-scraper/"):
            self.handle_host_relay("scraper.xfree.com", "/x-scraper",
                                   parsed, raw,
                                   self.headers.get("Content-Type"))
            return
        if parsed.path.startswith("/x-upload/"):
            self.handle_host_relay("upload.xfree.com", "/x-upload",
                                   parsed, raw,
                                   self.headers.get("Content-Type"))
            return
        self.handle_reverse_proxy(parsed, raw,
                                  self.headers.get("Content-Type"))

    def handle_host_relay(self, host, prefix, parsed, raw, content_type):
        """Forward a request to scraper.xfree.com / upload.xfree.com
        (path after the prefix, same method/body/headers) and return
        the upstream status/body verbatim. Used by the rewritten site
        JS, which otherwise calls these filtered hosts directly."""
        rest = parsed.path[len(prefix):] or "/"
        target = rest
        if parsed.query:
            target += "?" + parsed.query
        headers = {"User-Agent": self.client_ua()}
        for name in ("Accept", "Accept-Language", "Range", "If-Range"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        cookies = self.headers.get("Cookie")
        if cookies:
            headers["Cookie"] = cookies
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        if content_type:
            headers["Content-Type"] = content_type
        headers["Origin"] = "https://www.xfree.com"
        headers["Referer"] = "https://www.xfree.com/"
        try:
            status, resp_headers, body = pool_request(
                host, self.command, target, headers,
                body=raw or None, timeout=60)
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        ctype = _header_get(resp_headers, "Content-Type")
        body = self._rewrite_body(body, ctype)
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name in ("Cache-Control", "Content-Range", "Accept-Ranges",
                     "Content-Disposition", "ETag"):
            value = _header_get(resp_headers, name)
            if value:
                self.send_header(name, value)
        loc = _header_get(resp_headers, "Location")
        if loc:
            self.send_header("Location", self._rewrite_location(loc))
        for cookie in _header_all(resp_headers, "Set-Cookie"):
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


    def handle_generic_relay(self, parsed, raw, content_type):
        """v3.1: /g/<host>/<path> — relay for the external hosts the
        site and its login flows call directly (Google OAuth/GSI,
        Apple ID, Translate, GTM, Turnstile, Matomo, content.xfree).
        They are filtered in Iran; the relay fetches them from
        outside Iran and returns status/headers/body verbatim (with
        Location and in-body URLs rewritten to stay on the proxy).
        Only allowlisted hosts (RELAY_HOSTS) — never an open proxy.
        NOTE (honest limit): cookies Google sets with
        Domain=.google.com are dropped by the browser because the
        page origin is the proxy, not google.com; host-only cookies
        do stick. The redirect (popup) OAuth flow does not depend on
        those cookies, but Google OneTap may refuse the proxy origin
        — only the user's own login test can prove the full flow."""
        rest = parsed.path[3:]  # strip "/g/"
        host = rest.split("/", 1)[0]
        if host not in RELAY_HOSTS:
            self.send_text(403, "disallowed relay host")
            return
        self.handle_host_relay(host, "/g/" + host, parsed, raw,
                               content_type)

    def handle_auth(self, raw):
        """POST /auth {"url": ..., "body": {...}, "cookies": "...",
        "origin": "...", "referer": "..."} -> upstream JSON POST.
        Only xfree.com / www.xfree.com targets are allowed.
        v2.4: forwards the app's Cookie header (the site's login flow
        is cookie-based: POST /api/anonymous-login first, then
        POST /api/login with that cookie — bundle 6347c71.js) and
        returns upstream Set-Cookie headers in "setCookie", because
        without them the session is lost. Nothing is invented: the
        upstream status/body/cookies are returned verbatim."""
        try:
            import json
            payload = json.loads(raw.decode("utf-8"))
            target = payload.get("url") or ""
            body = payload.get("body") or {}
            cookies = payload.get("cookies") or ""
            origin = payload.get("origin") or ""
            referer = payload.get("referer") or ""
        except Exception as exc:
            self.send_text(400, "bad json: %s" % exc)
            return
        host = urllib.parse.urlparse(target).hostname or ""
        if host not in ("www.xfree.com", "xfree.com") \
                or not target.startswith("https://"):
            self.send_text(403, "disallowed auth target")
            return
        # v2.4: the site's axios request interceptor (6347c71.js) puts
        # these headers on EVERY /api call: apiversion "1.0", language,
        # country, plus the cookie. Without apiversion the API edge
        # answers 404 "null" even for real paths.
        headers = {"User-Agent": self.client_ua(),
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/plain, */*",
                   "apiversion": "1.0",
                   "language": "en",
                   "Origin": "https://www.xfree.com",
                   "Referer": "https://www.xfree.com/"}
        if cookies:
            headers["Cookie"] = cookies
        if origin:
            headers["Origin"] = origin
        if referer:
            headers["Referer"] = referer
        try:
            import json
            status, resp_headers, upstream = pool_request(
                host, "POST", urllib.parse.urlparse(target).path
                + (("?" + urllib.parse.urlparse(target).query)
                   if urllib.parse.urlparse(target).query else ""),
                headers, body=json.dumps(body).encode("utf-8"), timeout=30)
            upstream_body = upstream.decode("utf-8", "replace")
            set_cookie = _header_all(resp_headers, "Set-Cookie")
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        import json
        out = json.dumps({"status": status, "body": upstream_body,
                          "setCookie": set_cookie})
        self.send_text(200, out, "application/json; charset=utf-8")

    def handle_auth_get(self, query):
        """GET /auth?url=https://www.xfree.com/api/... — same relay
        for GET calls that need the session cookie (e.g. the site's
        loadMyInfo: GET /api/user/<userId>, bundle 6347c71.js).
        Cookie header of the phone is forwarded; upstream Set-Cookie
        headers are returned in "setCookie"."""
        target = (query.get("url") or [""])[0]
        host = urllib.parse.urlparse(target).hostname or ""
        if host not in ("www.xfree.com", "xfree.com") \
                or not target.startswith("https://"):
            self.send_text(403, "disallowed auth target")
            return
        headers = {"User-Agent": self.client_ua(),
                   "Accept": "application/json, text/plain, */*",
                   "apiversion": "1.0",
                   "language": "en",
                   "Origin": "https://www.xfree.com",
                   "Referer": "https://www.xfree.com/"}
        cookies = self.headers.get("Cookie")
        if cookies:
            headers["Cookie"] = cookies
        parsed_t = urllib.parse.urlparse(target)
        path = parsed_t.path + (("?" + parsed_t.query)
                                if parsed_t.query else "")
        try:
            status, resp_headers, upstream = pool_request(
                host, "GET", path, headers, timeout=30)
            upstream_body = upstream.decode("utf-8", "replace")
            set_cookie = _header_all(resp_headers, "Set-Cookie")
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        import json
        out = json.dumps({"status": status, "body": upstream_body,
                          "setCookie": set_cookie})
        self.send_text(200, out, "application/json; charset=utf-8")

    def handle_upload(self, query, raw):
        """POST /upload?url=<target> — forward a raw multipart body to
        upload.xfree.com, return the upstream status + body verbatim."""
        target = (query.get("url") or [""])[0]
        host = urllib.parse.urlparse(target).hostname or ""
        if host != "upload.xfree.com" or not target.startswith("https://"):
            self.send_text(403, "disallowed upload target")
            return
        headers = {"User-Agent": self.client_ua()}
        ctype = self.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        parsed_t = urllib.parse.urlparse(target)
        path = parsed_t.path + (("?" + parsed_t.query)
                                if parsed_t.query else "")
        try:
            status, resp_headers, body = pool_request(
                host, "POST", path, headers, body=raw or None, timeout=120)
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        self.send_response(status)
        self.send_header("Content-Type",
                         _header_get(resp_headers, "Content-Type")
                         or "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def handle_diag(self, query):
        """GET /diag?url=https://www.xfree.com/... — diagnostic only:
        returns the upstream status + response headers (incl.
        Set-Cookie) without the body. Used to learn exactly what the
        site sets on a page load, so the app can reproduce the site's
        own boot sequence instead of guessing it."""
        target = (query.get("url") or [""])[0]
        host = urllib.parse.urlparse(target).hostname or ""
        if host not in ("www.xfree.com", "xfree.com") \
                or not target.startswith("https://"):
            self.send_text(403, "disallowed diag target")
            return
        parsed_t = urllib.parse.urlparse(target)
        path = parsed_t.path + (("?" + parsed_t.query)
                                if parsed_t.query else "")
        try:
            status, resp_headers, _body = pool_request(
                host, "GET", path, {"User-Agent": self.client_ua()},
                timeout=30)
            headers = dict(resp_headers)
            set_cookie = _header_all(resp_headers, "Set-Cookie")
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        import json
        out = json.dumps({"status": status, "headers": headers,
                          "setCookie": set_cookie})
        self.send_text(200, out, "application/json; charset=utf-8")

    # -- v2.6 reverse proxy (whole site through this origin) ---------
    def _rewrite_location(self, loc):
        """A redirect to www.xfree.com must stay on this proxy origin,
        or the WebView would navigate straight to the filtered site.
        v3.1: redirects to a relay host (e.g. Google's OAuth server
        redirecting to accounts.google.com) are rewritten to the
        /g/<host>/ relay the same way.
        v3.2: NOTE — this rewriting is INTENTIONAL for the OAuth
        callback too: Google's 302 to
        https://www.xfree.com/api/external/google?code=... becomes
        the proxy-relative /api/external/google?code=..., so the
        callback page loads on the proxy origin and its postMessage
        to the login modal is same-origin (the modal ignores
        cross-origin messages). The redirect_uri SENT to Google is
        a different thing — that one must stay www.xfree.com and is
        protected in _rewrite_body, never here."""
        if not loc:
            return loc
        for host in ("https://www.xfree.com", "https://xfree.com",
                     "http://www.xfree.com", "http://xfree.com",
                     "//www.xfree.com", "//xfree.com"):
            if loc.startswith(host):
                return loc[len(host):] or "/"
        for scheme in ("https://", "http://", "//"):
            for host in RELAY_HOSTS:
                prefix = scheme + host
                if loc.startswith(prefix):
                    return "/g/" + host + loc[len(prefix):]
        return loc

    def _rewrite_body(self, body, ctype):
        """Rewrite absolute site URLs in text bodies so subresources
        stay on the proxy origin instead of the filtered hosts.
        Only for text/* and JS/JSON; binary bodies pass through.
        Also handles the \\u002F-escaped URLs inside the Nuxt state
        JSON (e.g. scraperUrl:"https:\\u002F\\u002Fscraper.xfree.com")."""
        if not body or not ctype:
            return body
        low = ctype.lower()
        if not (low.startswith("text/") or "javascript" in low
                or "json" in low or "xml" in low):
            return body
        try:
            text = body.decode("utf-8")
        except Exception:
            return body
        base = PROXY_BASE
        esc = "https:\\u002F\\u002F"
        # v3.2: protect the OAuth callback URLs BEFORE any rewrite
        # runs (see _OAUTH_CALLBACK_PROTECT): googleURL/appleURL
        # must stay https://www.xfree.com/api/external/... — the
        # registered redirect_uri. v3.1 rewrote them to the proxy
        # origin and Google rejected login with
        # 400 redirect_uri_mismatch (proved live 2026-11-22).
        # Restored at the end of this function.
        for _orig, _ph in _OAUTH_CALLBACK_PROTECT:
            text = text.replace(_orig, _ph)
        # v3.1: relay hosts — Google/Apple/Translate/GTM/Turnstile/
        # Matomo and content.xfree.com are called DIRECTLY by the
        # page and its login flows; in Iran they are filtered, so
        # their URLs are rewritten to the /g/<host>/ relay (plain,
        # protocol-relative and \u002F-escaped forms).
        for _h in sorted(RELAY_HOSTS):
            text = text.replace("https://" + _h, base + "/g/" + _h)
            text = text.replace("//" + _h,
                                "//xfree-fa-proxy.onrender.com/g/" + _h)
            text = text.replace(esc + _h,
                                esc + "xfree-fa-proxy.onrender.com"
                                "\\u002Fg\\u002F" + _h)
        # v3.0: the Nuxt config sends the browser straight to
        # scraper.xfree.com and upload.xfree.com/media-api — both
        # filtered in Iran, so upload/scraper would silently fail.
        # Rewrite them to the proxy relays (added in v3.0).
        text = text.replace("https://scraper.xfree.com",
                            base + "/x-scraper")
        text = text.replace("https://upload.xfree.com",
                            base + "/x-upload")
        text = text.replace("//scraper.xfree.com", "//" + base[8:] + "/x-scraper")
        text = text.replace("//upload.xfree.com", "//" + base[8:] + "/x-upload")
        text = text.replace(esc + "scraper.xfree.com",
                            esc + "xfree-fa-proxy.onrender.com\\u002Fx-scraper")
        text = text.replace(esc + "upload.xfree.com",
                            esc + "xfree-fa-proxy.onrender.com\\u002Fx-upload")
        text = text.replace(esc + "www.xfree.com", esc + "xfree-fa-proxy.onrender.com")
        text = text.replace(esc + "xfree.com", esc + "xfree-fa-proxy.onrender.com")
        text = text.replace(esc + "thumbs.xfree.com", esc + "xfree-fa-proxy.onrender.com\\u002Fimg?u=https:\\u002F\\u002Fthumbs.xfree.com")
        text = text.replace(esc + "cdn.xfree.com", esc + "xfree-fa-proxy.onrender.com\\u002Fvideo?u=https:\\u002F\\u002Fcdn.xfree.com")
        text = text.replace(esc + "static.xfree.com", esc + "xfree-fa-proxy.onrender.com\\u002Fstatic?u=https:\\u002F\\u002Fstatic.xfree.com")
        # thumbs/cdn absolute URLs -> proxy /img / /video relays
        def _thumb(m):
            u = m.group(0)
            return base + "/img?u=" + urllib.parse.quote(u, safe="")
        def _cdn(m):
            u = m.group(0)
            return base + "/video?u=" + urllib.parse.quote(u, safe="")
        text = re.sub(r"https://thumbs\.xfree\.com/[^\s\"'<>]+", _thumb, text)
        text = re.sub(r"https://cdn\.xfree\.com/[^\s\"'<>]+", _cdn, text)
        # v2.7/v3.0: static.xfree.com is ~25s+ per request FROM Render's IP
        # (measured 2026-11-22: /static?u=<js> took 25s for 9 KB, and
        # /_nuxt/<same file> via www.xfree.com took 0.77s). The same
        # bundles are served by www.xfree.com under /_nuxt/, so .js AND
        # .css go there (fast pooled path); only non-JS/CSS static assets
        # (icons/svg) still use /static.
        def _static_fast(m):
            u = m.group(0)
            name = u.rsplit("/", 1)[-1]
            return base + "/_nuxt/" + name
        text = re.sub(r"https://static\.xfree\.com/[^\s\"'<>]+\.(?:js|css)",
                      _static_fast, text)
        def _static(m):
            u = m.group(0)
            return base + "/static?u=" + urllib.parse.quote(u, safe="")
        text = re.sub(r"https://static\.xfree\.com/[^\s\"'<>]+", _static, text)
        text = text.replace("https://www.xfree.com", base)
        text = text.replace("https://xfree.com", base)
        text = text.replace("//www.xfree.com", "//xfree-fa-proxy.onrender.com")
        text = text.replace("//xfree.com", "//xfree-fa-proxy.onrender.com")
        # v3.2: restore the OAuth callback URLs untouched — they
        # must reach the browser as https://www.xfree.com/...
        for _orig, _ph in _OAUTH_CALLBACK_PROTECT:
            text = text.replace(_ph, _orig)
        return text.encode("utf-8")

    def handle_reverse_proxy(self, parsed, raw=b"", content_type=None):
        """Forward any other path/method to https://www.xfree.com
        and return the upstream response (status, headers, Set-Cookie,
        body). v3.0: pooled keep-alive + upstream gzip + anonymous
        response cache. Jina fires ONLY for an HTML page that came back
        as a Cloudflare challenge (never on the happy path, never for
        API JSON — the v2.x bug was firing a doomed Jina fetch on every
        API body >= 1000 bytes, because looks_like_page() is False for
        JSON). A challenge page is still never served as if it were
        the site."""
        path = parsed.path
        if path.startswith("/site/"):
            path = path[5:] or "/"
        if not path.startswith("/"):
            path = "/" + path
        target_path = path
        if parsed.query:
            target_path += "?" + parsed.query
        method = self.command
        has_cookie = bool(self.headers.get("Cookie"))
        # v3.0 cache: anonymous GETs only.
        cache_key = None
        cache_ttl = 0
        if method == "GET" and not has_cookie:
            if path.startswith("/_nuxt/"):
                cache_ttl = 3600
            else:
                cache_ttl = _cache_ttl(path)
                if path in ("/", "/playlist") or path.startswith("/search") \
                        or path.startswith("/video"):
                    cache_ttl = max(cache_ttl, 120)
            if cache_ttl > 0:
                # v3.1: mobile and desktop SSR differ — never serve
                # a cached desktop page to a phone (or vice versa).
                _ua = self.client_ua()
                _cls = "m" if ("Mobile" in _ua or "Android" in _ua) else "d"
                cache_key = (method, target_path, _cls)
                hit = _cache_get(cache_key)
                if hit is not None:
                    _status, _hdrs, _body, _at, _ttl = hit
                    self._send_proxied(_status, _hdrs, _body, cache_hit=True)
                    return
        headers = {"User-Agent": self.client_ua()}
        for name in ("Accept", "Accept-Language", "Range", "If-Range"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        cookies = self.headers.get("Cookie")
        if cookies:
            headers["Cookie"] = cookies
        if path.startswith("/api"):
            headers["apiversion"] = "1.0"
            headers["language"] = "en"
        origin = self.headers.get("Origin") or ""
        if origin.startswith(PROXY_BASE):
            origin = "https://www.xfree.com"
        headers["Origin"] = origin or "https://www.xfree.com"
        referer = self.headers.get("Referer") or ""
        if referer.startswith(PROXY_BASE):
            referer = "https://www.xfree.com/" + referer[len(PROXY_BASE + "/"):]
        headers["Referer"] = referer or "https://www.xfree.com/"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            status, resp_headers, body = pool_request(
                "www.xfree.com", method, target_path, headers,
                body=raw or None, timeout=30)
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        ctype = _header_get(resp_headers, "Content-Type") or ""
        body = self._rewrite_body(body, ctype)
        if method == "GET" and ctype.startswith("text/html") \
                and len(body) >= 1000 and not looks_like_page(body):
            # v3.0: Cloudflare challenge page (or a 403 with one).
            # The happy path (Render's IP, 200 + real page) never
            # reaches this — it costs nothing when direct works.
            # When the IP IS challenged, Jina Reader is the only
            # fallback that still returns the real page (same as
            # legacy /page). One attempt, short timeout; if it also
            # fails, report the real failure instead of a fake page.
            jina_url = ("https://r.jina.ai/https://www.xfree.com"
                        + target_path)
            try:
                req = urllib.request.Request(
                    jina_url,
                    headers={"User-Agent": self.client_ua(),
                             "X-Return-Format": "html"},
                    method="GET")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    cand = resp.read(25 * 1024 * 1024)
                if looks_like_page(cand):
                    body = cand
                    status = 200
                    ctype = "text/html; charset=utf-8"
                    resp_headers = [("Content-Type", ctype)]
                else:
                    self.send_text(
                        502, "upstream blocked (challenge page)")
                    return
            except Exception:
                self.send_text(502, "upstream blocked (challenge page)")
                return
        # v3.6: Translate Element injection REMOVED (user correction
        # 2026-11-22): it produced the Chrome translate bar/popup
        # the user rejected. Subtitles are the bottom-of-video
        # caption overlay only — never page translation.
        if cache_key is not None and status == 200:
            if ctype.startswith("text/html") and not looks_like_page(body):
                pass  # never cache a challenge page
            else:
                _cache_put(cache_key, status, resp_headers, body, cache_ttl)
        self._send_proxied(status, resp_headers, body)

    def _send_proxied(self, status, resp_headers, body, cache_hit=False):
        """Send a reverse-proxied response: status, headers (with
        Location rewritten to this origin, Set-Cookie passed through),
        body. Used by handle_reverse_proxy and the cache path."""
        self.send_response(status)
        ctype = _header_get(resp_headers, "Content-Type")
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name in ("Cache-Control", "Content-Range", "Accept-Ranges",
                     "Content-Disposition", "ETag"):
            value = _header_get(resp_headers, name)
            if value:
                self.send_header(name, value)
        loc = _header_get(resp_headers, "Location")
        if loc:
            self.send_header("Location", self._rewrite_location(loc))
        for cookie in _header_all(resp_headers, "Set-Cookie"):
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        if cache_hit:
            self.send_header("X-Proxy-Cache", "HIT")
        else:
            self.send_header("X-Proxy-Cache", "MISS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_all(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        # v2.6: "/" IS the site homepage for the WebView app (the
        # WebView loads the proxy origin as if it were xfree.com).
        # Health moved to /health only, so the root is not "ok" text.
        if parsed.path in ("/health",):
            self.send_text(200, "ok")
            return

        # v3.2: live Persian dubbing endpoints.
        if parsed.path.startswith("/dub/"):
            if dub_mod.handle_dub(self):
                return

        if parsed.path == "/auth":
            self.handle_auth_get(query)
            return

        if parsed.path == "/diag":
            self.handle_diag(query)
            return

        if parsed.path == "/page":
            self.handle_page(query)
            return

        if parsed.path == "/img":
            self.handle_binary(query, "image")
            return

        if parsed.path == "/static":
            # v2.6: JS bundles + icons from static.xfree.com.
            self.handle_binary(query, "static")
            return

        if parsed.path == "/video":
            # ?u=... is the mp4 relay; anything else (/video?id=...)
            # is a site page through the reverse proxy.
            if (query.get("u") or [""])[0]:
                self.handle_binary(query, "video")
                return
            self.handle_reverse_proxy(parsed)
            return

        # v3.1: generic relay for Google/Apple/Translate/etc.
        if parsed.path.startswith("/g/"):
            self.handle_generic_relay(parsed, b"", None)
            return

        # v3.0: scraper / upload relays (GET side; POST/PUT/DELETE
        # are routed in do_POST/_do_write_method).
        if parsed.path.startswith("/x-scraper/"):
            self.handle_host_relay("scraper.xfree.com", "/x-scraper",
                                   parsed, b"", None)
            return
        if parsed.path.startswith("/x-upload/"):
            self.handle_host_relay("upload.xfree.com", "/x-upload",
                                   parsed, b"", None)
            return

        # v2.6: everything else is the real site, served from this
        # origin (/_nuxt/..., /api/..., /playlist, /search,
        # /<username>, ...). Unknown paths are NOT invented — the
        # upstream status/body is returned verbatim.
        self.handle_reverse_proxy(parsed)

    def send_cached_page(self, body, ctype, hit=True):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("X-Proxy-Cache", "HIT" if hit else "MISS")
        # v1.9 diagnostic: what the upstream (Jina/direct) actually did
        # and how long it took, e.g. "jina:200:8.4s" or "jina:429:2.0s".
        upstream = getattr(self, "_upstream", "")
        if upstream:
            self.send_header("X-Upstream", upstream)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_page(self, query):
        path = (query.get("path") or [""])[0]
        if not path.startswith("/"):
            self.send_text(400, "path must start with /")
            return
        want_md = (query.get("format") or [""])[0] == "md"
        _ua = self.client_ua()
        _cls = "m" if ("Mobile" in _ua or "Android" in _ua) else "d"
        cache_key = (path, want_md, _cls)
        with _PAGE_CACHE_LOCK:
            hit = _PAGE_CACHE.get(cache_key)
            if hit is not None and time.time() - hit[2] < _PAGE_CACHE_TTL:
                self.send_cached_page(hit[0], hit[1])
                return
        # v1.9: DIRECT FIRST. Live X-Upstream measurements (2026-11-21)
        # proved Jina is currently UNREACHABLE from Render — both Jina
        # attempts throw instantly (0.1-0.3s, then ~2.2s) — while a
        # direct fetch of www.xfree.com from Render returns HTTP 200
        # with the real page (24 wall__item cards) in 1.2-1.8s. The old
        # Jina-first order wasted ~6.5s on two doomed Jina attempts +
        # sleeps before the direct fallback ever ran: that was the
        # ~9s /page cost. Direct may be Cloudflare-challenged on some
        # IPs/times (it is from this VM), so the direct body is
        # VALIDATED before use: a challenge page is never cached or
        # returned — Jina is the fallback for exactly that case.
        body = None
        last_exc = None
        upstream_info = []
        t_start = time.time()
        direct = "https://www.xfree.com" + path
        try:
            with fetch_stream(direct, {"User-Agent": self.client_ua()},
                             timeout=10) as resp:
                candidate = resp.read()
            upstream_info.append("direct:%s:%.1fs"
                                 % (resp.status, time.time() - t_start))
            if resp.status == 200 and looks_like_page(candidate):
                body = candidate
        except Exception as exc:
            last_exc = exc
            upstream_info.append("direct:%s:%.1fs"
                                 % (type(exc).__name__, time.time() - t_start))
        if body is None:
            jina_url = "https://r.jina.ai/https://www.xfree.com" + path
            headers = self.upstream_headers(
                {} if want_md else {"X-Return-Format": "html"})
            for attempt in range(2):
                try:
                    with fetch_stream(jina_url, headers, timeout=20) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            upstream_info.append("jina:%s:%.1fs"
                                                 % (resp.status, time.time() - t_start))
                            retry_after = resp.headers.get("Retry-After")
                            wait = 2 * (attempt + 1)
                            if retry_after:
                                try:
                                    wait = max(wait, int(retry_after))
                                except ValueError:
                                    pass
                            time.sleep(wait)
                            continue
                        body = resp.read()
                        upstream_info.append("jina:%s:%.1fs"
                                             % (resp.status, time.time() - t_start))
                    break
                except Exception as exc:
                    last_exc = exc
                    upstream_info.append("jina:%s:%.1fs"
                                         % (type(exc).__name__, time.time() - t_start))
                    time.sleep(2 * (attempt + 1))
        self._upstream = ";".join(upstream_info)
        if body is not None:
            ctype = ("text/markdown; charset=utf-8" if want_md
                     else "text/html; charset=utf-8")
            with _PAGE_CACHE_LOCK:
                _PAGE_CACHE[cache_key] = (body, ctype, time.time())
            self.send_cached_page(body, ctype, hit=False)
            return
        self.send_text(502, "upstream failed: %s" % last_exc)

    def handle_binary(self, query, _kind):
        target = (query.get("u") or [""])[0]
        # v3.10: unwrap a double-wrapped relay URL ONCE. The app's
        # v3.9 SubtitleEngine wrapped an already-proxied /video?u=
        # URL a second time, so the target host was the proxy
        # itself — not in ALLOWED_HOSTS — and the proxy answered
        # 400 "bad or disallowed url". That 400 was the whole
        # "audio cannot be captured" subtitle failure (the mp4
        # DOES have an audio track; ffprobe proves it). One unwrap
        # only, then the normal allowed() check runs on the inner
        # URL, so no new host is ever smuggled in. The proxy's own
        # host is PROXY_BASE's hostname plus the Host header this
        # request arrived on (so it also works on a local dev
        # instance, not just on Render).
        try:
            _pt = urllib.parse.urlparse(target)
            _own = {urllib.parse.urlparse(PROXY_BASE).hostname}
            _host_hdr = (self.headers.get("Host") or "").split(":")[0]
            if _host_hdr:
                _own.add(_host_hdr)
            if _pt.hostname in _own:
                _inner = urllib.parse.parse_qs(
                    _pt.query).get("u", [""])[0]
                if _inner.startswith("https://"):
                    target = _inner
        except Exception:
            pass
        if not target or not target.startswith("https://") \
                or not self.allowed(target):
            self.send_text(400, "bad or disallowed url")
            return
        # v3.0: .js/.css via /static now come from the FAST www
        # /_nuxt/ path (pooled) instead of the slow static host.
        if _kind == "static":
            low = target.lower()
            if low.endswith(".js") or low.endswith(".css"):
                name = target.rsplit("/", 1)[-1]
                try:
                    status, resp_headers, body = pool_request(
                        "www.xfree.com", "GET", "/_nuxt/" + name,
                        {"User-Agent": self.client_ua(),
                         "Accept": self.headers.get("Accept") or "*/*"},
                        timeout=15)
                except Exception as exc:
                    self.send_text(502, "upstream failed: %s" % exc)
                    return
                self.send_response(status)
                ctype = _header_get(resp_headers, "Content-Type")
                if ctype:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                cc = _header_get(resp_headers, "Cache-Control")
                if cc:
                    self.send_header("Cache-Control", cc)
                self.send_header("X-Proxy-Version", PROXY_VERSION)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
        if _kind == "video":
            # v3.1: Range stream over a POOLED keep-alive connection
            # to cdn.xfree.com. The old urllib path paid a fresh
            # TCP+TLS handshake per Range request (0.4-0.7s each) and
            # the player fires many Range requests — that was the
            # stutter. The slot is released when the stream ends.
            parsed_t = urllib.parse.urlparse(target)
            host = parsed_t.hostname
            path = parsed_t.path + (("?" + parsed_t.query)
                                    if parsed_t.query else "")
            last_exc = None
            for attempt in range(2):
                token = None
                try:
                    _st, _hdrs, resp, token = pool_stream(
                        host, "GET", path, self.upstream_headers(),
                        timeout=30)
                    self.stream_response_pooled(resp, token)
                    return
                except Exception as exc:
                    last_exc = exc
                    if token is not None:
                        release_stream(token, broken=True)
                    if attempt == 0:
                        time.sleep(0.5)
            self.send_text(502, "upstream failed: %s" % last_exc)
            return
        # v3.0: thumbs + static icons via the pooled connection
        # (keep-alive) instead of a fresh TLS handshake per image.
        parsed_t = urllib.parse.urlparse(target)
        host = parsed_t.hostname
        path = parsed_t.path + (("?" + parsed_t.query)
                                if parsed_t.query else "")
        try:
            status, resp_headers, body = pool_request(
                host, "GET", path, self.upstream_headers(), timeout=12)
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return
        self.send_response(status)
        ctype = _header_get(resp_headers, "Content-Type")
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name in ("Content-Range", "Accept-Ranges", "Cache-Control"):
            value = _header_get(resp_headers, name)
            if value:
                self.send_header(name, value)
        if _kind == "image" and not _header_get(resp_headers,
                                                 "Cache-Control"):
            self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = Server(("0.0.0.0", port), Handler)
    print("xfree-fa proxy listening on port %d" % port, flush=True)
    server.serve_forever()
