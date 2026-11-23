# v3.12 — host migration OFF Render (IN PROGRESS — blocked on account creation)

## Why
Render Hobby workspace exceeded its 5 GB/month free outbound bandwidth quota
on 2026-11-23 16:04 ("Suspended by Monthly Quota Exceeded"). The proxy
returns 503 "Service Suspended" and has NO Resume button; without paying it
returns automatically ~2026-12-01. Video bytes relayed through the proxy
(~174 MB per video at 4.27 Mbps) burned the quota in days. The app is dead
until the proxy moves.

## Host research (verified 2026-11-24 from vendor docs/pricing pages)
Bandwidth is the deciding factor; no-card signup is a hard requirement
(user has NO international card — Iranian cards rejected, no one abroad).

| Host | Bandwidth | Card? | Sleep | Verdict |
|---|---|---|---|---|
| Render (current) | 5 GB/mo | No | 15 min idle | SUSPENDED — quota burned |
| Koyeb | 100 GB/mo | YES ($29 hold, since Feb 2026) | 1 h idle | EXCLUDED — card |
| Hugging Face Spaces (Docker) | unmetered | PRO $9/mo to create (since Jul/Aug 2026) | 48 h idle | EXCLUDED — paid |
| Back4App Containers | 100 GB/mo | No | **container killed after exactly 60 min; URL lasts 1 h** (measured by Back4App's own blog, Sep 2026) | EXCLUDED — demo host only |
| Miget | n/a | YES (saved card required) | 30 min idle | EXCLUDED — card |
| Northflank Sandbox | $20/mo allowance, egress $0.06/GB | Pricing page says no card, but billing docs: "all users must add a payment method to start creating resources, regardless of plan" | Always on | EXCLUDED — card-gated in practice |
| SnapDeploy | 100 container-h/mo | No | 15 min idle; WebSockets need $12/mo Always-On | EXCLUDED — 100 h/mo ≈ 4 days, WS gated |
| Zeabur | 10 GB/mo serverless | No | auto-sleep; always-on containers need paid Dev plan | EXCLUDED — no free container |
| Clever Cloud | n/a | No permanent free tier — one-time signup credits only | n/a | EXCLUDED — trial only |
| Oracle / Fly.io / GCP / AWS / Azure | — | YES | — | EXCLUDED — card |
| Deno Deploy | 20 GiB/mo | No (email only) | serverless, wakes fast | FALLBACK ONLY — needs a full TypeScript rewrite of the ~2000-line Python proxy, and Deno Deploy sunsets April 2027 |
| Cloudflare Workers | unmetered | No | serverless | NOT RECOMMENDED — full rewrite + Cloudflare bans proxy use in ToS |
| **Railway** | **$0.05/GB egress from a usage pool** | **No** | **No sleep during 30-day trial; Free plan sleeps after 5–10 min idle** | **CHOSEN — bridge** |

## Chosen: Railway (30-day trial, no card)
- New accounts: one-time $5 credit for 30 days, no credit card, services do
  NOT sleep during the trial. Then Free plan: $1/month usage credit.
- Cost model (docs.railway.com/pricing/free-plan, read 2026-11-24): RAM
  $10/GB/mo, CPU $20/vCPU/mo, egress $0.05/GB — all from the same pool.
  The proxy's measured RSS is ~24 MB (~$0.25/mo) and it is mostly idle, so
  the $5 trial buys roughly $4+ of egress ≈ 80 GB ≈ 450+ videos.
  After the trial, the $1/mo Free plan only fits if video bytes do NOT flow
  through the proxy (see "Permanent fix" below).
- WebSocket: supported; a service with an open WebSocket stays awake.
  Railway closes HTTP requests after 5 idle minutes, but the relay pings
  and reconnects, so the Gemini relay should survive.
- Deploy: from GitHub repo hkkkkh/xfree-fa. `Dockerfile` (root) + 
  `railway.json` (healthcheck /health) prepared in this commit. The proxy
  binds 0.0.0.0:$PORT (Railway sets PORT automatically), stdlib only, no
  build step. No code changes were needed.
- Trial needs GitHub verification for full network access (limited trial =
  restricted outbound network). The user's GitHub account hkkkkh should
  verify; if the trial comes back "limited", the proxy cannot reach
  xfree.com/Gemini and Railway is a dead end.

## STATUS: blocked on account creation (needs the user's browser)
No host can be deployed without an account, and account signup needs the
user's GitHub OAuth in a browser — a subagent cannot do it. Exact steps for
the parent agent (browser task):
1. railway.com → sign up with the user's GitHub account hkkkkh
   (no card anywhere in this flow). If Railway asks to verify, connect the
   GitHub account.
2. New project → Deploy from GitHub repo → hkkkkh/xfree-fa, branch main.
   Railway detects the Dockerfile at the repo root and builds it.
3. Service Settings → Networking → Generate Domain → note the public URL
   (https://<name>.up.railway.app). Variables: none needed yet.
4. Verify live from the VM: /health → ok, homepage 200, 1 MB video Range
   → 206, /dub/health, POST /dub/stt 200, /dub/gemini-status →
   configured false (expected — no key yet).
5. THEN build v3.12 APK: versionCode 33, PROXY_BASE in
   app/src/main/java/com/xfree/fa/api/Proxy.java:18,
   WS_HOST in SubtitleEngine.java:112, and the host check in
   WebActivity.java:648 ("xfree-fa-proxy.onrender.com") → new Railway host.
   Same package + keystore. Copy to ~/workspace/your_files/xfree-fa-3.12.apk.
6. Repoint the keep-alive cron xfree-proxy-keepalive at the new host URL
   (currently still pinging the dead Render URL every 6 min — harmless,
   fails silently, no user messages).
7. User re-enters the Gemini key on Railway (Variables → GEMINI_API_KEY)
   via the browser, exactly as on Render — agents cannot read the old key
   back. Until then subtitles work via the keyless HTTP /dub/stt fallback
   only (Gemini Live relay returns gemini_not_configured).

## Permanent fix (required for ANY free host to last)
Video bytes must stop flowing through the proxy. Plan (not yet built):
proxy answers /video with a 302 redirect to the direct CDN URL instead of
relaying bytes, so the proxy carries only HTML/API/subtitles (a few hundred
MB/month). BLOCKER: unproven whether cdn.xfree.com is reachable from the
user's phone in Iran without VPN. Phone test needed: open a direct CDN mp4
URL in the phone browser — if it plays, video-direct works and Render's
5 GB (back Dec 1) or Railway's $1/mo both last all month. If the CDN is
blocked in Iran, every free host will burn its quota on video and there is
no free permanent home — only the 30-day Railway trial, then Render again
each month until its 5 GB burns.

## Render fallback
The suspended Render service returns automatically ~2026-12-01 with a fresh
5 GB. Do NOT create a second Render workspace/account to get fresh quota —
that is quota evasion, not a solution.
