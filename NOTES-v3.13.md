# NOTES — v3.13 (2026-11-24 ~03:35)

## ROOT CAUSE — why video NEVER played in v3.12 (proxy healthy, site + thumbs fine)

**Decisive fact (2026-11-24 03:00): the user opened the Railway proxy site in
Chrome on the phone and the VIDEO PLAYS.** Same site, same proxy, same phone
— Chrome plays, the app's WebView doesn't. So the fault was in the app side,
not the proxy or the network.

**Pinned cause — the proxy answered 501 to CORS preflights (OPTIONS):**
- The site's player element is `<video … crossOrigin="anonymous">`
  (verified in the served video page HTML). Its Range fetch is therefore a
  CORS fetch.
- `Range` was NOT a CORS-safelisted header before Chrome 111, so an older
  System WebView sends a CORS PREFLIGHT (`OPTIONS`, with
  `Access-Control-Request-Headers: range`) before the video GET.
- `proxy/app.py` had NO `do_OPTIONS` — BaseHTTPRequestHandler answered
  **501 Not Implemented with no CORS headers** (proved live 2026-11-24:
  `OPTIONS /video?u=…` → 501). The preflight failed, the media element
  errored, video NEVER played.
- Up-to-date Chrome (Range safelisted since M111) sends NO preflight, so it
  played the same URL through the same proxy — exactly the observed split.
- Thumbnails are plain `<img>` (no crossorigin, no Range) so they never
  preflight — which is why ONLY video broke while thumbs loaded fast.

**Ruled out by inspection (all verified, not guessed):**
- Video URL construction: player builds `cdnUrl + name[0]/name[1]/name[2]/
  name/full-d.mp4` (`videopreview-d`); served pages carry cdnUrl rewritten
  to the Railway proxy (0 onrender refs, 187 railway refs). `full-d.mp4`
  is faststart (moov at byte 32, H.264 Main 720×1152) and serves 206.
- App `shouldInterceptRequest`: proxy `/video?u=` → native fetch (same as
  Chrome); no race/prefetch/302 left (all removed in v3.10, verified gone).
- Proxy `/video` handler: Range/If-Range forwarded, pooled streaming,
  206 + Content-Range correct (verified from VM incl. phone-like headers
  and abort-then-reseek sequence). Pool: 6 slots/host + ephemeral overflow,
  no blocking. HTTP/1.1 + ThreadingHTTPServer (no HTTP/1.0 issue).
- DubBridge: read-only observers only (no AudioContext since v3.9);
  SubtitleEngine: manual-only, file-decode, never touches the element.
- Manifest: hardware acceleration defaults on; codec is universal H.264.

## FIX (v3.13, minimal, at the cause)

1. **proxy/app.py — `do_OPTIONS`** (new, next to do_GET/do_HEAD): answers
   every preflight with 204 + `Access-Control-Allow-Origin: *`,
   `Allow-Methods: GET, HEAD, OPTIONS`,
   `Allow-Headers: Range, If-Range, Content-Type, Authorization,
   X-Request-Id`, `Max-Age: 86400`. `PROXY_VERSION` 3.12 → 3.13.
   Verified locally before push: `OPTIONS /video?u=…` → 204 with the
   CORS headers; `/health` → 200.
2. **WebActivity.java — `cdn.xfree.com` branch** (~line 759): an unrewritten
   cdn URL used to be fetched DIRECT by the WebView (`return null`) — but
   cdn.xfree.com is unreachable from the phone in Iran (proved 2026-11-24:
   ERR_TIMED_OUT), so that hung forever. Now routed through the proxy's
   `/video` relay (`fetchViaProxy(Proxy.videoUrl(url), request)`) — the
   same path rewritten player URLs already use. Secondary hardening; the
   served pages are fully rewritten so this branch should never fire.

## APK — v3.13 (versionCode 34)

**Deliverable:** [xfree-fa-3.13.apk](sandbox://workspace/your_files/xfree-fa-3.13.apk)
— 214,237 bytes, `com.xfree.fa`, versionCode 34 / versionName 3.13, same
keystore + same cert SHA-256 `f5ace241…dc4dc1` (installs over v3.12 without
uninstalling). apksigner v2+v3 verified; aapt2 badging confirms package +
versions. Dex check: 14 occurrences of the Railway host, 0 `onrender`,
0 `AIza` (no key in the APK).

**Changes (app code):** the `cdn.xfree.com` intercept fix above + **launcher
icon = the USER'S ORIGINAL logo, black background removed to transparency**
(user rejected both redesigns — the glossy 3D one and the PRO glassmorphism
one — and wants his own logo only):
`logo/xfree-fa-logo-original-transparent.png` (1254×1254 RGBA, the exact
purple X he supplied, corners alpha 0) scaled to all densities (mdpi 48 …
xxxhdpi 192) for both `ic_launcher.png` and `ic_launcher_round.png` — no
cropping, no added background, no black square. Verified in the built APK:
all 10 icon files present as RGBA with transparent corners (aapt2
re-encoding changes at most a few pixel values by ≤13/255 — no flattening).
No adaptive-icon XML in this project (plain PNG mipmaps), so the transparent
PNG shows alone.

**Live verification from the VM (after Railway auto-deploy):** _to be filled
in once the 3.13 proxy is live_ — `/health` → 200, homepage → 200,
`OPTIONS /video?u=…` → 204 with CORS headers, 1 MB video Range → 206,
`/dub/health` → dub 2.0, `/dub/gemini-status` → `configured: false`
(expected — no key yet), `X-Proxy-Version: 3.13`.

## Still open (not this build)

- User must re-enter `GEMINI_API_KEY` on Railway Variables — until then
  subtitles work via the keyless HTTP `/dub/stt` fallback only.
- Direct-CDN video is DEAD (phone test 2026-11-24 02:41: ERR_TIMED_OUT) —
  video stays proxy-relayed; the Railway $5 trial (~50–80 GB) is the bridge,
  Render returns Dec 1 with a fresh 5 GB.
- Phone test needed for v3.13 (VPN off, both networks): site loads
  immediately; video plays past 0:06 with sound; tap «زیرنویس» on a video
  where someone actually talks → Persian captions at the bottom.
- The app's real System WebView version is unknown (the app's UA string is
  a fixed Chrome/131, hiding the real engine version). If video still fails
  after the OPTIONS fix, the next evidence needed is the phone's logcat
  (`WV_CONSOLE` VIDEO_EVENT lines) and the «تست سرعت» Toast text.
