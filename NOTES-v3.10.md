# XFreeFa v3.10 — video stall + subtitles, fixed from the root

**App:** versionCode 31 / 3.10 — `~/workspace/your_files/xfree-fa-3.10.apk`
(144,605 bytes, apksigner v2+v3, same cert `f5ace241…dc4dc1`, installs over v3.9)
**Proxy:** `x-proxy-version: 3.10`, dub 1.9 — commit (this push)

User-tested v3.9 (2026-11-22 19:12): the video page opens, but playback is
stuck at 0:06 with the pill «در حال اتصال...», and the subtitle pill says
«زیرنویس: صدا قابل گرفتن نیست». Two separate root causes, both proved.

---

## 1. Subtitles: "audio cannot be captured" — double-wrapped video URL

**Root cause (proved live).** The player loads through the proxy, so the
video src handed to `SubtitleEngine` is already
`https://xfree-fa-proxy.onrender.com/video?u=<encoded cdn url>`.
The old `start()` checked `contains("cdn.xfree.com")` FIRST — the
percent-encoded `?u=` value still contains that literal text — and wrapped
the proxy URL a SECOND time with `Proxy.videoUrl()`. The proxy's
`ALLOWED_HOSTS` does not include its own host, so `handle_binary` answered
**400 "bad or disallowed url"** (reproduced live with curl, 0.35 s).
`MediaExtractor.setDataSource` threw → the catch posted
«زیرنویس: صدا قابل گرفتن نیست».

The mp4 itself is FINE — this was never a missing audio track:
ffprobe (post 1489587, 185,967,225 bytes, 348.7 s) shows h264 1080×1920
60 fps (4.13 Mbps) + AAC LC 44.1 kHz stereo (129 kbps); moov box at byte
32 (size 507,920), ftyp at 0 — faststart, moov at the START. ffmpeg
decoded the first 5 s of audio to verified non-zero PCM
(16,335/16,347 non-zero samples, RMS 1659.3).

**Fix (belt and suspenders).**
- `SubtitleEngine.start()`: already-proxied URL (`PROXY_BASE + "/video?u="`)
  is used as-is FIRST; only a genuinely direct CDN URL is wrapped.
- `SubtitleEngine.decodeLoop()`: if `setDataSource` on the proxy URL throws,
  it retries with the direct CDN URL extracted from the `?u=` param.
- `proxy/app.py` `handle_binary()`: unwraps a double-wrapped relay URL ONCE
  (own host = `PROXY_BASE` hostname or the request's Host header), then the
  normal `allowed()` check runs on the inner URL. Triple-wrap still 400s,
  disallowed hosts still 400 (both verified locally).
- `enqueueWindow()`: all-zero PCM windows are skipped on the phone before
  they reach any STT/Gemini path (ChatGPT priority 2: verify PCM non-zero
  before connecting Gemini).

**Audio-capture decision (evidence-based).** The v3.9 file-decode path
(MediaExtractor + MediaCodec on the mp4's audio track) is KEPT — the only
proven failure was the URL bug above. It needs no RECORD_AUDIO permission,
never touches playback audio, and has no capture-policy problem.
AudioContext dual-connect (source → destination + source → worklet) would
reintroduce the fragile graph that cut audio in v3.8; AudioPlaybackCapture
needs a RECORD_AUDIO permission the user never approved and may return
silence for non-capturable WebView audio.

## 2. Playback stuck at 0:06 — race / prefetch / 302 redirect removed

**What v3.9 added (reverted in v3.10, per the "if it cannot be proven
safe, revert it" rule):** per-video 64 KB race legs, 512 KB prefetch,
`servePrefetched` serving any Range inside the window, and a 302
`redirectTo` when "direct" won. Prime suspects for the stall: the 302 from
`shouldInterceptRequest` may not be followed by WebView; 64 KB legs measure
latency, not throughput, so "direct" can win spuriously on the phone; the
race + prefetch steal ~640 KB ≈ 2 s of line time at the exact moment
playback starts. None of it is phone-provable from the VM, so ALL of it is
gone: `WebActivity` has no race, no prefetch, no redirect — the video
streams proxy-only (`/video?u=` intercept returns null; WebView fetches
from the proxy origin natively). Verified in the built dex: `runVideoRace`,
`servePrefetched`, `redirectTo`, `ensureRaceFor`, `maybeRaceVideo` all
absent; `SubtitleEngine` present; no `createMediaElementSource` anywhere.

**Range proxy — ChatGPT test A, passed live AND locally.** bytes=0-65535
then 65536-131071 → both 206 with correct Content-Range and different
bytes; bytes=0-1048575 then 1048576-2097151 → both 206. The proxy streams
in 64 KB chunks (`VIDEO_CHUNK`, video path only) and never buffers a whole
file (`stream_response_pooled` → `pool_stream`, 2 MB upstream reads written
out in 64 KB writes). moov is at the start (see §1), so mp4 structure is
not the stall cause.

**Bitrate correction (supersedes v3.9 notes).** The real file averages
**~4.27 Mbps** (185,967,225 × 8 / 348.7 s; ffprobe video 4.13 Mbps + audio
129 kbps), NOT 2.6 Mbps. On the old 2.8 Mbps line it needed ~152% of the
line — no app fix could play it smoothly. On the raised 5.3 Mbps line it
needs ~81% — playable with the proxy-only path. HLS/360p was NOT built:
on-demand ffmpeg transcoding is not viable on Render Free (no ffmpeg
binary, 512 MB RAM, stdlib-only Python proxy).

## 3. Gemini relay + transports

- **Gemini Live works live, but cold start is ~20 s.** 60 s realtime test:
  first Persian interim caption («فقط») at 21.5 s. The old 10 s
  stale-transport guard killed a healthy Gemini session before it ever
  spoke. **Gemini grace is now 30 s**; after 2 dead sessions in a row it
  is abandoned for that video.
- **Dual-send until Gemini proves itself:** every chunk goes to HTTP
  `POST /dub/stt` in parallel until the first Gemini transcript arrives
  (HTTP: 387 ms per chunk live, first caption ~4 s), then Gemini-only.
  If Gemini is dead, HTTP-only.
- **`/dub/ws` REMOVED from the engine's transport chain** (mode 2 gone):
  proven broken live — every window returns empty STT in ~0.2 s while
  HTTP STT on the same server returns text. Same code works against a
  local server; the live cause is still unproven. (Local WS test after
  the fix: STT returns "hello this is" / "short", Persian finals
  «سلام این است» / «کوتاه» — the WS code path itself is healthy.)
- **`_gemini_parse_out` now returns `(text, done, lang)`:** primary
  `serverContent.outputTranscription` (lang "fa"); fallback
  `serverContent.inputTranscription` (lang "en") so the phone translates
  on-phone instead of showing nothing.
- **New `GET /dub/gemini-diag`:** attempts the Gemini Live WS handshake +
  setup (≤12 s), returns `{"ok", "stage", "error"}` (stages:
  `not_configured`, `handshake_failed`, `gemini_error`, `setup_timeout`,
  `setup_complete`) — never the key, never response bodies.
- **Instrumentation (ChatGPT priority 3):** `_STATS` counters —
  `ws_audio_packets`, `ws_audio_bytes`, `ws_stt_calls`, `ws_stt_empty`,
  `http_stt_calls`, `gemini_sessions`, `gemini_messages`,
  `gemini_transcripts`, `gemini_input_transcripts`, `gemini_errors`,
  `subtitles_sent` — exposed via `/dub/health` → `"stats"`.
- **Bug found while verifying the counters:** `dub_health()` cached the
  WHOLE response (including the stats snapshot) for 60 s, so a working WS
  path looked dead (counters frozen at 0 while captions flowed). Fixed:
  probes stay cached 60 s, but `stats` is re-read live on every call.
  (Local proof after the fix: 2 binary frames → `ws_audio_packets: 2`,
  `ws_audio_bytes: 64000`, `ws_stt_calls: 6`, `subtitles_sent: 2` within
  3 s of sending.)
- **Note:** server-side translate worked from this VM during the v3.10
  tests (Persian finals «سلام این است», «کوتاه» via gtx/MyMemory) — the
  Render-IP 429 is IP-specific, not universal. The app's translate chain
  (phone gtx → MyMemory → proxy) is unchanged.

## 4. NEW — WebSocket continuation frames: the relay was losing 7/8 of the audio

**Found during live verification of this build (2026-11-22 ~16:10).**
A middlebox on the live path (Render/Cloudflare edge) splits any large
WS frame into 4096-byte CONTINUATION frames (opcode 0x0). The relay's
`_ws_recv_frame` returned only the first frame and silently dropped every
continuation. **Proved live:** one 32,000-byte binary frame raised
`ws_audio_bytes` by exactly **4,096** (a 1,000-byte frame by 1,000); over
a 3-pass streaming test, 30 frames / 868,608 bytes sent arrived as 30
packets / **115,200** bytes — 13% of the audio. Locally (loopback, no
middlebox) frames are never split, which is why every local test passed
and live subtitles starved: Gemini only ever heard the first 0.128 s of
each 1 s audio chunk. This is very likely the real reason live subtitles
never worked on the phone, even though the relay "worked" in probes.

**Fix (proxy-only, no APK rebuild needed):** `_ws_recv_frame` now
reassembles fragmented messages (first frame + continuation frames until
FIN) for both WS handlers and the Gemini reader. The dead duplicate
`_ws_recv_frame` (old version that discarded the mask bytes) is deleted.
**Local proof with manually fragmented frames:** 64 KB sent as 4096-byte
fragments → reassembled into 1 packet / 64,000 bytes, STT finals
"hello this is" / "short", Persian captions «سلام این است» / «کوتاه».
**Live re-verification (2026-11-22 ~16:15, commit `91525cfb`):**
one 32,000-byte frame → **+32,000 bytes** (was +4,096); full streaming
test (9 s clip × 3): **40 captions, first Persian «سلام، این یک» at
+1.8 s** (was 4 captions, first at +6.9 s, only «فقط»), full accurate
transcription ("Hello, this is a short test of the subtitle system, the
quick brown fox jumps over the lazy dog…") with good Persian
(«سلام، این یک تست کوتاه از سیستم زیرنویس است، روباه قهوه‌ای سریع از
روی سگ تنبل می‌پرد…»); live stats `ws_audio_bytes: 900,608`
(= 868,608 sent + 32,000 controlled test — ALL bytes arrive),
`gemini_transcripts: 20`, `subtitles_sent: 40`. The relay was starving;
now it hears everything.

Local proxy (port 10099, same code as pushed):
- Range 0-65535 / 65536-131071 / 1048576-2097151 → all 206, correct
  Content-Range, different bytes.
- Double-wrapped `/video?u=` → 206 (was 400); triple-wrap → 400;
  disallowed host → 400.
- `POST /dub/stt` → 200, "hello this is a short test", conf 0.968.
- `/dub/health` → dub 1.9, stats live (http_stt_calls increments at once).
- `/dub/gemini-diag` → `not_configured` locally (no key in this shell —
  expected; live Render has the key).
- `_gemini_parse_out`: out-only → `('سلام', True, 'fa')`; in-only →
  `('hello test', False, 'en')`; none → `(None, False, 'en')`.

Live Render (after push): see final report — `x-proxy-version: 3.10`,
Range test A, double-wrap 206, `/dub/gemini-test` 200,
`POST /dub/stt` 200, `/dub/gemini-ws` Persian caption (allow ~30 s),
`/dub/gemini-diag` `setup_complete`, `/dub/health` stats.

## What the user must test (phone, VPN off)

1. Open a video — does it PLAY past 0:06 (not stuck on «در حال اتصال...»)?
   Is the sound on the whole time?
2. Tap «زیرنویس» on a video where someone actually talks — do Persian
   captions appear at the bottom of the video? First caption via HTTP in
   ~4 s; Gemini captions take ~20 s to start. (Most xfree clips have no
   recognisable speech — music/moans only — and subtitles correctly stay
   silent there.)
3. If a caption never appears, the button now reports the real reason on
   screen (via `DubBridge` status) instead of a generic failure.

## Honest limits

- Only the phone proves the Iran paths and the MediaExtractor decode;
  the VM cannot run the APK.
- Real file ~4.27 Mbps vs the 5.3 Mbps line — tight but playable; on the
  old 2.8 Mbps line it could not play smoothly by physics.
- Gemini first caption ~20 s cold start from Render; HTTP covers the gap.
- HLS/360p not feasible on Render Free.
- `/dub/ws` live failure cause still unproven (works locally, empty STT
  live); it is no longer in the chain.
