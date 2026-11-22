#!/usr/bin/env python3
"""XFreeFa proxy — tiny no-VPN backend for the Persian XFree app.

Runs on Render free tier (or anywhere with Python 3, zero dependencies —
only the standard library, so there is nothing to pip-install).

Why this exists: xfree.com / thumbs.xfree.com / cdn.xfree.com are filtered
in Iran, so the phone cannot reach them directly with VPN off. The phone
talks ONLY to this service (onrender.com) and this service — running
outside Iran — fetches the bytes and streams them back.

Endpoints (all GET unless noted):
  /                  health check -> "ok"
  /page?path=/...    site page HTML, fetched via Jina Reader server-side
                     (&format=md -> Markdown instead of HTML)
  /img?u=<thumb url>    thumbnail image, streamed from thumbs.xfree.com
  /video?u=<mp4 url>    video file, streamed from cdn.xfree.com with
                     HTTP Range passthrough (206), so VideoView seeking
                     and DownloadManager resume work.
  /* (any other path)  v2.6 REVERSE PROXY for the WebView app: the whole
                     site served from this origin, so the real xfree.com
                     runs inside the app without VPN:
                     GET /_nuxt/..., /api/..., /video?id=..., /playlist,
                     /search, /<username> ... are forwarded to
                     https://www.xfree.com + path, with Cookie /
                     apiversion / Set-Cookie forwarded, absolute
                     www/thumbs/cdn URLs rewritten to this proxy, and
                     redirects rewritten to this origin. POSTs to /api/*
                     are forwarded the same way (login, follow, ...).
                     /page, /img, /video?u=, /auth, /upload, /diag keep
                     their old behaviour; /video without ?u= and
                     /upload (GET) are site pages, not relays.
  POST /auth         v2.4 JSON relay for login/signup: body
                     {"url": "https://www.xfree.com/api/...", "body": {...},
                      "cookies": "anonymous=...; authorization=..."}
                     -> {"status": <upstream code>, "body": "<text>",
                         "setCookie": [...]}
                     Only xfree.com / www.xfree.com are allowed.
                     Forwards Cookie/Origin/Referer; returns upstream
                     Set-Cookie (v2.3 dropped them, so login could
                     never keep a session).
  GET  /auth?url=... v2.4 GET relay for cookie-authenticated calls
                     (e.g. GET /api/user/<userId>), same response shape.
  POST /upload?url=<upload host>   v2.3 raw relay for video uploads:
                     forwards the multipart body + Content-Type +
                     Authorization to upload.xfree.com and returns the
                     upstream status/body. Only upload.xfree.com allowed.

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

PROXY_VERSION = "2.6"

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

    def do_POST(self):
        # v2.3 POST relays (login + upload). The phone cannot reach
        # xfree.com directly in Iran; these forward the request from
        # outside Iran and return the REAL upstream status/body.
        # Nothing here invents a success: a failed upstream is
        # reported with its real code.
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024 * 1024:
            self.send_text(413, "body too large")
            return
        raw = self.rfile.read(length) if length > 0 else b""

        if parsed.path == "/auth":
            self.handle_auth(raw)
            return
        if parsed.path == "/upload":
            self.handle_upload(query, raw)
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
        self.handle_reverse_proxy(parsed, raw,
                                  self.headers.get("Content-Type"))

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
        headers = {"User-Agent": UA,
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
            req = urllib.request.Request(
                target,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                upstream_body = resp.read(20000).decode("utf-8", "replace")
                status = resp.status
                set_cookie = resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            upstream_body = exc.read(20000).decode("utf-8", "replace")
            status = exc.code
            set_cookie = exc.headers.get_all("Set-Cookie") or []
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
        headers = {"User-Agent": UA,
                   "Accept": "application/json, text/plain, */*",
                   "apiversion": "1.0",
                   "language": "en",
                   "Origin": "https://www.xfree.com",
                   "Referer": "https://www.xfree.com/"}
        cookies = self.headers.get("Cookie")
        if cookies:
            headers["Cookie"] = cookies
        try:
            req = urllib.request.Request(target, headers=headers,
                                         method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                upstream_body = resp.read(20000).decode("utf-8", "replace")
                status = resp.status
                set_cookie = resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            upstream_body = exc.read(20000).decode("utf-8", "replace")
            status = exc.code
            set_cookie = exc.headers.get_all("Set-Cookie") or []
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
        headers = {"User-Agent": UA}
        ctype = self.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        try:
            req = urllib.request.Request(target, data=raw,
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = resp.read(20000)
                self.send_response(resp.status)
                self.send_header("Content-Type",
                                 resp.headers.get("Content-Type",
                                                  "text/plain; charset=utf-8"))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Proxy-Version", PROXY_VERSION)
                self.end_headers()
                self.wfile.write(body)
                return
        except urllib.error.HTTPError as exc:
            body = exc.read(20000)
            self.send_response(exc.code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Proxy-Version", PROXY_VERSION)
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)
            return

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
        try:
            req = urllib.request.Request(
                target, headers={"User-Agent": UA}, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                headers = dict(resp.headers.items())
                set_cookie = resp.headers.get_all("Set-Cookie") or []
                resp.read(1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items())
            set_cookie = exc.headers.get_all("Set-Cookie") or []
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
        or the WebView would navigate straight to the filtered site."""
        if not loc:
            return loc
        for host in ("https://www.xfree.com", "https://xfree.com",
                     "http://www.xfree.com", "http://xfree.com",
                     "//www.xfree.com", "//xfree.com"):
            if loc.startswith(host):
                return loc[len(host):] or "/"
        return loc

    def _rewrite_body(self, body, ctype):
        """Rewrite absolute site URLs in text bodies so subresources
        stay on the proxy origin instead of the filtered hosts.
        Only for text/* and JS/JSON; binary bodies pass through."""
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
        import re
        base = "https://xfree-fa-proxy.onrender.com"
        # thumbs/cdn absolute URLs -> proxy /img / /video relays
        def _thumb(m):
            u = m.group(0)
            return base + "/img?u=" + urllib.parse.quote(u, safe="")
        def _cdn(m):
            u = m.group(0)
            return base + "/video?u=" + urllib.parse.quote(u, safe="")
        text = re.sub(r"https://thumbs\.xfree\.com/[^\s\"'<>]+", _thumb, text)
        text = re.sub(r"https://cdn\.xfree\.com/[^\s\"'<>]+", _cdn, text)
        text = text.replace("https://www.xfree.com", base)
        text = text.replace("https://xfree.com", base)
        text = text.replace("//www.xfree.com", "//xfree-fa-proxy.onrender.com")
        text = text.replace("//xfree.com", "//xfree-fa-proxy.onrender.com")
        return text.encode("utf-8")

    def handle_reverse_proxy(self, parsed, raw=b"", content_type=None):
        """Forward any other path/method to https://www.xfree.com
        and return the upstream response verbatim (status, headers,
        Set-Cookie, body). Direct first; for an HTML GET that comes
        back as a Cloudflare challenge, fall back to Jina like
        /page does. No caching here — API/session responses must
        never be served stale."""
        path = parsed.path
        if path.startswith("/site/"):
            path = path[5:] or "/"
        if not path.startswith("/"):
            path = "/" + path
        target = "https://www.xfree.com" + path
        if parsed.query:
            target += "?" + parsed.query
        headers = {"User-Agent": UA}
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
        if origin.startswith("https://xfree-fa-proxy.onrender.com"):
            origin = "https://www.xfree.com"
        headers["Origin"] = origin or "https://www.xfree.com"
        referer = self.headers.get("Referer") or ""
        if referer.startswith("https://xfree-fa-proxy.onrender.com"):
            referer = "https://www.xfree.com/" + referer[len(
                "https://xfree-fa-proxy.onrender.com/"):]
        headers["Referer"] = referer or "https://www.xfree.com/"
        if content_type:
            headers["Content-Type"] = content_type
        method = self.command
        status = 502
        body = b"upstream failed"
        resp_headers = {}
        set_cookie = []
        try:
            req = urllib.request.Request(target, data=raw or None,
                                         headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                body = resp.read(25 * 1024 * 1024)
                resp_headers = dict(resp.headers.items())
                set_cookie = resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(25 * 1024 * 1024)
            except Exception:
                body = b""
            resp_headers = dict(exc.headers.items())
            set_cookie = exc.headers.get_all("Set-Cookie") or []
        except Exception as exc:
            body = ("upstream failed: %s" % exc).encode("utf-8")
            status = 502
        # HTML GET that got a challenge page: retry via Jina (same
        # fallback as /page) instead of serving the challenge.
        if (method == "GET"
                and not looks_like_page(body)
                and len(body) >= 1000):
            try:
                jina_url = "https://r.jina.ai/" + target
                req = urllib.request.Request(
                    jina_url,
                    headers={"User-Agent": UA,
                             "X-Return-Format": "html"},
                    method="GET")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    cand = resp.read(25 * 1024 * 1024)
                    if looks_like_page(cand):
                        body = cand
                        status = 200
                        resp_headers = {"Content-Type":
                                        "text/html; charset=utf-8"}
                        set_cookie = []
            except Exception:
                pass
        ctype = resp_headers.get("Content-Type") or ""
        body = self._rewrite_body(body, ctype)
        if ctype.startswith("text/html") and not looks_like_page(body) \
                and status == 200 and len(body) >= 1000:
            # Still a challenge page after Jina: do not pretend it
            # is the site — report the real failure.
            status = 502
            body = b"upstream blocked (challenge page)"
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name in ("Cache-Control", "Content-Range", "Accept-Ranges",
                     "Content-Disposition", "ETag"):
            if resp_headers.get(name):
                self.send_header(name, resp_headers[name])
        loc = resp_headers.get("Location")
        if loc:
            self.send_header("Location", self._rewrite_location(loc))
        for cookie in set_cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("X-Proxy-Version", PROXY_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD" and method != "HEAD":
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

        if parsed.path == "/video":
            # ?u=... is the mp4 relay; anything else (/video?id=...)
            # is a site page through the reverse proxy.
            if (query.get("u") or [""])[0]:
                self.handle_binary(query, "video")
                return
            self.handle_reverse_proxy(parsed)
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
