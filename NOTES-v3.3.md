# v3.3 — dubbing fixed, separate top buttons, live subtitles, faster open

## What the user reported (2026-11-22 08:22, v3.2)
- Site loads very late / speed low.
- Dubbing doesn't work. Screenshot: purple pill "دوبله در دسترس نیست".

## 1. Dubbing — root causes found (both fixed)
(a) Translate dead on live Render: /dub/health showed translate_gtx FAIL
(2x 10 s timeouts) while STT/TTS were OK. Render's shared egress IP is
blocked/timed-out on translate.googleapis.com gtx. Fix: translate chain
in proxy/dub.py (v1.1) — gtx on translate.googleapis.com, then gtx on
translate.google.com, then MyMemory (no key); a failed host is skipped
for 60 s; /dub/health probes each candidate and reports which works.
Gemini ruled out: the key lives in Muse's secure vault and cannot be
put on Render.
(b) App bug: after turning dubbing off once, hookVideo() returned false
for the already-hooked video element (video.__xfDubHooked) and the
button said "دوبله در دسترس نیست" forever. Fix: already-hooked returns
true (graph reused). Failure messages now say WHY briefly
("دوبله: صدا قابل گرفتن نیست", "دوبله: ترجمه در دسترس نیست").

## 2. Buttons + subtitles (user's asks)
- No more floating button over the video. Two small buttons together
  at the TOP of the video page (top-right, under the header, next to
  the profile column): "▶ دوبله" and "زیرنویس". Fully independent
  toggles: subtitles on without dubbing, dubbing on without subtitles.
- Live subtitles: same STT→translate pipeline; the Persian text of
  each segment appears as an overlay at the bottom of the video as
  soon as its translation is ready. Subtitles-only calls
  /dub/segment?tts=0 (skips TTS, ~2 s sooner: measured 1.9 s vs 5.3 s
  locally). Honest limit: text lags speech ~3-5 s by design.

## 3. Speed
- Keep-alive cron tightened 12 min -> 6 min (runs were exactly on
  schedule; tighter interval removes any >15 min gap risk that would
  let Render sleep).
- Google Translate Element script now loads async (was a blocking
  script tag at the end of every page).
- Measured warm from this VM: homepage TTFB 0.47-1.55 s, total
  0.69-1.83 s; element.js via relay 1.09 s. Cold wake after 15+ min
  idle is still ~30-60 s on Render Free (only a paid plan removes it;
  never approved).

## Verify (live)
- /dub/health: translate ok via <candidate> (see live result).
- Real /dub/segment on live proxy with the verified English clip:
  STT -> fa text -> Edge MP3, <time>.
