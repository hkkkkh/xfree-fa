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

CHUNK = 256 * 1024  # v1.4: fewer, bigger writes = faster streaming
                      # through the 0.1-CPU Render free instance


def fetch_stream(url, headers, timeout=30):
    """Open an upstream URL, return the response object (caller streams it)."""
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


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

    def handle_page(self, query):
        path = (query.get("path") or [""])[0]
        if not path.startswith("/"):
            self.send_text(400, "path must start with /")
            return
        want_md = (query.get("format") or [""])[0] == "md"
        jina_url = "https://r.jina.ai/https://www.xfree.com" + path
        headers = self.upstream_headers(
            {} if want_md else {"X-Return-Format": "html"})
        try:
            with fetch_stream(jina_url, headers, timeout=30) as resp:
                body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/markdown; charset=utf-8" if want_md
                             else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except Exception as exc:
            # Fallback: direct fetch of the site page (works when the
            # server's IP is not Cloudflare-challenged).
            try:
                direct = "https://www.xfree.com" + path
                with fetch_stream(direct, {"User-Agent": UA},
                                 timeout=20) as resp:
                    body = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except Exception:
                self.send_text(502, "upstream failed: %s" % exc)

    def handle_binary(self, query, _kind):
        target = (query.get("u") or [""])[0]
        if not target or not target.startswith("https://") \
                or not self.allowed(target):
            self.send_text(400, "bad or disallowed url")
            return
        try:
            with fetch_stream(target, self.upstream_headers(),
                             timeout=30) as resp:
                self.stream_response(resp)
        except urllib.error.HTTPError as exc:
            self.send_text(exc.code or 502, "upstream error %s" % exc.code)
        except Exception as exc:
            self.send_text(502, "upstream failed: %s" % exc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("xfree-fa proxy listening on port %d" % port, flush=True)
    server.serve_forever()
