#!/usr/bin/env python3
"""XFreeFa proxy — tiny no-VPN backend for the Persian XFree app.

Runs on Render free tier (or anywhere with Python 3, zero dependencies —
only the standard library, so there is nothing to pip-install).

Why this exists: xfree.com / thumbs.xfree.com / cdn.xfree.com are filtered
in Iran, so the phone cannot reach them directly with VPN off. The phone
talks ONLY to this service (onrender.com) and this service — running
outside Iran — fetches the bytes and streams them back.

Endpoints (all GET):
  /                  health check -> "ok"
  /page?path=/...    site page HTML, fetched via Jina Reader server-side
                     (&format=md -> Markdown instead of HTML)
  /img?u=<thumb url>    thumbnail image, streamed from thumbs.xfree.com
  /video?u=<mp4 url>    video file, streamed from cdn.xfree.com with
                     HTTP Range passthrough (206), so VideoView seeking
                     and DownloadManager resume work.

Security: only these hosts are allowed (anything else -> 403), so the
proxy cannot be abused as an open proxy:
  thumbs.xfree.com, cdn.xfree.com, www.xfree.com, xfree.com, r.jina.ai

Deploy on Render (free, ~5 minutes, no card):
  1. Push this repo to GitHub (or use the xfree-fa repo).
  2. Render dashboard -> New -> Web Service -> pick the repo.
  3. Name: xfree-fa-proxy   (EXACT — the app expects
     https://xfree-fa-proxy.onrender.com)
     Runtime: Python 3 | Root Directory: proxy
     Build Command: (leave empty — no dependencies)
     Start Command: python app.py
     Plan: Free.
  4. Deploy, open https://xfree-fa-proxy.onrender.com/ — it must say "ok".

Limits (honest): Render free sleeps after ~15 min idle; the first
request after sleep takes ~30-60s to wake the service. The app's proxy
attempts use long read timeouts for pages and fall back to Jina/direct
if the proxy is unreachable, so a sleeping proxy only slows the first
load, it does not break the app. Render free also has monthly hours/
bandwidth limits — fine for personal use, not for sharing publicly.
"""

import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED_HOSTS = {
    "thumbs.xfree.com",
    "cdn.xfree.com",
    "www.xfree.com",
    "xfree.com",
    "r.jina.ai",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

PROXY_VERSION = "1.9"

# v1.9: 900s TTL — the keep-alive pings /page every 12 min, so the
# cache must outlive the ping interval or the home page is cold again.
# Home content turns over slowly; a 15-min-stale list is a fair trade
# for a ~1s load instead of ~9s through Jina.
_PAGE_CACHE = {}  # (path, want_md) -> (body_bytes, content_type, at)
_PAGE_CACHE_TTL = 900
_PAGE_CACHE_LOCK = threading.Lock()

CHUNK = 256 * 1024  # v1.4: fewer, bigger writes = faster streaming
                      # through the 0.1-CPU Render free instance


def fetch_stream(url, headers, timeout=30):
    """Open an upstream URL, return the response object (caller streams it)."""
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep Render logs readable
        print("proxy:", fmt % args, flush=True)

    # -- helpers ---------------------------------------------------------
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

    def upstream_headers(self, extra=None):
        headers = {"User-Agent": UA}
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

    def handle_all(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/health"):
            self.send_text(200, "ok")
            return

        if parsed.path == "/page":
            self.handle_page(query)
            return

        if parsed.path == "/img":
            self.handle_binary(query, "image")
            return

        if parsed.path == "/video":
            self.handle_binary(query, "video")
            return

        self.send_text(404, "not found")

    def send_cached_page(self, body, ctype, hit=True):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("X-Proxy-Cache", "HIT" if hit else "MISS")
        # v1.9 diagnostic: what the upstream (Jina/direct) actually did
        # and how long it took, e.g. "jina:200:8.4s" or "jina:429:2.0s".
        # The ~9s MISS cost lives here, not in the app — this header
        # proves whether it is Jina slowness or a 429 retry from Render.
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
        cache_key = (path, want_md)
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
            with fetch_stream(direct, {"User-Agent": UA},
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
        if not target or not target.startswith("https://") \
                or not self.allowed(target):
            self.send_text(400, "bad or disallowed url")
            return
        # v1.6: one retry on failure (a first-attempt hiccup must not
        # blank a thumbnail that would load on the second try).
        last_exc = None
        for attempt in range(2):
            try:
                with fetch_stream(target, self.upstream_headers(),
                                 timeout=30) as resp:
                    self.stream_response(resp)
                return
            except urllib.error.HTTPError as exc:
                self.send_text(exc.code or 502, "upstream error %s" % exc.code)
                return
            except Exception as exc:
                last_exc = exc
                time.sleep(1)
        self.send_text(502, "upstream failed: %s" % last_exc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("xfree-fa proxy listening on port %d" % port, flush=True)
    server.serve_forever()
