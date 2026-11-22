# NOTES v3.8 — 2026-11-22

## Problem (user, confirmed)
Video PLAYBACK is slow on BOTH mobile data and Wi-Fi. The site itself
and thumbnails load fast — only playback lags. v3.7 diagnosis (measured
from this VM, same real video): direct CDN and proxy are both fast
from the VM, so the phone path itself is the problem, not proxy
throughput. Three app-side causes found in the code:
1. The direct-CDN probe was boolean reachability only (1-byte Range)
   — it never measured speed, so the app could lock into the slower
   path for a whole session.
2. Direct video went through the app-side HttpURLConnection relay
   (HTTP/1.1, one connection per Range) — the exact bottleneck v3.6
   removed for the proxy path but left in place for direct.
3. Prefetch (first 1 MB) ran only on unmetered networks; on metered
   data only a 1-byte warm-up — playback started with nothing buffered.

## What changed
- **Per-video speed race (WebActivity.java):** on every video page
  open, the first 256 KB is fetched via direct CDN AND via the proxy
  in parallel; the faster leg wins, and its bytes become the prefetch
  (served from memory on the player's first Range request). Started
  from `onPageFinished` (800 ms delay) and, as a fallback, from the
  first video intercept (`ensureRaceFor`, waits max ~2 s). A leg that
  fails/times out loses; if both fail, video falls back to the proxy.
- **Cronet-native direct video:** when direct wins, the intercept
  returns a 302 redirect to the CDN URL, so the WebView's own Cronet
  stack (HTTP/2+HTTP/3, native Range/206, adaptive media buffering)
  fetches the video. The old `fetchDirect` HttpURLConnection relay
  is GONE. When proxy wins (or no race), the WebView fetches
  `/video?u=` straight from the proxy origin itself, natively.
- **Prefetch on metered networks too:** the race runs on metered
  data as well — 256 KB is the prefetch either way, and on a slow
  link the race is exactly what finds the faster path.
- Proxy: only `PROXY_VERSION 3.8` (no behaviour change); the
  subtitle/Gemini work from v3.7 is untouched.

## Verification (live, 2026-11-22)
- APK v3.8 (code 29, 136,413 bytes): apksigner v2+v3 OK, badging
  29 / 3.8, same cert SHA-256 `f5ace241…dc4dc1`. Dex contains
  `runVideoRace`, `raceLeg`, `redirectTo`, `ensureRaceFor`,
  `servePrefetched`, `maybeRaceVideo`.
- Same real video (`77f4c889…`, `cdn.xfree.com/xfree-prod/…/full.mp4`),
  this VM: 256 KB race size direct 0.63–0.83 s vs proxy 0.62–0.81 s
  (tied); 1 MB direct 1.09 s vs proxy 1.21 s; mid-file 1 MB seek
  direct 0.98 s vs proxy 1.01 s; warm homepage TTFB 0.55–0.64 s,
  total 0.81–0.94 s. Honest limit: from the VM the proxy is already
  at the direct ceiling, so the race's win is only provable on the
  user's phone in Iran, where the two paths actually differ.
- Proxy pushed (commit TBD), live `x-proxy-version: 3.8`.

## Still needs the user's own test (VPN off, mobile data AND Wi-Fi)
1. Open the app, open a video — it should start playing fast, with
   less/no stutter, on both networks.
2. Subtitles unchanged from v3.7 (tap «زیرنویس», Gemini via server
   key once `GEMINI_API_KEY` is set in the Render dashboard, no-key
   fallback until then).
