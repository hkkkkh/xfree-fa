# Proxy 3.17 — tik-porn.com as a second site (NO app install)

Date: 2026-11-28. Commit: (see push record). Railway EU West.
No APK change — the already-installed v3.17 app reaches tik-porn
through the switch pill the proxy injects into served HTML.

## What the site is (verified 2026-11-28 from the VM)
- Next.js app behind Cloudflare; title "TikPorn – Free Short
  Porn Videos TikTok Style | Scroll & Swipe".
- A RedGifs client: feed data comes from its own same-origin
  `/api/proxy?path=niches/<n>/gifs?...` (RedGifs API),
  `/api/proxy?slug=`, `/api/proxy?creator_vids=`;
  images via `/api/img?url=`; demo clips via `/api/candy-video`.
- Video element src (bundle 789): 
  `https://media.tik-porn.com/api/video?url=<redgifs mp4>`.
  Real clips ~4 MB (`media.redgifs.com/<Name>-mobile.mp4`).
  `/video?url=` (no /api) on the media host is a 404 Next page —
  the <video> element uses the /api form.
- Tik-porn's /api/proxy rate-limits THIS VM's rotating
  datacenter IP (`{"gifs":[],"pages":0,"_ratelimited":true}`) —
  whether Railway's egress IP is also limited is only provable
  live (checked below).
- RedGifs hotlink protection: `media.redgifs.com` 403s any
  request carrying a foreign Referer (tik-porn referer -> 403,
  no Referer -> 200). The /g/ relay must send NO Referer/Origin
  for redgifs hosts (fixed in this version).

## What changed (proxy/app.py only)
- `PROXY_VERSION` 3.16 -> 3.17.
- `/tik/<path>` -> https://tik-porn.com/<path> (pages, _next,
  same-origin /api/*); `/tikm/<path>` ->
  https://media.tik-porn.com/<path> (video host). Range
  requests always stream via pool_stream (never buffered).
  Cookies/Set-Cookie forwarded; Referer/Origin rewritten to the
  tik origin. Cache: _next chunks 86400s, /tik/ shell 300s
  (120s first, then raised — 120s TTL = 120s warmup interval
  meant entries expired at the next warmup, the v3.16 bug again).
- `_rewrite_body`: `https://tik-porn.com` -> `/tik`,
  `https://media.tik-porn.com` -> `/tikm` (plain, //,
  `\/` and `\u002F` forms). RedGifs poster/avatar hosts
  (`media.redgifs.com`, `userpic.redgifs.com`) added to
  RELAY_HOSTS -> /g/ relay.
- `_rewrite_location`: tik-porn redirects stay on the proxy.
- `_inject_switch_pill`: small fixed top-left pill in HTML only.
  xfree pages: «تیک‌پورن» -> /tik/ ; tik pages: «ایکس‌فری» -> /.
  Injected in `handle_reverse_proxy` (xfree, after the Jina
  fallback too, before cache put) and `handle_tik_proxy`.
- Warmup loop also warms /tik/ + its _next chunks every 120s.
- do_POST / PUT / DELETE / PATCH route /tik* and /tikm* to the
  same handler.

## Local verification (VM, proxy on :18099)
- /tik/ 200, 37,737 B, pill «ایکس‌فری» present, 0 remaining
  `tik-porn.com` refs (`/tik/android-chrome-512x512.png` etc.).
- /tik/niche/just-boobs 200; media URLs rewritten to
  `/tikm/video?url=https%3A%2F%2Fmedia.redgifs.com%2F...`
  (0 remaining `media.tik-porn.com` refs), pill present.
- /tik/_next chunk 200. /tik/ 2nd fetch X-Proxy-Cache: HIT.
- /tikm/api/video?url=<real redgifs mp4> Range 0-1MB -> 206,
  1,048,576 B, TTFB 0.96 s (direct media host: 206, TTFB 1.07 s).
- /tik/api/candy-video Range -> 200 full file 5,830,097 B
  (upstream ignores Range there — same as xfree /video behaviour
  when upstream answers 200; player handles it).
- /tik/api/img?url=<poster> 200, 116,776 B.
- /g/media.redgifs.com/<poster> 403 BEFORE the Referer fix,
  200 AFTER.
- /tik/api/proxy -> 200 `{"gifs":[],"pages":0,"_ratelimited":true}`
  (VM IP rate-limited, passed through faithfully).
- xfree / -> 502 "upstream blocked (challenge page)" FROM THIS VM
  ONLY (direct xfree.com is 403 Cloudflare from the VM — pre-existing,
  unchanged code path; live Railway check below is the real test).

## Live verification (Railway, after deploy)
- (fill in after deploy: /health, xfree / 200 + pill, /tik/ 200 +
  pill, /tikm video Range 206, xfree /video Range 206,
  /dub/gemini-status configured:true, /tik/api/proxy feed result
  — does Railway's IP get real gifs or _ratelimited?)

## Honest limits (phone-only)
- Whether Railway's egress IP is rate-limited by tik-porn's
  /api/proxy (feed empty vs real videos) — only the live check
  above + the user's phone test decide it. If rate-limited, the
  server-rendered niche pages still show videos, but the
  homepage feed would be empty.
- Whether tik-porn.com is filtered in Iran without VPN — only
  the user's phone test (VPN off) decides it; the proxy route is
  the same mechanism as xfree.
- Subtitles are NOT wired to tik-porn (xfree-only engine).
- The pill covers the site's own top-left corner (logo/hamburger
  area) — user-approved placement.
