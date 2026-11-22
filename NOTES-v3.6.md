# NOTES v3.6 — 2026-11-22

## User report (v3.5)
- Subtitles still broken + execution slow.
- Relayed ChatGPT prompt 1: live Persian subtitles — floating button,
  Audio Playback Capture, short chunks, WebSocket to Render, streaming
  STT, fast Persian translate, interim + final subtitles with precise
  timestamps, concurrent processing, small buffer, dedup, auto-reconnect,
  error handling, current Render server, lowest latency.
- Relayed ChatGPT prompt 2: site load + video playback in WebView very
  slow — separate HTML/CSS/JS/images from video, video direct from CDN,
  keep Range/206, HTTP/2/3, static cache, WebView optimize + preconnect,
  adaptive buffer, remove duplicate requests, measure playback start.
- Correction (screenshot 1000300657): NO Google Translate page-translation
  popup for subtitles — subtitles must be a caption line at the BOTTOM
  of the video itself, like Instagram/TikTok, synced with speech.

## 1. Subtitles — WebSocket streaming (dub 1.5)
- Proxy: GET /dub/ws (stdlib, in dub.py). App streams 1 s PCM windows
  over ONE persistent wss connection; server STTs each 1 s window +
  0.5 s interim window in a 2-worker pool, returns interim/final JSON
  with precise video timestamps (mark t0 + audio seconds).
- Measured locally (real English clip, via real app.py): interim
  "hello" 0.7 s, final "hello" 0.9 s, final "this is a short test" 1.66 s.
- True streaming STT is not free without a key — closest real thing:
  1 s windows + 0.5 s interims, concurrent STT. No invented subtitles.
- Audio Playback Capture (MediaProjection) NOT used: it captures OTHER
  apps' audio and needs a system consent dialog the user never approved;
  the video plays inside our own WebView, so JS AudioContext capture
  (no permission needed) is the correct path.
- HTTP POST /dub/stt (1 s chunks) stays as fallback; auto-reconnect
  with backoff (1s/2s/4s, then HTTP fallback); errors shown on button.
- Dedup: a final identical to the previous final is skipped; interims
  equal to the last final are skipped.
- Translate still ON THE PHONE (gtx direct -> MyMemory -> proxy
  /dub/translate) — Render's egress IP is burned for translate.
- Caption anchored to the video's on-screen rect (bottom of the video,
  Instagram/TikTok style), repositioned every 400 ms + on scroll.
  Native fullscreen: in-page overlay is hidden behind the custom view,
  so captions are mirrored to a native TextView via the XfSub
  JavascriptInterface (added to decor view while fullscreen).
- Fixed: ensureBar crashed when document.body was null (injection
  before <body> existed) — scan is now guarded; that could silently
  kill the subtitle button entirely.
- Subtitles MANUAL ONLY (unchanged): tap «زیرنویس» to start, tap again
  to stop; a new video starts OFF. Original audio never touched.

## 2. Chrome translate popup REMOVED
- Proxy no longer injects Google Translate Element (TRANSLATE_INJECT
  deleted). That injection is what produced the Chrome translate
  bar/popup in the user's screenshot. The site now shows in its own
  language; Persian comes only from the subtitle overlay.

## 3. Speed — diagnose first (measured live from this VM)
- Warm home 0.62-0.97 s, /health 0.74 s; proxy video 1 MB 1.35 s
  @777 KB/s vs direct cdn 1.16 s @904 KB/s — proxy only ~16% slower
  than direct from here. The phone slowness was NOT the proxy path.
- Root cause found: the app re-fetched EVERY video Range request
  with HttpURLConnection (HTTP/1.1, fresh connection per request,
  256 KB app-side buffer) and streamed it to the WebView — a second
  hop that bypassed the WebView's own Cronet stack (HTTP/2 + HTTP/3
  at the proxy edge — Render/Cloudflare advertises alt-svc h3 — native
  Range/206, adaptive media buffering).
- Fix: when direct CDN is NOT proven, shouldInterceptRequest now
  returns null for /video?u= — the WebView fetches video straight
  from the proxy origin via Cronet. The intercept stays ONLY for the
  proven-direct case (skip the proxy entirely). Prefetch kept for
  the direct case.
- Direct-CDN probe: startup probe can fail transiently; re-probed
  when a video page opens (max once/60 s).
- HTTP/2/3: already at the edge (Cloudflare alt-svc h3); stdlib
  Python server is HTTP/1.1 behind it — nothing to change server-side.
- Static cache: /_nuxt/ already cached 3600 s server-side, upstream
  Cache-Control forwarded; WebView LOAD_DEFAULT respects it.
- Honest limit: Render Free cold wake is still ~30-60 s after 15+ min
  idle (keep-alive cron every 6 min covers daytime); the first
  Iran->Render connection can still be slow — now the load starts at
  once instead of after the gate (v3.5) and video no longer pays the
  app relay.

## Build
- v3.6, versionCode 27, same package com.xfree.fa, same keystore
  (SHA-256 f5ace241…dc4dc1). apksigner v2+v3 verified. Dex checked:
  dub/ws + xf-sub-bar + onCaption present.

## Still needs the user's own test (VPN off)
1. Open the app — site should come up fast, no translate popup.
2. Open a video where someone talks, tap «زیرنویس» — Persian caption
   at the BOTTOM of the video within ~3 s (grey italic interim, then
   white final); tap again to turn off. Also test in fullscreen.
3. Video should start faster and not stutter; Google login tap
   (v3.2 fix untouched).
