# NOTES v3.9 — 2026-11-22

## Problems (user, live-tested v3.8 on phone, VPN off, mobile data + modem Wi-Fi)
1. Audio CUTS OFF when subtitles are turned on.
2. NO subtitle text at all — translation never arrives.
3. Caption line sits too low (on the site's bottom bar / gesture area).
4. Video still slow: thumbnails fast on both networks, but video
   starts very late, plays slowly, stutters/cuts.

## Root causes — proved live from this VM (not guessed)
### (a)+(b) ONE root cause: the capture hijacked playback audio
The old JS hooked the `<video>` element with
`AudioContext.createMediaElementSource(video)`. That REROUTES the
element's audio output into the AudioContext graph: the element
goes silent and is only audible through the graph. In WebView the
graph is fragile (AudioWorklet blob-URL module load, deprecated
ScriptProcessor, suspended context) — when any link breaks, the
user hears NOTHING, and the capture itself receives silence, so
every window was skipped as silent (RMS < 120) and no STT ever
ran. Both symptoms, one cause.
FIX (v3.9): audio for subtitles is decoded from the VIDEO FILE
itself — `SubtitleEngine` (MediaExtractor + MediaCodec on the
mp4's audio track, read through the proxy with Range requests),
time-synced to the video element's clock. The `<video>` element,
the WebView audio path and the playback volume are NEVER touched.
No AudioContext anywhere in the app anymore (verified: dex has no
`createMediaElementSource` / `AudioContext`). No AudioPlaybackCapture
either: it would need a RECORD_AUDIO permission the user never
approved, captures the whole device mix, and may return silence
when WebView audio is marked non-capturable — the file-decode path
needs no permission, no policy, and no playback interference.

### (b) Server paths — measured live 2026-11-22 (9 s real English speech)
- HTTP `POST /dub/stt`: WORKS — "hello this is a short test",
  conf 0.97, ~0.5 s.
- WS `/dub/ws`: BROKEN live — every 1.0 s window returned empty
  STT (stt-done "" in ~0.2 s) while the same audio works via HTTP
  and the same code works against a local server. Concurrent HTTP
  STT works live too, so it is not rate-limiting. The WS debug
  channel could not tell an empty transcript from a thrown
  exception (google_stt returns None on error, debug printed "").
  v3.9 reports the exact worker error in the debug channel, and
  WS finals now also try a server-side translate (Persian sent
  straight away when the server can translate).
- WS `/dub/gemini-ws`: WORKS live at realtime speed — 8 Persian
  interim captions over 18 s of speech, first at ~5.7 s. The
  user's AQ.-prefixed key is VALID; it is now also sent as the
  documented `x-goog-api-key` header (in addition to ?key=).
  New `GET /dub/gemini-test`: a real REST generateContent call
  returning the raw HTTP status + error (never the key) — because
  "configured: true" proves nothing. The Gemini reader no longer
  kills the session after 30 s of silence (now 90 s, like /dub/ws).

### (3) Caption position
In-page box was `bottom - max(56, height*0.14)`; now
`max(110, height*0.24)`. Native fullscreen overlay bottomMargin
was 90 dp; now 140 dp.

### (4) Video — the physics
Real file (verified): 1080x1848, 174 MB, 532 s, video 2.48 Mbps
+ audio 129 kbps ≈ 2.6 Mbps. The user's line is 2.8 Mbps — the
video needs ~93% of the line, so smooth playback is marginal
whatever the path. The site serves ONE rendition: the JS bundle
has a quality-chooser flag but no rendition list; only `full.mp4`
exists (`converted.mp4` is 404). No lighter file to prefer, and
on-demand ffmpeg transcoding is not viable on Render Free.
What v3.9 DOES fix (app-added stalls, proved in code):
- v3.8 BLOCKED the player's first request up to 2 s in the
  intercept while racing 256 KB on the same slow line, and
  started 800 ms late. v3.9 NEVER blocks the first request;
  the race runs in the background.
- Race legs are now 64 KB (path decision in ~0.3 s, minimal
  bandwidth stolen from playback); the winner's first 512 KB is
  prefetched in the background from the winner only.
- `servePrefetched` now serves ANY Range fully inside the
  prefetched window (v3.8 only served ranges starting at 0).

## What changed
- `proxy/dub.py` (dub 1.8): WS worker error diagnostics,
  server-side translate on WS finals, Gemini `x-goog-api-key`
  header, `/dub/gemini-test`, Gemini reader 90 s idle.
- `proxy/app.py`: PROXY_VERSION 3.9.
- `app/.../SubtitleEngine.java` (NEW): file-audio decode +
  Java WS client (Gemini -> /dub/ws -> HTTP /dub/stt) +
  on-phone translate chain (gtx -> MyMemory -> proxy).
- `app/.../DubBridge.java`: button + caption overlay only;
  all AudioContext/WS/translate JS removed.
- `app/.../WebActivity.java`: engine wiring (XfSub onStart /
  onStop / onPos), race fixes above, native caption 140 dp.
- `version.json` / `build.sh`: 30 / 3.9.

## Verification (live, 2026-11-22)
- APK v3.9 (code 30, 144,605 bytes): apksigner v2+v3 OK,
  badging 30 / 3.9, same cert SHA-256 `f5ace241…dc4dc1`.
  Dex contains `SubtitleEngine`, `MediaExtractor`;
  `createMediaElementSource` / `AudioContext` absent.
- Proxy pushed (commit TBD), live `x-proxy-version: 3.9` TBD.
- Live subtitle paths re-tested (see "Root causes" above).
- Honest limits: only the user's phone test can prove the
  Iran paths, the file-decode capture, and whether 512 KB
  prefetch + no-block race is enough on a 2.8 Mbps line for a
  2.6 Mbps video. Subtitle translations from Gemini Live are
  rough on fast/robotic speech (e.g. "test" -> "مسیر").

## Still needs the user's own test (VPN off, mobile data AND Wi-Fi)
1. Open a video — it should start faster, with less stutter.
2. Tap «زیرنویس» on a video where someone actually talks:
   video sound must KEEP PLAYING normally, Persian captions
   appear at the bottom (clearly above the bottom bar) within
   ~3-6 s. Tap again to stop.
3. If a caption never appears, note the button text (it now
   says WHY, e.g. "ویدیو صدا ندارد" / "صدا قابل گرفتن نیست").
