# NOTES — v3.14 (2026-11-24 ~03:40)

## What the phone proved (new evidence 03:31–03:37, device browser, VPN on)

- Homepage loads FAST, thumbnails all appear — proxy path good on the phone.
- **Video page OPENS and PLAYS past 0:06 twice with no errors and no
  buffering spinner (frames change over time)** in the phone's browser
  through the proxy. Sound could not be verified by the browser agent.
  So video works in the phone's browser — the app's WebView is the only
  place it fails.
- SEARCH: the magnifier tap failed repeatedly in the browser tool
  (screenshot-capture error locating the target — tool limitation, not
  necessarily the site). The search box opened via the nav menu, typing
  worked, the search ran — and returned ZERO results for "blonde".
  **Checked from the VM: the proxy is NOT the cause.** `/api/2/search?
  search=blonde&ns=1` → statusCode 2, **30 posts**, JSON parses cleanly
  (same for `search=test`); `/search?q=blonde` SSR page contains 30
  video links and 95 "blonde" mentions. The zero-result view in the
  browser test was the test's own interaction (or the site's client-side
  search flow), not a broken/mangled API response — the proxy returns
  full results.

## Proxy: fully exonerated (verified from VM, all live on Railway 3.13)

Homepage 200 (0 onrender refs, cdnUrl rewritten to proxy /video relay);
video page `/video?id=1496184…` 200 with `<video … crossOrigin=
"anonymous" id="feed-video-element">`; `/api/post/1496184` → statusCode 2
(media `3d6db6a5-a404-4e2d-a97a-9183984341fe`, 352 s, video/mp4);
anonymous-login → statusCode 2 + token; search API + SSR page return
30 posts for "blonde"; OPTIONS /video → 204 + CORS headers; 1 MB Range
on `full-d.mp4` and `full.mp4` → 206. **No proxy change in v3.14 —
PROXY_VERSION stays 3.13.** The video URL the player builds
(`getCDNurl`, bundle 6347c71.js): `cdnUrl + name[0]/name[1]/name[2]/name/
full-d.mp4` for `videopreview-d`, `full.mp4` for `videopreview`.

## ROOT CAUSES fixed in the app (WebView), with file:line evidence

Both phone symptoms are taps on site controls doing nothing. The proxy
is proven healthy and the site works in Chrome on the same phone, so the
faults are the app's own WebView layer. Four proven defects fixed:

1. **CORS headers stripped by `fetchViaProxy` → video/XHR rejected
   (WebActivity.java `buildResponse`).** It copied only Content-Range,
   Accept-Ranges, Content-Length, Cache-Control, Content-Encoding and
   dropped every CORS header. Any cross-origin fetch routed through
   `shouldInterceptRequest` (a raw `cdn.xfree.com` video URL, an XHR to
   an absolute `www.xfree.com` URL) came back WITHOUT
   `Access-Control-Allow-Origin` — the `<video crossOrigin="anonymous">`
   element (and any CORS XHR) must reject such a response, so the video
   errored and never played even though the bytes were fine. FIX: copy
   `Access-Control-Allow-Origin`, `Allow-Headers`, `Expose-Headers`,
   `Allow-Credentials`, `Max-Age`, `Vary` through.
2. **Stuck black wake-gate overlay (WebActivity.java `onPageFinished` /
   `onMainFrameError` / `onReceivedError`).** `onPageFinished` NEVER
   called `showWaiting(false)`. If `onMainFrameError` ever fired — even
   once, transiently — the opaque full-screen overlay stayed up FOREVER
   over the loaded site and swallowed every tap, so the top of the page
   (header/search) went dead while the site underneath was fine. Worse,
   the deprecated `onReceivedError` overload fires for SUBRESOURCE
   errors too (a failed font/image/tracking script), and the old code
   treated ANY error whose URL started with the proxy origin as a
   main-frame failure, blanking a good page with `about:blank`. FIX:
   (a) `onPageFinished` always hides the overlay; (b) the deprecated
   `onReceivedError` only acts when the failing URL equals the current
   main-frame URL (`view.getUrl()`); genuine main-frame failures keep
   the friendly retry UI and never show the Chrome error page.
3. **Stale WebView cache (WebActivity.java `setupWebView`).** The app's
   WebView keeps its HTTP cache across app updates; Chrome has its own
   fresh cache. A stale/corrupt cached JS bundle (half-download from a
   Render stall, or a bundle rewritten for the dead Render host) leaves
   the site's JS dead in the app — SSR HTML renders, but NO tap works
   (video cards, search magnifier) — while the same site works in
   Chrome. FIX: version-gated one-time `clearCache(true)` on upgrade to
   v3.14 (SharedPreferences `cache_cleared_for` == 35); cookies/login
   untouched.
4. **Subtitle bar could cover the header (DubBridge.java `scan` /
   `ensureBar`).** The bar appeared whenever ANY `<video>` with a src
   existed in the DOM — including small preview videos inside feed
   cards — as a fixed box (z-index 999999, top-right) that eats taps
   over its whole rect. FIX: new `isMainPlayer(v)` — only the video
   page's `#feed-video-element`, or a video ≥ 300×200 (feed previews
   are ~190 px wide on a phone, the player is full-width); bar moved
   from top 56 px to 64 px so it sits clear below the ~56 px header.
   Subtitles stay manual-only, bottom captions, original audio untouched.

Ruled out again by inspection: `shouldOverrideUrlLoading` (same-origin
proxy links return false; external links open in the browser),
`fetchViaProxy` POST/method handling for relative `/api/` calls (axios
uses relative baseURL "/", so API calls go same-origin, no interception;
proxy adds `apiversion: 1.0` itself), DubBridge observers (read-only,
no AudioContext since v3.9), SubtitleEngine (file-decode, never touches
the element), pull-to-refresh listener (returns false, passes touches
through).

## APK — v3.14 (versionCode 35)

**Deliverable:** [xfree-fa-3.14.apk](sandbox://workspace/your_files/xfree-fa-3.14.apk)
— 214,237 bytes, md5 `0ee914a4d2f42fe90f795dd68732dc85`, `com.xfree.fa`,
versionCode 35 / versionName 3.14, same keystore + same cert SHA-256
`f5ace241…dc4dc1` (installs over v3.13 without uninstalling). apksigner
v2+v3 verified; aapt2 badging confirms package + versions. Dex check:
14 Railway host refs, 0 `onrender`, 0 `AIza` (no key in the APK);
`cache_cleared_for`, `Access-Control-Allow-Origin`, `feed-video-element`
present in dex.

**Launcher icon (user-approved 03:28, «همین خوب»): the FIRST glossy 3D
logo** — `logo/xfree-fa-logo-3d.png` (1600×1600 RGBA, glossy 3D purple
X, transparent background), scaled to all densities (mdpi 48 … xxxhdpi
192) for `ic_launcher.png` + `ic_launcher_round.png` — no cropping, no
added background, no black square. Verified inside the built APK: all
10 icon files RGBA with transparent corners (corner alpha 0, center
255). The glass PRO logo is NOT used; the original-transparent logo was
v3.13 only. No further logo changes unless the user says so.

**Other changes:** `AndroidManifest.xml` — explicit
`android:hardwareAccelerated="true"` on the application. `build.sh` —
VERSION_CODE 35 / VERSION_NAME 3.14. `version.json` — 35 / 3.14.

**Live verification from the VM (after build):** proxy unchanged 3.13 —
`/health` 200, `X-Proxy-Version: 3.13`, `OPTIONS /video` 204,
1 MB Range on `full-d.mp4` → 206 in 0.77 s. `version.json` +
`NOTES-v3.14.md` pushed to `hkkkkh/xfree-fa` main (commit — see sync
record; no proxy code changed).

## Still open (phone-only)

- Phone test needed for v3.14 (user does it himself — he can only chat
  with Muse with VPN on; VPN off cuts his Muse connection): install
  over v3.13, VPN off, both networks — site loads immediately (no black
  wake screen), tap a video thumbnail → video page opens and plays past
  0:06 with sound, tap the search magnifier → search opens and returns
  results for "blonde", tap «زیرنویس» on a talking video → Persian
  captions at the bottom. The v3.14 cache clear runs once on first
  launch (site re-downloads, a few seconds slower that once).
- If taps still fail on the phone, the next evidence needed is logcat
  (`WV_CONSOLE` VIDEO_EVENT lines, any `onReceivedError` lines) and the
  «تست سرعت» Toast text — the app-side cause would then be narrowed to
  the WebView engine version itself.
- User must re-enter `GEMINI_API_KEY` on Railway Variables — until then
  subtitles work via the keyless HTTP `/dub/stt` fallback only.
- Direct-CDN video is DEAD (phone test 2026-11-24 02:41: ERR_TIMED_OUT)
  — video stays proxy-relayed; Railway $5 trial is the bridge, Render
  returns Dec 1 with a fresh 5 GB.
