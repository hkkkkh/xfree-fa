# NOTES v3.5 — 2026-11-22

## What the user reported (v3.4)
- Subtitle button broken, no subtitles appear.
- Execution very slow.

## 1. Subtitle button — ROOT CAUSE (proved, fixed)
`proxy/app.py` `do_POST` read the request body (`rfile.read(length)`)
and then `dub.py` `handle_dub()` read `rfile` AGAIN for `/dub/stt`,
`/dub/translate`, `/dub/segment`. The second read blocks forever on a
keep-alive connection (body already consumed) — POST hung until the
client timed out (HTTP 000), while GET `/dub/health` worked fine.
Local smoke tests passed because `dub.py`'s standalone server calls
`handle_dub` directly without pre-reading.

Fix: `/dub/*` is now routed in `do_POST` BEFORE the body is read.
`handle_dub` reads the body itself.

Verified locally against the real `app.py`:
- before fix: POST /dub/stt hung >12 s (timeout).
- after fix: POST /dub/stt returns 200 in ~1.2 s; real English clip
  "hello this is a short test" recognised, confidence 0.98.
- Full subtitle chain: STT (proxy) + MyMemory translate (works from
  this VM: «سلام، این یک آزمون کوتاه است»). Note: Google gtx is now
  ALSO blocked from this VM's IP ("Sorry..." page); the phone-side
  gtx path (user's own mobile IP, first in the app chain) is the
  real path, MyMemory second, proxy /dub/translate last resort
  (still 429 from Render's IP).

## 2. Speed — v3.4 wake-gate was the bottleneck
The gate waited for `/health` first (up to ~3 min) before the site
even started loading, and each ping from Iran can burn its 10 s
connect timeout. Keep-alive cron is healthy (every 6 min, last run
succeeded), so the proxy is warm in the daytime — the gate was pure
serial delay.

Fix (WebActivity): site loads IMMEDIATELY on launch; the load itself
wakes Render. On main-frame error, retry the load directly first
(4 x 3 s, guarded against double-scheduling); the 3-min wake-gate
is now only the fallback after 4 direct retries.

## 3. Subtitles MANUAL ONLY (user correction, reverses v3.4 auto-start)
DubBridge: auto-start removed entirely. The «زیرنویس» button is
always visible on video pages; tap once to start Persian subtitles,
tap again to stop. A new video starts with subtitles OFF. Dubbing
stays removed; original audio never touched.

## Build
- v3.5, versionCode 26, same package `com.xfree.fa`, same keystore
  (SHA-256 f5ace241…dc4dc1). apksigner v2+v3 verified.
- Dex checked: `retryScheduled` present, `autoDone` absent,
  no-arg `startSub()` present.

## Still needs the user's own test (VPN off)
1. Open the app — site should come up fast.
2. Open a video where someone talks, tap «زیرنویس» — Persian
   subtitle line should appear within ~5 s; tap again to turn off.
3. Subtitles must NOT start by themselves on any video.
