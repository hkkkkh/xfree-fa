# Proxy 3.19 — weak-line playback (transcode-once HLS) + speed

Date: 2026-11-28. Railway EU West. NO app install — the
already-installed v3.17 app gets light video through the proxy's
injected player script.

## The problem (verified)
xfree serves ONE rendition, ~4.27 Mbps, no lighter file
(converted.mp4 is 404). On a line below ~4.3 Mbps live playback
is physically impossible without a lighter rendition.

## What was built (proxy only)
- NEW proxy/hls.py: transcode-once. First request for a video
  starts ONE ffmpeg job (FIFO queue, max 1 concurrent) producing
  3 HLS renditions (240p ~300k, 360p ~600k, 480p ~1000k; H.264 +
  AAC, 4 s segments, aligned GOP/keyframes, EVENT playlists) + a
  progressive 360p MP4 (faststart). Cache on the ephemeral disk,
  LRU, 700 MB cap (Railway free disk ~1 GB/deployment).
- Routes: /lite/<name>/master.m3u8 (ABR master, blocks up to 8 s
  for the first segment, else 503 + retry), /lite/<name>/<rend>
  .m3u8 (grows while transcoding, no-cache), /lite/<name>/<rend>
  _%03d.ts (immutable), /lite/<name>/360.mp4 (only once the job
  is done, else 503), /lite-status (JSON: ffmpeg, cache bytes,
  jobs). /hls.js serves the pinned hls.js 1.5.13 light build
  (296 KB, immutable, cached by the WebView).
- Injected player script (xfree HTML only, NOT tik/red): on the
  main video element it swaps the src to the light HLS master
  (native HLS if the WebView supports it, else hls.js from this
  proxy). video.currentSrc is overridden (own getter) to keep
  returning the ORIGINAL mp4 URL, so the app's subtitle engine
  (which reads currentSrc) keeps decoding the original file's
  audio on the same timeline. Fallback chain: HLS -> light 360p
  MP4 -> original mp4. Any failure leaves the original playing.
- Dockerfile: ffmpeg added (apt-get, Debian bookworm 5.1).
- The source URL is deterministic from the media name
  (cdn.xfree.com/xfree-prod/<n0>/<n1>/<n2>/<name>/full.mp4),
  so the per-video cache key is stable. A job probes the source
  once (single ffprobe: dims + audio presence) for the master
  playlist RESOLUTION and the audio maps (silent videos get
  video-only outputs).

## Measured (VM, ffmpeg 8.1.2, real 337 s video 720x1152)
- One ffmpeg invocation (3 HLS + 360p MP4, veryfast, threads 1
  per output): 156 s wall = 2.16x real-time; 85 segments of 4 s
  per rendition; 79-83 MB on disk; 360p MP4 10.5 MB.
- Time to first segment (cold, single-probe build): master.m3u8
  in 4.5 s on the VM (6.0 s with the old two-probe version).
  Throttled to 1.5 Mbps: first 240p segment (166 KB) follows
  immediately; 240p needs only ~250 kbps, so it plays with a
  large margin on a 1.5 Mbps line.
- Egress per full play (this video): 240p ~10 MB, 360p ~13 MB,
  480p ~46 MB vs 177 MB original (4-17x less; quota lasts 4-17x
  longer depending on rendition).
- Master playlist RESOLUTION is computed per video from the
  probe (verified: 720x1152 -> 300x480 / 226x360 / 150x240;
  a second, landscape-ish video -> 270x480 / 202x360 / 136x240).
- nameOf() unit-checked in node: /video?u= form and raw CDN form
  both give the UUID; master.m3u8 / tik-porn URLs give null (no
  re-arm loop, tik videos untouched). Injected JS passes
  node --check. Served homepage contains the script (6x
  __xfLite) + both pills.
- Repeat play: master + segments served from cache instantly.

## Speed knobs — MEASURED, and deliberately NOT changed
- Pool MAX_PER_HOST 6 -> 12: NO WIN. Chrome opens max 6
  connections per host, so the pool already matches the
  browser's concurrency; more slots cannot shorten a burst
  (the wall time is set by the slowest fresh-TLS request either
  way). Kept at 6.
- TCP_NODELAY: NO MEASURABLE WIN. Local /health TTFB is already
  0.5-1.4 ms without it; live TTFB (~0.5 s) is network RTT
  noise. Not implemented.
- VIDEO_CHUNK 1 MB -> 64 KB: first-byte saving ~50 ms at most
  (warm-pool video Range TTFB is already 64 ms locally), costs
  16x write syscalls on a small CPU. Kept at 1 MB.
- Brotli: NOT in Python stdlib — would break the stdlib-only
  proxy for ~10% gain. Skipped.
- Cache-Control immutable on hashed bundles: upstream ALREADY
  sends max-age=31536000 on real _nuxt chunks. Nothing to add.
- Parallel upstream Range prefetch: unneeded once light
  renditions exist (per-Range TTFB 0.56 s vs 1 MB lasting ~11 s
  of 360p video). Skipped.
- Request collapsing: only helps under concurrency; this is a
  single-user proxy. Skipped.
- Baselines (live Railway 3.18, VM): /health TTFB 0.46-0.88 s;
  homepage warm TTFB ~0.55 s, total ~0.76 s (468 KB); 17 _nuxt
  chunks in parallel 3 s wall (fresh TLS per curl); video Range
  1 MB TTFB ~0.58-0.73 s, total ~1.0-1.2 s; /tik/ warm 0.84 s;
  /red/ warm 0.55 s. Honest first-launch floor on the user's
  5.3 Mbps line stays ~2-4 s (2 MB of HTML+JS + WebView init);
  the HLS work does not change that floor, it changes what
  happens AFTER the page loads (video starts in seconds on a
  weak line instead of stalling).

## Live verification (Railway, after deploy)
- (fill in: /health 3.19, /hls.js 200, /lite-status ffmpeg:true,
  a LIVE transcode on Railway for the example video: time to
  first segment, job state, disk used, 360.mp4 Range 206 after
  done; xfree / + /tik/ + /red/ still 200 with pills;
  /dub/gemini-status configured:true; video Range 206.)

## Honest limits (phone-only)
- Whether HLS actually plays in the app's WebView (native vs
  hls.js path) is UNPROVEN until the user's phone test — the
  360p MP4 fallback + original mp4 cover a refusal, and the
  report must state which path the phone took.
- Whether the Railway trial container can transcode without OOM
  (exit 137) is UNPROVEN until the first live transcode — the
  live check above is the test. If it OOMs, the job fails and
  the original mp4 plays (no user-visible breakage).
- First play of a long video still waits for the first segment
  (~5 s on the VM; slower on Railway — measure live). A video
  queued behind another transcode falls back to the original
  mp4 for that play.
- Subtitles during HLS playback rely on the currentSrc override
  (the engine decodes the original mp4's audio in parallel).
  If the override ever fails on the phone, the tap reads a
  blob:/m3u8 URL and subtitles fail for that play — the video
  itself keeps playing. Only the phone test proves it.

## Pre-existing observation (NOT changed in 3.19)
- The \u002F-escaped CDN rewrite in _rewrite_body (line ~1269)
  is a latent no-op: `esc` is built with double backslashes
  (`https:\\u002F\\u002F`) but the served HTML contains single
  backslashes (`https:\u002F\u002F`), so the site's `cdnUrl`
  config string is never rewritten and runtime-built video URLs
  stay raw CDN URLs (the app's network intercept routes them
  through the proxy). The lite script handles the raw-CDN form,
  so this does not affect 3.19. Left alone deliberately: fixing
  it would change video src forms app-wide for no user-visible
  gain.
