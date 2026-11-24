# NOTES — v3.15 (2026-11-24 ~01:00)

## What the phone proved (v3.14 user test, 2026-11-24 ~04:17)

- v3.14 "opens fast and runs, still room to improve" — video + search
  fixed, but TWO new complaints:
  1. **Launcher icon has a WHITE background** (screenshot: purple 3D X
     centred on a white rounded square, label «ایکس فری فارسی» below,
     on a dark wallpaper). User: "just the logo, it must have NO
     background at all."
  2. **Subtitles start very late and aren't in sync with the audio.**
     User: "make it smart — they shouldn't come for every video;
     check which video has speech, translate the speech into
     subtitles, fast and simultaneous."
- App name: user answered «0» — NO name under the icon, just the
  icon alone (NOT the literal string "0").

## PROBLEM 1 — white icon background: root cause + fix

ROOT CAUSE (verified in the built v3.14 APK): the app shipped ONLY
legacy PNG mipmaps (`ic_launcher.png`, RGBA, transparent corners —
verified corner alpha 0). Android 8+ wraps a legacy icon in an
adaptive-icon mask and fills the background WHITE by default. The
white square was the SYSTEM's filler, not the PNG — no white was
baked into any icon file.

FIX (app only, no proxy change):
- `res/mipmap-anydpi-v26/ic_launcher.xml` + `ic_launcher_round.xml`
  (new): proper `<adaptive-icon>` with
  `<background android:drawable="@android:color/transparent"/>` and
  `<foreground android:drawable="@mipmap/ic_launcher_foreground"/>`.
- Foreground PNG re-padded with PIL: the source logo
  (`logo/xfree-fa-logo-3d.png`, 1600×1600) has the X spanning bbox
  (245,169,1364,1438) = 1119×1269 px (~70–79% of the canvas), which
  the adaptive mask (safe zone = central 66dp of 108dp = 61%)
  would CROP. The X was scaled ×0.769 and centred on a transparent
  1600×1600 canvas (new bbox 858×974 px, ≤61%), then downscaled
  with LANCZOS to `res/mipmap-*/ic_launcher_foreground.png`
  (mdpi 108, hdpi 162, xhdpi 216, xxhdpi 324, xxxhdpi 432). The X
  itself is NOT redesigned — only padded.
- Legacy PNG mipmaps kept for pre-26 devices.

VERIFIED in the built APK: `aapt2 dump xmltree` shows the compiled
adaptive icon with background `@0x0106000d` (framework transparent)
and foreground `@0x7f020001`; all 5 foreground PNGs have corner
alpha 0 / centre 255; legacy icons still corner alpha 0; no white
background baked anywhere in the icon files.

## APP NAME — blank launcher label

- `res/values/strings.xml`: new `launcher_label` = U+200B
  (zero-width space — a NON-empty string, so no launcher falls
  back to the package name, that renders as nothing).
- `AndroidManifest.xml`: BOTH the application and the launcher
  activity now use `android:label="@string/launcher_label"`.
- VERIFIED: `aapt2 dump badging` shows `application-label:'<U+200B>'`
  (hex `E2 80 8B` in the output) and
  `launchable-activity: name='com.xfree.fa.WebActivity' label='<U+200B>'`
  — blank, NOT the Persian name, NOT `com.xfree.fa`.
- Residual risk (honest): a launcher that ignores the activity
  label AND the application label would fall back to the package
  name; no stock launcher is known to do that with an explicitly
  set non-empty label. Phone test will confirm on the Redmi.

## PROBLEM 2 — subtitles late / out of sync / not smart: fixes

Current pipeline (verified in code before changing): manual-only
start (tap «زیرنویس» — KEPT, never auto-start); SubtitleEngine
decodes the mp4 audio track via MediaExtractor/MediaCodec, streams
to the proxy Gemini Live relay first (GEMINI_API_KEY is still NOT
re-entered on Railway, so today only the keyless HTTP /dub/stt
fallback runs); captions at the bottom; original audio untouched.

Why the first caption was so late (measured/known latencies):
- Gemini Live cold start ~21.5 s to first caption (measured live).
- The engine waited for TWO failed WS attempts before falling
  back to HTTP — each failed connect is a TLS handshake round-trip.
- HTTP fallback accumulated 4 s chunks (first caption needs 4 s
  of audio + POST ~1.2 s + on-phone translate).
- The extractor + codec open over the network happened only AFTER
  the tap.

FIXES (all in `SubtitleEngine.java`, `DubBridge.java`,
`WebActivity.java` — client-side only, NO proxy change):

(a) SPEECH GATE (smart): every decoded 1 s window gets an RMS
    energy check (`rmsOf()`, threshold 250 on the s16 scale).
    - If the first ~5 s of a run have no window above threshold,
      the engine stops with ONE short note as the button label:
      «در این ویدیو حرفی نیست» — no STT calls, no captions.
    - While running, silent windows are never sent to STT.
    - LIMIT (stated honestly): this is an ENERGY gate, not true
      speech recognition — loud music also passes it; the server
      STT still returns empty for music, so no caption appears.
      True speech-vs-music detection would need a model, which
      does not fit this app.
(b) LOWER LATENCY:
    - HTTP-fallback chunks 4 s → **2 s** with 0.5 s overlap
      (stride 1.5 s): `FALLBACK_CHUNK_BYTES` 128000 → 64000,
      `FALLBACK_KEEP_BYTES` 24000 → 16000, `FALLBACK_STRIDE_S`
      3.25 → 1.5; `endMs` now derives from the chunk constant.
      2 s is still enough audio for reliable STT (1 s windows
      proved too short — fragments, frequent empty results).
    - ONE failed Gemini attempt (was two) switches to HTTP
      fallback (`noteGeminiFail` threshold 2 → 1). The background
      reconnect still retries the WS, and the WS is only trusted
      again after a reconnect is CONFIRMED by a real transcript.
      Gemini relay stays first when it works (~2–3 s captions).
(c) SYNC: `postCaption()` now drops any caption whose audio END
    (t0 + duration: 2 s fallback chunks, 1 s WS windows) is more
    than ~2 s behind the current video clock — a caption for
    speech that already passed is never shown late. One caption
    at a time (single caption box, unchanged).
(d) PRE-WARM (manual-only preserved): when a video page's main
    player appears, the page JS calls `XfSub.onPrewarm(src, t)`
    and the engine starts decoding the file's audio in the
    background (decode only — no WS, no STT, NO captions; the
    clock is reported via new `XfSub.onClock()` every 500 ms so
    the decoder chases the real position with the same >2.5 s
    seek logic). On tap, `start()` reuses the already-open
    extractor + codec and re-seeks to the tap position — the
    first caption then only needs STT + translate. If the
    pre-warm already decoded ≥5 s with no audible window, the
    tap immediately shows «در این ویدیو حرفی نیست» instead of
    starting STT. Pre-warm queue is capped at 6 windows and
    only runs for main-player video pages (feed previews never
    trigger it), so the extra proxy traffic is ~200 KB per
    video page at most. If pre-warm state is ever inconsistent,
    `start()` falls back to the old cold start.
    - One real bug caught in review before building: the decode
      loop's `if (inIdx >= 0 && running)` guard would have
      starved the pre-warm decoder (it dequeues input buffers
      forever and decodes nothing) — fixed to
      `(running || prewarming)`.

KEPT: original audio never touched/cut, bottom TikTok-style
captions, manual-only start, no dubbing, no wake screen, site
loads immediately. No proxy change — PROXY_VERSION stays 3.13
(`/dub/stt` accepts arbitrary PCM; no new parameter needed).

## Build + verification

- APK: `xfree-fa-3.15.apk` — **419,670 bytes** (was 214,237 in
  v3.14; +~205 KB is the 5-density foreground PNG set),
  md5 `d7d7e36fdb46e87f20589d754618130f`, `com.xfree.fa`,
  **versionCode 36 / versionName 3.15**, same keystore + same
  cert SHA-256 `f5ace241…dc4dc1` (installs over v3.14).
- apksigner **v2+v3 verified** (v1 false, same as all builds);
  aapt2 badging confirms package + versions.
- Dex: 14 Railway host refs, **0 `onrender`, 0 `AIza`** (no key
  in the APK); prewarm/onPrewarm/onClock code present.
- `version.json` bumped to 36 / 3.15; pushed to `hkkkkh/xfree-fa`
  main with this note (commit TBD — see parent agent's push
  record). No proxy code change, so no Railway redeploy needed.

## Still phone-only to test (user does it himself — VPN off)

Install over v3.14, VPN off:
1. Launcher icon: the purple 3D X ALONE on the wallpaper — no
   white square, no name under it. (If a white square is still
   there, the launcher cached the old icon — remove/re-add it
   from the home screen once.)
2. Site loads immediately; video plays past 0:06 with sound.
3. Subtitles: tap «زیرنویس» on a video where someone actually
   talks → first Persian caption should arrive noticeably faster
   than v3.14 and track the speech; on a music-only / silent
   video → the button should show «در این ویدیو حرفی نیست» and
   no captions appear.
4. Gemini captions (fast simultaneous path) still need the user
   to re-enter `GEMINI_API_KEY` in Railway Variables — until
   then subtitles run on the keyless HTTP `/dub/stt` fallback
   only (~2–4 s per caption).
