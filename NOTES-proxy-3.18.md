# Proxy 3.18 — redgifs.com as a third site (NO app install)

Date: 2026-11-28. Commit: (see push record). Railway EU West.
No APK change — the already-installed v3.17 app reaches redgifs
through the switch pills the proxy injects into served HTML.

## What the site is (verified 2026-11-28 from the VM)
- React SPA behind Cloudflare; title "Porn Gifs and Porn Pics |
  RedGIFs"; 11 KB shell, feed loads client-side.
- API base `https://api.redgifs.com/` (axios baseURL in bundle
  common-*). Endpoints seen: v2/auth/temporary, v2/feeds/modules,
  v2/tags/feed/, v2/recommend/gifs/, v2/niches/, v2/gifs/,
  v2/user_profile, v2/me/settings/age-gate (age gate exists).
- Auth token is IP-BOUND: GET v2/auth/temporary returns a JWT;
  using it from a different IP gives 401 WrongSender ("This token
  belongs to a different device") — proved from this VM, whose
  egress IPv6 rotates between requests. Routing token request +
  API calls through this one proxy keeps them on a single egress
  IP (Railway's).
- Video: direct mp4 on media.redgifs.com/<Name>.mp4 (also
  -mobile.mp4, -poster.jpg) AND an HLS builder in the bundle:
  `https://api.redgifs.com/v2/gifs/<id>/<q>.m3u8`.
- Avatars: userpic.redgifs.com. Consent script:
  cdn-cookieyes.com (NOT relayed — non-essential).

## What changed (proxy/app.py only)
- `PROXY_VERSION` 3.17 -> 3.18.
- `/red/<path>` -> https://www.redgifs.com/<path> (SPA shell +
  /static chunks); `/redapi/<path>` -> https://api.redgifs.com/
  <path> (API incl. token flow + m3u8 playlists/segments).
  Range streams via pool_stream; cookies + Authorization
  forwarded; Referer/Origin rewritten to www.redgifs.com.
  Cache: /static/ 86400s, /red/ shell 300s, API never cached.
- `_rewrite_body`: `https://api.redgifs.com` -> `/redapi`,
  `https://www.redgifs.com` -> `/red` (plain, //, `\/` and
  `\u002F` forms). media/userpic redgifs hosts were already in
  RELAY_HOSTS (-> /g/, no-Referer fix from 3.17).
- `mpegurl` added to rewriteable content types so .m3u8
  playlists served through /redapi/ have their segment URLs
  rewritten to stay on the proxy.
- `_rewrite_location`: redgifs redirects stay on the proxy.
- `_inject_switch_pill` now emits TWO pills (the other two
  sites), stacked top-left (6px / 38px): xfree -> «تیک‌پورن» +
  «ردگیفز»; tik -> «ایکس‌فری» + «ردگیفز»; red -> «ایکس‌فری» +
  «تیک‌پورن».
- Warmup loop also warms /red/ + its /static chunks.
- do_POST / PUT / DELETE / PATCH route /red* and /redapi*.

## Local verification (VM, proxy on :18099)
- /red/ 200, 12,101 B, BOTH pills («ایکس‌فری» + «تیک‌پورن»).
- /red/static/index-*.js 200.
- /redapi/v2/auth/temporary 200 + token JSON (relay works).
- /tik/ 200, pills now «ایکس‌فری» + «ردگیفز».
- xfree / -> 502 challenge FROM THIS VM ONLY (pre-existing).
- m3u8 playlist rewrite NOT verifiable from the VM (needs a
  real gif id from the API, and the token is unusable from the
  VM's rotating egress) — live check below is the real test.

## Live verification (Railway, after deploy)
- (fill in after deploy: /health + version, /red/ 200 + 2 pills,
  /redapi/v2/auth/temporary 200, token THEN used for a feed call
  (search/feeds) -> 200 with real gifs (proves single-egress
  token flow works from Railway), one gif's m3u8 via /redapi
  -> 200 with /redapi/ segment URLs, a media mp4 Range 206,
  xfree / + /tik/ 200 with 2 pills each, /dub/gemini-status
  configured:true.)

## Honest limits (phone-only)
- If Railway's egress IP also rotates per request, the token
  flow breaks with WrongSender even through the proxy — only
  the live feed test above decides it.
- The age gate may ask for confirmation on first open (cookie).
- Whether redgifs.com is filtered in Iran without VPN — only
  the user's phone test (VPN off) decides it.
- Subtitles are NOT wired to redgifs (xfree-only engine).
