# NOTES — v3.16 (versionCode 37, 2026-11-24 ~01:27)

Built from the v3.15 phone test (user, 2026-11-24 04:47, screenshot
`1000300743_1_44hn.png`): the icon is STILL a black rounded square
with the label `com.xfree.fa` under it; first launch is slow;
subtitles arrive late with a big delay. Three fixes.

## 1. Launcher icon — FULL-BLEED, no background, no name

What the user saw in v3.15: purple 3D X on a BLACK rounded square,
label `com.xfree.fa`. Two separate causes:

(a) BACKGROUND: v3.15's adaptive icon had a transparent background
and the X padded to ~61% (the "safe zone"). MIUI/HyperOS fills any
transparent area inside the icon mask — white in v3.14 (legacy-only
icon, framework default) and black in v3.15 (explicit transparent
background). Transparent margin = launcher fill, always. FIX:
the foreground is now FULL-BLEED — the approved 3D logo
(`logo/xfree-fa-logo-3d.png`, NOT redesigned) scaled ×1.4298 about
its centre (cover-fit) so the X's arms reach every edge of the
108dp foreground canvas; the squircle mask crops only the X tips.
Preview rendered (`/tmp/icon-preview-squircle.png`, checked
visually): a big glossy purple X filling the tile, reads clearly.
The gaps between the X arms are still transparent pixels, so the
launcher's fill shows there — but with the arms at the edges the
tile reads as the logo itself (the standard X-app look), with no
visible margin ring. Residual risk, stated honestly: if MIUI still
paints a visible square edge, that is the launcher's own tile
behaviour, not the PNG — no APK-side pixel can remove it, short of
an opaque backdrop the user explicitly forbade.
Applied to `ic_launcher_foreground.png` AND the legacy
`ic_launcher.png` / `ic_launcher_round.png` at all 5 densities
(mdpi 108 … xxxhdpi 432). Adaptive XML unchanged (transparent
background + foreground ref).

(b) LABEL: v3.15's U+200B zero-width space FAILED — MIUI treated it
as empty and fell back to the package name `com.xfree.fa`
(visible in the screenshot). FIX: `launcher_label` is now U+2800
BRAILLE PATTERN BLANK "⠀" (`app/src/main/res/values/strings.xml`):
a non-empty, NON-whitespace character (General Category So), so no
launcher trims it away or treats the label as empty, and it renders
as a blank cell. Verified in badging: `application-label:'⠀'` and
`launchable-activity … label='⠀'` — byte check `E2 A0 80`.
Residual risk: if MIUI still falls back to the package name, the
remaining fallback is the launcher's own setting, not the APK.

## 2. First launch — "slow at first", fast once running

Diagnosis (measured, not guessed):
- The task's prime suspect was WRONG: v3.14's one-time cache clear
  (`cache_cleared_for != 35`) does NOT fire on v3.15 — the flag was
  already 35 from v3.14. It only fires on a FRESH install/reinstall
  (flag 0), where it wipes the just-built WebView cache and forces a
  full re-download of every JS bundle on the very first launch.
  Removed entirely in v3.16 (`WebActivity.setupWebView`) — dead
  code after v3.14, and a trap on any fresh install.
- The load flow is otherwise clean: `loadSite()` is called
  directly from `initUi()` — no wake-gate, no /health wait, no
  serial retry before first paint (verified in code).
- What a cold first load actually costs (measured from the VM):
  homepage 475,160 B + 17 `_nuxt/*.js` bundles totalling ~1.5 MB
  = ~2 MB the phone must pull over a 5.3 Mbps line (~3 s of pure
  transfer) + WebView cold init on the phone (1-3 s, not measurable
  from the VM). Proxy-side, a COLD cache serves the homepage in
  1.76 s and each bundle in 1.1-2.1 s; WARM, the same requests take
  0.72 s / 0.4-0.8 s (2-3x faster — repeat fetches verified).
  The catch: the cache only fills when a real client asks. The
  homepage HTML is cached just 120 s (the 6-min keep-alive ping
  cannot keep it warm) and the bundles (3600 s TTL) are never
  fetched by the ping at all — so the first load after any idle
  stretch pays upstream latency for every asset.
- FIX (proxy 3.14, `proxy/app.py` `_warmup_loop`, started as a
  daemon thread in `__main__`): every 300 s the proxy re-fetches
  `/` (mobile UA, so the "m" cache class is warmed) and every
  `/_nuxt/*.js` it references, through the normal handler path so
  the response cache fills. ~2 MB per cycle, ingress from
  xfree.com is free on Railway. All errors swallowed; the loop
  always sleeps; never blocks serving. Verified locally: the
  thread's first `GET /` appears in the access log ~5 s after
  start; `/health` 200 with `X-Proxy-Version: 3.14`. (The local
  homepage fetch returns 502 — the VM's IP is Cloudflare-blocked
  by xfree.com directly; on Railway the same path serves 200, as
  measured live before this change.)
- Honest floor: WebView cold init + ~2 MB over 5.3 Mbps cannot go
  below ~3-4 s on the user's line; this change removes the
  extra 1-2 s of upstream latency on top of it.

## 3. Subtitles — word-by-word, real-time

What the user asked: "each word should be subtitled at the moment
it is spoken". What was already true (verified in code, NOT rebuilt):
- The proxy Gemini relay ALREADY forwards interim partials:
  `_gemini_parse_out` (`proxy/dub.py`) returns `done=False` for any
  message without `turnComplete`, and `_gemini_reader` sends
  `{"type":"interim",…}` immediately for those.
- The app ALREADY renders interims live: `onWsMessage`
  (`SubtitleEngine.java`) accepts `interim`/`final` and calls
  `postCaption(text, interim, …)`; the caption view shows interim
  text at 0.65 alpha/italic and updates in place.
- So NO proxy change was needed for interim streaming.
- The real blocker is unchanged: `GEMINI_API_KEY` is STILL not on
  Railway (`/dub/gemini-status` → `configured:false`, verified
  live). With the key missing, every subtitle run pays a doomed
  Gemini WS attempt first: a TLS handshake + upgrade (~0.5-1.5 s
  on the user's line) that fast-fails at the proxy, and the first
  1 s window's parallel HTTP post is skipped because the failure
  flips the engine into fallback mode before it runs.
- FIX (app only): `SubtitleEngine.checkGeminiConfigured()` —
  background `GET /dub/gemini-status` (2 s timeouts), result
  cached 60 s in `geminiConfigured` (null = unknown → current
  behaviour). Called from `prewarm()` (video page open, so the
  answer is usually cached long before the tap) and from
  `start()` as a fallback. `sendPcm()` applies it: if the cached
  result is FALSE, it enters HTTP fallback immediately and feeds
  the window into the first fallback chunk (no audio lost).
  The background WS reconnect keeps retrying, so once the user
  enters the key on Railway, Gemini activates without re-tapping.
- KEPT: manual-only start, 2 s HTTP fallback chunks, v3.15 speech
  gate (no captions for speechless videos), stale-caption drop,
  original audio untouched, no dubbing, no wake screen.
- HONEST LIMIT: even the Gemini path trails speech by ~1-2 s —
  translation needs the words first, so true zero-delay is
  impossible; the goal is live streaming partials, not batched
  2 s captions. AND until the USER re-enters `GEMINI_API_KEY` in
  Railway Variables, the fast path stays off and subtitles stay on
  the slower HTTP fallback (~2-4 s per caption) — that step is
  his, not buildable.

## Build + verification

- APK: `xfree-fa-3.16.apk` — **1,476,438 bytes** (was 419,670 in
  v3.15; the growth is the icon PNG set — 15 PNGs, 3 per density,
  now mostly opaque purple instead of mostly transparent, so they
  barely compress), md5 `7fde189e14ad7ac301ebd54b6a67feda`,
  `com.xfree.fa`, **versionCode 37 / versionName 3.16**, same
  keystore + same cert SHA-256 `f5ace241…dc4dc1` (installs over
  v3.15).
- apksigner **v2+v3 verified**; aapt2 badging confirms package +
  versions + `application-label` / `launchable-activity` label
  exactly U+2800 (`E2 A0 80`) + adaptive icon XML at all
  densities.
- Dex: **0 `onrender`, 0 `AIza`** (no key in the APK);
  `checkGeminiConfigured` + `gemini-status` present; 14 Railway
  host refs.
- `version.json` bumped to 37 / 3.16; pushed to `hkkkkh/xfree-fa`
  main with this note (commit recorded in the local sync record).
  Proxy 3.14 live-verified after the Railway redeploy: `/health` 200,
  `X-Proxy-Version: 3.14`, `OPTIONS /video` 204, 1 MB Range 206,
  `/dub/health` dub 2.0, `/dub/gemini-status` configured:false
  (key still not re-entered — expected).

## Still phone-only to test (user does it himself — VPN off)

Install over v3.15, VPN off:
1. Launcher icon: the purple 3D X ALONE, filling the tile — no
   white/black margin ring, no name under it. (If the old icon
   persists, the launcher cached it — remove/re-add it from the
   home screen once.)
2. Cold start: tap the icon with the app killed — the site should
   appear noticeably faster than v3.15's first launch.
3. Video plays past 0:06 with sound.
4. Subtitles: tap «زیرنویس» on a talking video — first caption
   should arrive ~1 s sooner than v3.15 (HTTP path); on a
   music-only video the button shows «در این ویدیو حرفی نیست».
   Word-by-word live captions need the Gemini key re-entered in
   Railway Variables first.
