# XFreeFa v3.2 — Live Persian Dubbing: wiring

Status 2026-11-22: **verified live end-to-end from this VM** (see
"Measured" below). Two new files only; `proxy/app.py` and
`WebActivity.java` were NOT edited — wire them with the snippets below.

## New files

- `proxy/dub.py` — stdlib-only dub pipeline (`/dub/health`,
  `/dub/segment`). No key of any kind is embedded or needed.
- `app/src/main/java/com/xfree/fa/DubBridge.java` — `DubBridge.inject(webView)`
  adds the floating «دوبله فارسی» button + capture/sync JS into the page.

## 1. Wire the proxy — `proxy/app.py`

At the top, next to the other imports:

```python
import dub as dub_mod  # live Persian dubbing (stdlib only, no keys)
```

In `Handler.handle_all`, **before** the reverse-proxy fallback
(right after the `/health` check at the top of the method):

```python
        if parsed.path.startswith("/dub/"):
            if dub_mod.handle_dub(self):
                return
```

In `Handler.do_POST`, **before** the `/g/` relay branch:

```python
        if parsed.path.startswith("/dub/"):
            if dub_mod.handle_dub(self):
                return
```

That is the whole proxy wiring: 2 imports/lines + 2 three-line guards.
`dub.py` sits next to `app.py`, so `import dub` resolves. Render
needs nothing installed (stdlib only). Bump `PROXY_VERSION` to `"3.2"`
when deploying.

## 2. Wire the app — `WebActivity.java`

In `onPageFinished` (inside the `WebViewClient`), after
`CookieManager.getInstance().flush();` add:

```java
                DubBridge.inject(view);
```

No import needed (`DubBridge` is in the same package `com.xfree.fa`).
No layout change, no new permission, no player change. The JS
re-scans for the `<video>` every second, so SPA navigation inside the
site is covered without further calls.

## Endpoints

| Route | Method | Body / reply |
|---|---|---|
| `/dub/health` | GET | JSON: per-service live status (`stt_google_v2`, `translate_gtx`, `tts_edge_fa`) + `ok` |
| `/dub/segment?t0=<sec>` | POST | Raw s16le 16 kHz mono PCM (`Content-Type: audio/l16`), or JSON `{"pcm_b64": ..., "t0": ...}`. Reply JSON: `t0`, `silent`, `source_text`, `source_lang`, `confidence`, `fa_text`, `audio_b64` (MP3, base64), `audio_dur`, `tts_engine`, `error`, `ms`, `timings` (`stt_ms` / `translate_ms` / `tts_ms`) |

## How it works

```
site <video> --(AudioWorklet, 4 s PCM chunks)--> POST /dub/segment
  -> silence check (RMS) -> Google Web Speech v2 STT (en-US, fa-IR fallback)
  -> Google Translate free (gtx) en->fa -> Edge neural TTS (fa-IR-DilaraNeural)
  -> JSON {fa_text, MP3 base64} -> JS schedules MP3 at the chunk's video
     timestamp, original volume ducked to 0.15 while dubbing
```

## Measured (live, 2026-11-22, this VM)

- `/dub/health`: all three services `ok` — STT 663 ms, translate 616 ms
  («Hello» → «سلام»), Edge TTS 1,975 ms (11,232-byte Persian MP3, 1.5 s).
- `/dub/segment` on a real 2.5 s English speech PCM clip
  («hello world this is a test of speech»): STT 941 ms, conf 0.988 →
  translate 1,983 ms → «سلام دنیا این یک تست گفتار است» → Edge TTS
  1,512 ms → 19 KB MP3, 3.18 s audio. **Total server time 4,441 ms.**
- Edge TTS token port verified byte-identical to the reference edge-tts
  library for 3 fixed timestamps.
- **First-dub delay: ~4.4 s server pipeline + 4 s capture window ≈ 8–9 s
  from tapping «دوبله فارسی» to the first Persian audio; then a new
  segment every 4 s, dub trailing live speech by ~5–8 s.** Never 0 s.

## Verified dead ends (do not retry without new evidence)

- Google Translate TTS `tl=fa` / `tl=fa-IR`: HTTP 400 for every text
  (6 attempts); `tl=en` and `tl=ar` return real MP3s. Persian voice
  is Edge, not Google TTS.
- Google Translate gtx: intermittently HTTP 429 under shared-IP load;
  retried (3x, backoff) and reported by `/dub/health`.
- StreamElements TTS: HTTP 401 (now requires auth).
- Lingva Translate: HTTP 503 «provider temporarily unavailable»;
  Lingva audio for fa returns Google's 400 page.
- MyMemory: HTTP 429, daily free quota exhausted on the shared IP.
- Google Web Speech v2 **without** a key: HTTP 400 «Missing parameter:
  key»; v1 endpoint is 404-dead. The key used is the **public Chromium
  demo key** shipped in open-source Chromium (same one this workspace's
  H.mbot already used) — not a private key, not the user's key.
- No Groq key exists in this environment; none is used or needed.

## Honest limits

- Dub lags speech by one segment (~5–8 s); first audio ~8–9 s after tap.
- Web Speech is a short-phrase recogniser: weak on fast, long or
  music-covered speech; empty segments are skipped, never invented.
- Adult-video speech is sparse: most 4 s segments are silent and are
  skipped client-side (RMS check) without any request.
- Edge TTS is unofficial: Microsoft can change/block it (token algorithm
  is version-pinned `2-143.0.3650.75`). If it breaks, `/dub/segment`
  returns Persian text with `audio_b64: null` and the app falls back to
  on-device `speechSynthesis` when available.
- In the site's native fullscreen (custom view) the page button is not
  visible; dubbing keeps playing if already on, toggled only outside
  fullscreen.
- The button never shows a fake «dubbing» state: capture failure shows
  «دوبله در دسترس نیست», and a failed segment just skips silently.

## Still needs the user's own test (VPN off)

1. Deploy proxy with the wiring above, install the v3.2 APK.
2. Open any video, tap «▶ دوبله فارسی» (floating purple button,
   bottom-right), wait ~9 s: Persian voice should start with the video
   quiet underneath; tap again to turn off.
3. Check `/dub/health` on the Render URL if dubbing ever stays silent.
