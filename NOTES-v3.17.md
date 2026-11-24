# XFreeFa v3.17 — Subtitle fix (pre-warm + PTS sync + lookahead)

**Built:** 2026-11-24 ~05:30–06:00 UTC. **versionCode 38, versionName 3.17.**
**APK:** `~/workspace/your_files/xfree-fa-3.17.apk` — 1,480,534 bytes,
md5 `4992f21404185c66507f6ace59a271e3`. Same keystore/cert as all versions
(SHA-256 `f5ace241…dc4dc1`) — installs over v3.16. apksigner: **v2 + v3 true**.
aapt2 badging: `com.xfree.fa` v38/3.17, label U+2800 blank, minSdk 24,
targetSdk 34. Dex: **0 × `onrender`, 0 × `AIza`**; Railway host present.
**Proxy: 3.15 (pushed 2026-11-24 05:26, commit `591f2f9e`), no change in
this build** — the app side of v3.17 only. Live re-verified after the
build: `/health` 3.15, `/dub/gemini-status` configured:true,
`/dub/gemini-diag` setup_complete.

## Why this version

v3.16 user test (2026-11-24 05:18): everything good EXCEPT subtitles —
on tap it showed «در حال اتصال» for a long time, first caption very late,
not in sync with speech. The user relayed the problem to Claude +
ChatGPT; their answers were evaluated first (see "Evaluated, not built"
below) and the one fix both agreed on is item 1.

## Live measurements (VM → Railway proxy → Gemini, JFK speech sample)

Pre-warmed session, first audio → first text: **2.75 s** (ready in 0.67 s
after WS 101; setupComplete ~0.17 s after the 101 — the Gemini handshake
itself is fast; the ~2.75 s is the model's context warm-up, unavoidable).
Interim transcripts arrive **incrementally** (phrase by phrase, English
input + Persian output alternating, ~0.5–1 s apart) — NOT a burst at
section end. Chunk size 40 / 100 / 200 ms: **2.74 / 2.81 / ~2.85 s** —
no measurable difference; **100 ms ships** (10 msgs/s, least overhead
for equal latency). Lookahead: audio at **2× → first text 1.70 s**,
**4× → ~1.5 s**, complete and correct transcripts, no errors — Gemini
accepts faster-than-real-time audio. **LOOKAHEAD_S = 3.0 ships.**
Test audio: JFK inaugural (16 kHz mono, looped 3× to 33 s) — the example
xfree video (post 504230) has NO recognisable speech at any probed
offset (30/90/150/210/270/320 s): music/moans only, consistent with
most xfree clips. HTTP `/dub/stt` on the JFK sample returned "no speech
recognised" (en-US then fa-IR) — the Google Web Speech v2 endpoint is
flaky on this sample; the Gemini path is unaffected and is the decisive
one.

## What changed

### Proxy 3.15 (already live, commit `591f2f9e`)
- `proxy/dub.py`: `_SESSION_T0` + `_dlog` appends `+<ms>ms` elapsed per
  session (`time.perf_counter()`); `{"type":"ready"}` sent to the app
  after GEMINI_CONNECTED (setupComplete); GoAway detection in
  `_gemini_reader` (logs `GEMINI_GOAWAY` with timeLeft, forwards
  `{"type":"goaway"}` to the app).
- `proxy/app.py`: `PROXY_VERSION = "3.15"` (line 176).

### App — `app/src/main/java/com/xfree/fa/SubtitleEngine.java` only

1. **Pre-warm the Gemini session** — `prewarm()` now also calls
   `ensureWarmWs(proxyUrl, startSec)` (new, ~line 1560): opens the
   phone→proxy WS + proxy→Gemini session on video-page open, completes
   setup, sends NO audio. `onWarmMessage` handles `ready` (sets
   `warmReady`/`warmReadyAt`), `goaway`/`error` (closes the warm
   session). `start()` checks `isWarmValid()` before any teardown and
   `tryAdoptWarm()` adopts the session (fresh mark at the tap position,
   keeps the warm `sid` so proxy logs join up). Idle rules: closed
   after 60 s without a tap (`onClock`, `WARM_IDLE_MS`), on page exit
   (empty src), on video change, and on `stop()`. One session per user.
   `teardownRun(keepWarm)` (new) lets start()'s cold branch kill the
   old run without killing the warm session. Manual-only start is NOT
   violated: no audio is sent before the tap.
2. **Staged connection states** — `setConnState()` (new): ST_PROXY
   «…در حال اتصال به سرور» → ST_GEMINI «…در حال اتصال به جمینای» →
   ST_STREAM «■ زیرنویس روشن». `ensureWs` sets ST_GEMINI on connect;
   `onWsMessage` sets ST_STREAM on `ready` (or on first transcript,
   if `ready` was missed). No more vague «در حال اتصال».
3. **Stage timing logs** — `XF_TIME` tag, `SystemClock.elapsedRealtime()`:
   `DECODE_FIRST_PCM` (decodeLoop), `FIRST_PCM_SENT`, `WS_OPEN`,
   `SESSION_READY`, `FIRST_CAPTION` (all with "+Xms since tap" and
   firstAudio→firstCaption), `CAPTION_RENDERED` (in `showCaptionNow`).
   Proxy side (3.15): per-session `+<ms>ms` on every `_dlog` line.
4. **Smaller Gemini audio chunks** — `sendPcm` splits each 1 s window
   into `GEMINI_FRAME_MS = 100` ms sub-frames (3200 bytes) sent through
   the bounded queue. Measured: 40/100/200 ms are equivalent
   (~2.75 s first-interim); 100 ms ships. HTTP fallback stays at 2 s
   chunks (`feedFallback`, unchanged).
5. **Controlled HTTP fallback** — `sendPcm` no longer runs HTTP in
   parallel from the first window: `httpParallel` starts only if no
   valid Gemini transcript arrives within `HTTP_FALLBACK_DELAY_MS =
   4000` ms of the first audio sent, and `onWsMessage` clears it the
   moment Gemini proves itself (`geminiProven = true`). Dedupe by
   audio time is unchanged (postCaption stale check).
6. **PTS-based display** — `decodeLoop` tags each 1 s window with
   `MediaCodec` `presentationTimeUs` (`windowPts`; sequential
   `windowT0` only as fallback; reset on seek). `sendPcm` now sends a
   fresh `{"op":"mark","t0":<window t0>}` before EVERY window's
   sub-frames — this fixes the stale-t0 bug found during the build:
   the old code sent the mark only at connect, so the proxy tagged
   every caption with the connect-time position and the v3.15 drop
   rule dropped Gemini captions once the video advanced ~3 s past
   connect. `postCaption` holds a caption arriving before its media
   time (`PendingCaption`, released by `maybeReleasePending` from
   `onPosition` every ~500 ms, re-checking staleness at release) and
   drops only if its END is more than `MAX_CAPTION_LATENESS_S = 2.5` s
   behind the clock (was 2.0 s). Pause/seek clears the held caption
   (`clearPending` in `onPosition`/`stop()`/`start()`). Sync is to
   `video.currentTime`, never wall clock.
7. **Lookahead — SHIPPED, verified** — `LOOKAHEAD_S = 3.0`: `sendLoop`
   releases a window when `videoPos >= w.t0 - 3.0 - 0.05`. Live test:
   Gemini accepts 2×/4× audio and returns complete transcripts;
   first-text drops 2.74 s → ~1.5 s. Captions are held until their
   media time (item 6), so nothing shows early. Pause stops sending
   (existing check); seek flushes the queue (existing check).

### Backpressure (round-2 #3)
- `wsSendQueue` (ArrayDeque, cap `WS_SEND_QUEUE_CAP = 64`) + dedicated
  `wsSenderThread` (`startWsSender`/`wsSenderLoop`, new): the sendLoop
  never blocks on the socket; items are written in order; a null/dead
  WS waits up to 200 ms (reconnect gap) then the item is dropped and
  counted. `WS_BACKPRESSURE` log (queued + dropped, at most every 2 s).

### GoAway + session expiry (round-2 #2)
- Proxy 3.15 forwards GoAway; app `onWsMessage` handles `goaway`
  (live run: `noteGeminiFail()` → reconnect + HTTP cover); `onWarmMessage`
  closes a GoAway'd warm session. `isWarmValid` requires ready +
  age < `WARM_MAX_AGE_MS = 540_000` (9 min) + proxy ping within
  `WARM_PING_FRESH_MS = 15_000` ms; otherwise the tap falls back to a
  fresh connect. `SESSION_READY` means genuinely valid, not "socket open".

### Moov/faststart (round-2 #4)
- Verified, no action: the mp4 IS faststart (moov at byte 32, v3.9/v3.10).
  `DECODE_FIRST_PCM` log + the proxy's per-Range `VIDEO` log lines
  prove t5 is not the bottleneck.

## Evaluated, NOT built
- Claude's claim that MediaExtractor downloads the whole video is FALSE
  for this engine: it fetches ONLY the audio-track byte ranges via
  Range requests (~3% of the file, verified v3.9/v3.10). No server-side
  ffmpeg audio extraction.
- Vosk + ML Kit on-device and Whisper/NLLB server models: v2
  architecture, not this fix.
- Railway cold start / sleep is NOT the cause: plain Web Service
  (Dockerfile, `python proxy/app.py`), always-on during the trial, plus
  the 6-min keep-alive cron and the proxy's own 300 s self-warmup loop
  (proxy 3.14).

## What is still phone-only to test (user, VPN off)
Install over v3.16 → site loads immediately; video plays past 0:06 with
sound; tap «زیرنویس» on a video where someone actually talks → the
button should step through the stages («…در حال اتصال به سرور» →
«…در حال اتصال به جمینای» → «■ زیرنویس روشن») and the first caption
should appear fast (pre-warmed session) and track speech word-by-word;
music-only video → «در این ویدیو حرفی نیست». If taps still fail, the
evidence needed is logcat (`XF_TIME` lines, `WV_CONSOLE` VIDEO_EVENT
lines, `XF_WS` lines) and the «تست سرعت» Toast text.
