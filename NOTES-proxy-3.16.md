# XFreeFa proxy 3.16 — SPEED FIX (2026-11-24)

NOTE: this is the PROXY version 3.16 (no APK change; app v3.16 is a
separate thing — see NOTES-v3.16.md). Proxy-only change.

## What was measured BEFORE (VM in Chicago, US → Railway, 2026-11-24 ~03:10 UTC)

| Check | Before |
|---|---|
| /health ×5 | 200, TTFB 0.35–0.61 s (earlier single probe 1.11 s — variable, network-dominated, not server-side: the handler is a trivial `send_text(200, "ok")`; TLS alone from this VM is ~0.16 s) |
| Homepage, mobile UA, 1st fetch | 200, TTFB 0.70 s, total 1.03 s, 477,585 B uncompressed; gzipped wire 123,062 B (TTFB 1.01 s on that run) |
| Homepage, 2nd fetch | 200, TTFB 0.62 s, total 0.89 s — `x-proxy-cache: HIT` |
| _nuxt bundle (6347c71.modern.js, 335,966 B) | 200, TTFB 0.61 s, total 0.87 s; gzipped 89,581 B; `x-proxy-cache: HIT`, `Cache-Control: public, max-age=31536000` |
| All 17 _nuxt bundles, gzipped total | 403,909 B |
| Video Range 1 MB (post 504230, bytes 0–1,048,575) | 206, TTFB 0.78 s, total 1.79 s, 586 KB/s |
| Video Range 1 MB (bytes 1,048,576–2,097,151) | 206, TTFB 0.43 s, total 1.31 s, 801 KB/s |
| Railway edge | `x-railway-edge: sjc1` (San Jose, US West) — service in default region us-west1 |
| Total first-load weight (gzipped, what the phone actually downloads) | homepage 123 KB + bundles 404 KB ≈ **527 KB ≈ 0.8 s on the user's 5.3 Mbps line** |

## Real bug found: the homepage cache expired mid-cycle

- `/` (and `/playlist`, `/search`, `/video` pages) were cached in `_RP_CACHE`
  with TTL **120 s**, but the v3.14 self-warmup loop re-fetched only every
  **300 s**. The entry expired ~180 s into every 300 s cycle, so the first
  real load in that window paid a full upstream fetch (~1.8 s cold).
- `_PAGE_CACHE` (900 s) is NOT involved — it serves only the legacy
  `/page?path=...` endpoint, which the app no longer uses.
- Response gzip to the client was already working (Railway edge compresses;
  the proxy itself never gzips — no fix needed there). Upstream gzip and the
  pooled keep-alive connections were already healthy; cache HITs confirmed
  live for homepage and bundles.

## Changes (proxy 3.16)

1. **Warmup 300 s → 120 s** (`_warmup_loop`).
2. **Homepage/page TTL 120 s → 300 s** (`handle_reverse_proxy`). Together
   with (1) the homepage stays warm continuously with margin. Safe:
   anonymous HTML shell only; the feed comes from `/api/rs/homepage`
   (30 s TTL) client-side; `Set-Cookie` is stripped on cache put.
3. **`/_nuxt/` TTL 3600 s → 86400 s** — filenames are content-hashed and
   immutable, so a day-long TTL is safe; protects warmth if the warmup
   thread ever stalls.
4. **Region us-west1 → europe-west4 (Amsterdam)** via `railway.json`
   `"deploy": {"region": "europe-west4-drams3a"}` (field confirmed in
   Railway's official schema at backboard.railway.app/railway.schema.json;
   current docs list the identifier as `europe-west4-drams3a`). Iran→EU is
   shorter than Iran→US by any route. NOTE: this VM is in Chicago, US, so
   VM-side timings after the move are expected to look the SAME or WORSE
   (VM→EU edge + EU service) — the win is for the user in Iran, and only
   his phone test can confirm it. The `x-railway-edge` header is the check:
   sjc1 (US) → an EU edge after the move.
5. `PROXY_VERSION` 3.15 → 3.16. No APK change needed.

## Honest first-launch floor on the user's 5.3 Mbps line

- Transfer: ~527 KB gzipped ≈ 0.8 s, plus API + thumbnails.
- WebView cold init: 1–3 s (phone-only, nothing server-side changes it).
- **Floor ≈ 2–4 s first launch.** Nothing below that is honest to promise.
- Video needs 4.27 Mbps of the 5.3 Mbps line; proxy Range throughput
  measured 586–801 KB/s from the VM (≈ line rate for the user).

## Verify after deploy

- `/health` 200, `X-Proxy-Version: 3.16`, `x-railway-edge` = EU edge.
- Homepage + bundle: `x-proxy-cache: HIT`, TTFB measured.
- Video Range 206, `/dub/gemini-status` configured:true.
