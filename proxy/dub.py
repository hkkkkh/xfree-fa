#!/usr/bin/env python3
"""XFreeFa live Persian dubbing — proxy side (v3.3, dub 1.2).

Self-contained, standard library only (no pip installs on Render free).
Mounted by proxy/app.py: /dub/health (GET) and /dub/segment (POST).
Nothing here needs a key, a login or a paid service:

  STT       Google Web Speech v2 (www.google.com/speech-api/v2/recognize,
            client=chromium). It REQUIRES a `key` parameter — the value used
            here is the PUBLIC Chromium demo key that ships in the
            open-source Chromium browser itself (and that this workspace's
            H.mbot already used for the same endpoint). It is NOT a private
            key, NOT the user's key, and NOT anyone's personal credential:
            it is Google's published demo credential for the Chromium
            project. Verified live 2026-11-22 from this VM: 200 + transcript
            "hello world this is a test of speech", confidence 0.99, ~1.2 s
            for a 2.5 s clip, POSTing raw 16-bit PCM (audio/l16) — no FLAC
            encoder needed. Without the key the endpoint answers
            400 "Missing parameter: key"; the v1 endpoint is 404-dead.
  TRANSLATE (v3.3 chain, dub 1.2) Google Translate free gtx endpoint
            on translate.googleapis.com, then gtx on translate.google.com,
            then MyMemory (api.mymemory.translated.net, no key). LIVE
            FROM RENDER (verified 2026-11-22): Google answers BOTH gtx
            hosts with a 302 to /sorry/index — Render's shared egress IP
            is blocked by Google ("unusual traffic"), in any code path
            (the /g/ relay gets the same 302). MyMemory WITHOUT de=
            429s on shared IPs (anonymous 5,000 chars/day/IP exhausted);
            WITH de=xfree-fa@example.com it works (documented 50,000
            chars/day; de is only a quota key, no account). So from
            Render the chain in practice runs on MyMemory. A host that
            failed is skipped for 60 s. /dub/health probes every
            candidate and reports which one works. (Gemini was
            considered and ruled out: the Gemini key lives in Muse's
            secure vault and cannot be put on Render. Also tried and
            rejected 2026-11-22: Bing ttranslatev3 (statusCode 205 even
            with a fresh IG token), DeepL free jsonrpc ("older version"
            reject), Edge translator + LibreTranslate argos (timeouts),
            AllOrigins->gtx (522), SimplyTranslate (empty result),
            Lingva (disconnected).)
  TTS       Microsoft Edge Read-Aloud neural voices over WebSocket
            (speech.platform.bing.com, fa-IR-DilaraNeural female /
            fa-IR-FaridNeural male, no key, no login). The protocol needs
            a Sec-MS-GEC token: a time-based token whose hash is a
            Microsoft-modified SHA-256 + a CS64 digest. The algorithm is
            reimplemented here in pure stdlib from the published
            open-source edge-tts implementation (MIT). Verified live
            2026-11-22 from this VM with a stdlib WebSocket client:
            23,760-byte real Persian MP3 (24 kHz, 48 kbps), 3.96 s of
            audio, in 2.1 s wall time. Google Translate TTS is NOT usable
            for Persian: translate_tts with tl=fa / tl=fa-IR returns
            HTTP 400 for every text (verified 6x; tl=en and tl=ar return
            real MP3s), so it is only a fallback for non-Persian text.

Pipeline (segment-streamed, first dub plays within seconds):
  JS in the app captures ~5 s PCM chunks from the site's <video>
  (AudioWorklet, ScriptProcessor fallback) -> POST /dub/segment
  {pcm: raw s16le 16 kHz mono, t0: video timestamp} ->
  silence check (RMS) -> STT (en-US, fa-IR fallback) -> translate
  to Persian (skipped when source is already Persian) -> Edge TTS
  (fa) -> JSON {fa_text, audio_b64 (MP3), audio_dur, timings}.
  The JS schedules each MP3 at its segment's video timestamp and ducks
  the original volume to ~0.15 while dubbing.

Honest limits (also surfaced by /dub/health and in the JS UI):
  * Dub lags speech by one segment (~5-8 s): STT+translate+TTS take
    ~3-5 s per 5 s segment on the free tier.
  * Web Speech is a short-phrase recogniser: weak on long, fast or
    music-covered speech; noisy segments return empty (skipped, never
    invented).
  * Adult-video speech is sparse: most segments are silent and are
    skipped without any request.
  * Edge TTS is an unofficial endpoint: Microsoft can change or block
    it at any time (the token algorithm is version-pinned); if it
    breaks, /dub/segment returns the Persian TEXT with audio_b64 null
    and the app falls back to on-device speechSynthesis when available.
  * Google gtx translate 429s intermittently; retried, and reported.
"""

import base64
import hashlib  # noqa: F401  (kept for clarity; token uses custom SHA-256)
import http.client
import json
import os
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DUB_VERSION = "2.0"

# v3.10 instrumentation (ChatGPT priority 3): counters for every
# step of the subtitle chain, exposed via /dub/health ("stats") so
# a break can be located without guessing. The app's "configured:
# true" only means the env var exists; these counters show what
# actually flowed: audio packets/bytes received, Gemini messages,
# transcripts, STT calls, subtitle messages sent.
_STATS = {
    "ws_audio_packets": 0,   # binary PCM frames received (both WS paths)
    "ws_audio_bytes": 0,
    "ws_stt_calls": 0,       # google_stt calls from /dub/ws workers
    "ws_stt_empty": 0,       # ... that returned empty text
    "http_stt_calls": 0,     # POST /dub/stt calls
    "gemini_sessions": 0,    # Gemini Live sessions that completed setup
    "gemini_messages": 0,    # messages received from Gemini Live
    "gemini_transcripts": 0,  # outputTranscription (Persian) parsed
    "gemini_input_transcripts": 0,  # inputTranscription (source lang)
    "gemini_errors": 0,      # gemini_error / gemini_closed sent to app
    "subtitles_sent": 0,     # final/interim caption messages sent
    "ws_connects": 0,        # WS upgrades accepted (both WS paths)
    "ws_disconnects": 0,     # WS sessions ended
    "gemini_audio_bytes": 0,  # PCM bytes forwarded to Gemini Live
    "http_stt_fallback": 0,  # POST /dub/stt calls carrying seq/id
                             # (HTTP-fallback chunks from the phone)
}
_STATS_LOCK = threading.Lock()

# v3.17 stage TIMING (Claude #6 + ChatGPT #4): each session's
# start time (perf_counter) so every _dlog line carries the
# elapsed ms since the session began — the measurement both
# answers ask for (BUTTON_CLICK->WS_OPEN->SESSION_READY->
# FIRST_AUDIO->FIRST_TEXT), without a separate timing channel.
_SESSION_T0 = {}
_SESSION_T0_LOCK = threading.Lock()


def _stat(name, n=1):
    try:
        with _STATS_LOCK:
            _STATS[name] = _STATS.get(name, 0) + n
    except Exception:
        pass


def _dlog(session, stage, extra=""):
    """v3.11 stage log (ChatGPT #4): one line per subtitle-chain
    stage, all sharing the phone's session id, so a dead subtitle
    can be located in the Render logs without guessing:
    WS_CONNECT / WS_HANDSHAKE_OK / WS_RECEIVED_AUDIO /
    WS_AUDIO_BYTES / GEMINI_CONNECTED / GEMINI_AUDIO_SENT /
    GEMINI_TRANSCRIPT_RECEIVED / WS_TRANSCRIPT_SENT /
    WS_DISCONNECTED. The session id comes from the phone (?sid=)
    when present, else a generated one.
    v3.17: every line also carries +<ms>ms since the session
    started (perf_counter), so the slow stage is visible in the
    log alone."""
    try:
        el = ""
        with _SESSION_T0_LOCK:
            t0 = _SESSION_T0.get(session)
        if t0 is not None:
            el = " +%.0fms" % ((time.perf_counter() - t0) * 1000)
        print("dub: [%s] %s%s %s" % (session, stage, el, extra),
              flush=True)
    except Exception:
        pass


def _new_session(handler):
    """Session id shared phone<->proxy: ?sid= from the phone when
    present, else a fresh 12-hex id. v3.17: also records the
    session start (perf_counter) for _dlog elapsed timing."""
    try:
        q = urllib.parse.parse_qs(
            urllib.parse.urlparse(handler.path).query)
        sid = (q.get("sid") or [""])[0].strip()
        if sid:
            sid = sid[:32]
            with _SESSION_T0_LOCK:
                _SESSION_T0.setdefault(sid, time.perf_counter())
            return sid
    except Exception:
        pass
    sid = secrets.token_hex(6)
    with _SESSION_T0_LOCK:
        _SESSION_T0.setdefault(sid, time.perf_counter())
    return sid

# v3.9 (2026-11-22) — subtitle root-cause fixes, proved live:
#  (a) /dub/ws returned EMPTY STT for every window on live Render
#      while HTTP /dub/stt on the same server returned text for the
#      same audio (proved 2026-11-22: WS finals all "" with 0.2 s
#      turnarounds; local WS works; concurrent HTTP STT works live).
#      The WS debug channel could not tell an empty transcript from
#      a thrown exception (google_stt returns None on error and the
#      debug printed ""). _run_stt now reports the exact error.
#  (b) Gemini Live relay WORKS live at realtime speed (8 Persian
#      interims over 18 s of speech, first at 5.7 s) with the
#      user's AQ.-prefixed key — the key is valid. The bridge now
#      also sends it as the documented `x-goog-api-key` header
#      (in addition to ?key=), and /dub/gemini-test does a real
#      REST generateContent call and returns the raw HTTP status +
#      error — "configured: true" alone proves nothing.
#  (c) The Gemini reader used to kill the session after 30 s of
#      Gemini silence (socket timeout -> gemini_closed), even
#      though Gemini can legitimately take longer to answer on a
#      slow link. It now waits up to 90 s like the WS path.
#  (d) WS finals now also try a server-side translate: when the
#      server CAN translate (it usually cannot from Render's
#      burned IP), the Persian text is sent straight away, so
#      captions do not depend on the phone's gtx call alone.

# v3.4: DUBBING IS DEAD — the app is subtitles-only now (user order).
# Root cause of dead translate: Render's SHARED egress IP is burned
# for every free translate API (Google 302s both gtx hosts to
# /sorry/index; MyMemory 429s — the 50k chars/day/IP quota is shared
# by every service on that IP). The fix is architectural, not another
# candidate: translation now happens ON THE PHONE (the app fetches
# gtx/MyMemory directly from the user's own mobile IP, which is not
# burned — Chrome page-translate on the same phone already proves
# the phone reaches Google Translate). The proxy only does STT
# (which works fine from Render, ~140 ms) via the new /dub/stt
# endpoint; /dub/translate remains as a last-resort fallback for the
# phone. /dub/segment (dub pipeline) is kept for compatibility but
# the app no longer calls it and no TTS audio is ever produced.

# ----------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------
SEGMENT_MAX_SECONDS = 10          # refuse PCM longer than this
SEGMENT_SAMPLE_RATE = 16000       # JS captures 16 kHz mono s16le
SILENCE_RMS = 120                 # below this -> silent, skip STT entirely
STT_TIMEOUT = 15
TRANSLATE_TIMEOUT = 10
# Per-candidate timeouts for the translate chain (v3.3): the chain
# tries each host once; a host that failed is skipped for 60 s so a
# dead host never stalls every segment.
TRANSLATE_CANDIDATE_TIMEOUTS = {
    "gtx-apis": 6,
    "gtx-com": 6,
    "mymemory": 8,
}
_TRANSLATE_DOWN = {}  # candidate -> unix time of last failure
_LAST_TRANSLATE_ERROR = {}  # candidate -> short error text (health diag)
_LAST_TRANSLATE_ENGINE = None
TTS_TIMEOUT = 30
TTS_VOICE = "fa-IR-DilaraNeural"  # female default; see TTS_VOICE_FEMALE/MALE
TTS_VOICE_FEMALE = "fa-IR-DilaraNeural"  # female Persian neural voice
TTS_VOICE_MALE = "fa-IR-FaridNeural"     # male Persian neural voice
# Gender detection: median fundamental frequency (F0) of voiced frames.
# Adult male F0 ~85-155 Hz, adult female ~165-255 Hz; 155 Hz splits them.
GENDER_F0_THRESHOLD = 155.0
# Dub speaks slightly faster than the original so it catches up with
# live speech instead of falling further behind each segment.
TTS_CATCHUP_RATE = "+5%"
TTS_RATE = "+0%"
TTS_VOLUME = "+0%"
TTS_PITCH = "+0Hz"

# Public Chromium demo key (see module docstring). NOT a private key.
_CHROMIUM_KEY = "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw"

_EDGE_HOST = "speech.platform.bing.com"
_EDGE_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
_EDGE_GEC_VERSION = "2-143.0.3650.75"


# ======================================================================
# Sec-MS-GEC token (port of the published edge-tts DRM algorithm).
# Custom SHA-256 (different IV/state/round constants) + CS64 digest.
# Verified against the edge-tts library for a fixed timestamp (see
# __main__ self-test in the repo smoke test, not shipped here).
# ======================================================================
_U32 = 0xFFFFFFFF

_SHA256_INITIAL = (
    0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
    0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
)
_SHA256_STATE = (
    0xFD9FBEA2, 0x91243CE7, 0xBE499094, 0xCD2E1E0A,
    0x6C006F49, 0x04BD35AB, 0xFEB0A7E4, 0x03EFFBD2,
)
_SHA256_SCHEDULE = (
    0xEB24BF9E, 0x1558691F, 0xA0C09EE2, 0x0CFA32BA,
    0xA622EE8C, 0xD1DD9491, 0xC0DD25A0, 0xF9E70AB6,
    0xF0C31573, 0x7058CE4E, 0x7188E606, 0x00E5AB33,
    0xD258B060, 0x72766564,
)
_SHA256_K = (
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5,
    0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
    0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
    0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
    0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC,
    0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
    0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7,
    0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
    0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13,
    0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
    0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3,
    0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
    0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5,
    0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
    0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208,
    0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
)
_SHA256_ADJ = 0xC9DC06A7
_SHA256_SIGMA0_ADJ = 0x059C0B80
_SHA256_SIGMA1_ADJ = 0x000B92C0

_CS64_COEFFS = (
    (0xE82E91B1, 0xB32700D0), (0x44DE24C6, 0x88DFF51D),
    (0x3BEB65BC, 0x29514038), (0xC89C5933, 0xB328CBDF),
    (0x8D1EC211, 0xD405B4D5), (0x314EF5D5, 0x26E55F3C),
    (0xE92491B1, 0x607801C6), (0xF0A5B9D5, 0x82B2C1FB),
    (0x06064CF3, 0xFA51A3DA), (0xDA79FA60, 0x207CC464),
    (0xBED20560, 0x3C3C74F3), (0x89F9C9F8, 0xD2D14B61),
    (0x35FA38C6, 0xBB04A2C1), (0xCB49E3C6, 0xC5626167),
)
_CS64_FACTOR = 0x82893933
_CS64_ADJ0 = 0xF2F90B1A
_CS64_ADJ1 = 0x0B1A0000


def _rotr(x, n):
    return ((x >> n) | (x << (32 - n))) & _U32


def _sigma0(v):
    return (_rotr(v, 7) ^ _rotr(v, 18) ^ (v >> 3)) & _U32


def _sigma1(v):
    return (_rotr(v, 17) ^ _rotr(v, 19) ^ (v >> 10)) & _U32


def _sha256_rounds(state, words, first_round=0):
    """One SHA-256 compression with the custom constants/state.
    Plain (non-unrolled) loop — identical low-32-bit result to the
    reference unrolled version, which uses a multiply trick that only
    affects bits above 32 (masked away at the end of each round)."""
    a, b, c, d, e, f, g, h = state
    consts = (_SHA256_K[:14] + (_SHA256_ADJ,) + _SHA256_K[15:]
              if first_round == 14 else _SHA256_K)
    for i in range(first_round, 64):
        s1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
        ch = (e & f) ^ ((~e & _U32) & g)
        t1 = (h + s1 + ch + consts[i] + words[i]) & _U32
        s0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
        maj = (a & b) ^ (a & c) ^ (b & c)
        t2 = (s0 + maj) & _U32
        h, g, f, e, d, c, b, a = (g, f, e, (d + t1) & _U32,
                                 c, b, a, (t1 + t2) & _U32)
    return a, b, c, d, e, f, g, h


def _expand_schedule(words, first_word=16):
    for i in range(first_word, 64):
        words.append((_sigma1(words[i - 2]) + words[i - 7]
                      + _sigma0(words[i - 15]) + words[i - 16]) & _U32)


def _sha256_init(data):
    """Initial state for token data (first 7 bytes seed the schedule)."""
    words = [0] * 14 + [int.from_bytes(data[:3], "big")]
    words.append(int.from_bytes(data[3:7], "big"))
    for i in range(16, 30):
        v = _SHA256_SCHEDULE[i - 16]
        if i - 2 >= 14:
            v += ((_sigma1(words[i - 2]) ^ _SHA256_SIGMA1_ADJ)
                  if i == 16 else _sigma1(words[i - 2]))
        if i - 7 >= 14:
            v += words[i - 7]
        if i - 15 >= 14:
            v += ((_sigma0(words[i - 15]) ^ _SHA256_SIGMA0_ADJ)
                  if i == 29 else _sigma0(words[i - 15]))
        words.append(v & _U32)
    words.append((_sigma1(words[28]) + words[23] + _sigma0(words[15])
                  + words[14] + _SHA256_ADJ - _SHA256_K[14]) & _U32)
    _expand_schedule(words, 31)
    working = _sha256_rounds(_SHA256_STATE, words, 14)
    return tuple((x + y) & _U32 for x, y in zip(_SHA256_INITIAL, working))


def _sha256_digest(data):
    if len(data) < 7:
        raise ValueError("token salt must contain at least seven bytes")
    length = 57 + len(data)
    padded = data[7:] + b"\x80" + b"\x00" * ((55 - length) % 64)
    padded += struct.pack(">Q", length * 8)
    state = _sha256_init(data[:7])
    for off in range(0, len(padded), 64):
        words = list(struct.unpack(">16I", padded[off:off + 64]))
        _expand_schedule(words)
        working = _sha256_rounds(state, words)
        state = tuple((x + y) & _U32 for x, y in zip(state, working))
    return struct.pack(">8I", *state)


def _rol16(v):
    v &= _U32
    return ((v << 16) | (v >> 16)) & _U32


def _cs64_digest(data, digest):
    key_a, key_b = struct.unpack_from("<II", digest)
    key_a |= 1
    key_b |= 1
    factors = ((key_a * _CS64_FACTOR) & _U32,
               (key_b * _CS64_FACTOR) & _U32)
    terms = [(c * factors[i % 2]) & _U32
             for i, (c, _) in enumerate(_CS64_COEFFS)]
    offsets = [o for _, o in _CS64_COEFFS]
    padded = b"\x00" + data
    padded += b"\x00" * ((-len(padded)) % 4)
    words = list(struct.unpack("<%dI" % (len(padded) // 4), padded))
    if len(words) % 2:
        words.append(0)
    for i, word in enumerate(words):
        term = word * (key_a if i % 2 == 0 else key_b)
        off = _rol16(word) * (0xEF0569FB if i % 2 == 0 else 0xC31713DB)
        if i == 0:
            term += _CS64_ADJ0 * factors[0]
            off += _CS64_ADJ1
        terms.append(term & _U32)
        offsets.append(off & _U32)
    swap_acc = swap_last = state = acc = 0
    for i in range(0, len(terms), 2):
        x, y = terms[i], terms[i + 1]
        t = _rol16((offsets[i] + x) & _U32)
        q = (t * 0x76F50F34) & _U32
        swap_acc = (swap_acc + (_rol16(q) * 0xC3EFEA97 + q)) & _U32
        t = _rol16((offsets[i + 1] + y) & _U32)
        q = (t * 0x1881CE3C) & _U32
        swap_last = (_rol16(q) * 0x35BD1EC9 + q) & _U32
        swap_acc = (swap_acc + swap_last) & _U32
        state = (state * key_a + x) & _U32
        state = (_rol16(state) * 0xCF98B111) & _U32
        state = (_rol16(state) * 0x87085B9F) & _U32
        state = (_rol16(state) * 0x12CEB96D) & _U32
        state = (_rol16(state) * 0x257E1D83) & _U32
        acc = (acc + state) & _U32
        t = (state * key_b + y) & _U32
        t = (_rol16(t) * 0xA27416F5) & _U32
        t = (_rol16(t) * 0xD38396FF) & _U32
        state = (_rol16((_rol16(t) * 0x7C932B89) & _U32) * 0xBFA49F69) & _U32
        acc = (acc + state) & _U32
    return swap_last ^ state, swap_acc ^ acc


def _token_hash(data):
    digest = _sha256_digest(data)
    cs64 = struct.pack("<II", *_cs64_digest(data, digest)).hex().upper()
    return cs64, digest.hex().upper()


def generate_sec_ms_gec(unix_timestamp=None):
    """Sec-MS-GEC v2 token: '<CS64>-<SHA256>' for the current time
    (Windows file time, rounded down to 5 min)."""
    if unix_timestamp is None:
        unix_timestamp = datetime.now(timezone.utc).timestamp()
    ticks = unix_timestamp + 11644473600
    ticks -= ticks % 300
    ticks *= 1e9 / 100
    raw = ("%d%s" % (ticks, _EDGE_TOKEN)).encode("ascii")
    cs64, sha = _token_hash(raw)
    return "%s-%s" % (cs64, sha)


def _edge_muid():
    return secrets.token_hex(16).upper()


# ======================================================================
# Minimal WebSocket client (stdlib only, CONNECT-tunnel aware).
# The Render host has no egress proxy; this VM does (https_proxy env),
# so the tunnel path is exercised here and skipped on Render.
# ======================================================================
def _ws_connect(host, path, headers, timeout=30, safe_error=False):
    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or "")
    if proxy:
        p = urllib.parse.urlparse(proxy)
        if p.scheme in ("http", "https") and p.hostname:
            conn = http.client.HTTPSConnection(p.hostname, p.port or 3128,
                                               timeout=timeout)
            conn.set_tunnel(host, 443)
            conn.connect()
            sock = conn.sock  # already TLS-wrapped by HTTPSConnection
        else:
            raise RuntimeError("unsupported proxy scheme")
    else:
        raw = socket.create_connection((host, 443), timeout=timeout)
        sock = ssl.create_default_context().wrap_socket(
            raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req_lines = ["GET %s HTTP/1.1" % path, "Host: %s" % host,
                 "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: %s" % key,
                 "Sec-WebSocket-Version: 13"]
    for k, v in headers.items():
        req_lines.append("%s: %s" % (k, v))
    req_lines.append("")
    req_lines.append("")
    sock.sendall("\r\n".join(req_lines).encode("utf-8"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("websocket handshake: connection closed")
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        # safe_error: never include the response body in the
        # exception — for the Gemini bridge the handshake request
        # carries the API key, and a server error body must never
        # be able to leak anything key-related into a log.
        if safe_error:
            raise RuntimeError("websocket handshake failed")
        raise RuntimeError("websocket handshake failed: %s"
                           % resp[:200].decode("utf-8", "replace"))
    return sock


def _ws_send_text(sock, text):
    data = text.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _ws_recv_exact(sock, n):
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise EOFError("websocket closed")
        out += chunk
    return out


def _js_date():
    return time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 "
                         "(Coordinated Universal Time)", time.gmtime())


def edge_tts(text, voice=TTS_VOICE, timeout=TTS_TIMEOUT,
             rate=TTS_RATE):
    """Synthesise Persian text with Edge neural voices.
    Returns (mp3_bytes, duration_seconds). Raises on failure."""
    import uuid
    import xml.sax.saxutils as sax

    if not text or not text.strip():
        raise ValueError("empty text")
    text = text.strip()
    if len(text) > 800:  # keep one WS round-trip short; JS splits longer
        text = text[:800]
    conn_id = uuid.uuid4().hex
    path = ("/consumer/speech/synthesize/readaloud/edge/v1"
            "?TrustedClientToken=%s&ConnectionId=%s&Sec-MS-GEC=%s"
            "&Sec-MS-GEC-Version=%s" % (
                _EDGE_TOKEN, conn_id, generate_sec_ms_gec(),
                _EDGE_GEC_VERSION))
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"),
        "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Cookie": "muid=%s;" % _edge_muid(),
    }
    sock = _ws_connect(_EDGE_HOST, path, headers, timeout=timeout)
    sock.settimeout(timeout)
    try:
        config = (
            "X-Timestamp:%s\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":'
            '{"sentenceBoundaryEnabled":"true",'
            '"wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
            % _js_date())
        _ws_send_text(sock, config)
        ssml = (
            "<speak version='1.0' "
            "xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>"
            "<voice name='%s'><prosody pitch='%s' rate='%s' volume='%s'>"
            "%s</prosody></voice></speak>"
            % (voice, TTS_PITCH, rate, TTS_VOLUME, sax.escape(text)))
        msg = ("X-RequestId:%s\r\nContent-Type:application/ssml+xml\r\n"
               "X-Timestamp:%sZ\r\nPath:ssml\r\n\r\n%s"
               % (conn_id, _js_date(), ssml))
        _ws_send_text(sock, msg)
        audio = bytearray()
        audio_dur = 0.0
        while True:
            op, payload = _ws_recv_frame(sock)
            if op == 8:  # close
                if len(payload) >= 2:
                    code = struct.unpack(">H", payload[:2])[0]
                    raise RuntimeError("edge tts closed: %d %s" % (
                        code, payload[2:].decode("utf-8", "replace")[:120]))
                raise RuntimeError("edge tts closed by server")
            if op == 1:
                txt = payload.decode("utf-8", "replace")
                if "Path:turn.end" in txt:
                    break
                if "Path:audio.metadata" in txt:
                    try:
                        meta = json.loads(
                            txt.split("\r\n\r\n", 1)[1])
                        for item in meta.get("Metadata", []):
                            d = item.get("Data", {})
                            off = d.get("Offset", 0)
                            dur = d.get("Duration", 0)
                            audio_dur = max(
                                audio_dur, (off + dur) / 1e7)
                    except Exception:
                        pass
            elif op == 2:
                if len(payload) >= 2:
                    hl = int.from_bytes(payload[:2], "big")
                    audio += payload[hl + 2:]
        if not audio:
            raise RuntimeError("edge tts returned no audio")
        if audio_dur <= 0:
            audio_dur = len(audio) * 8 / 48000.0  # 48 kbps CBR
        return bytes(audio), audio_dur
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ======================================================================
# Google Web Speech v2 (STT) + Google Translate free (gtx)
# ======================================================================
def _pcm_rms(pcm):
    if len(pcm) < 2:
        return 0
    n = len(pcm) // 2
    vals = struct.unpack("<%dH" % n, pcm[:n * 2])
    mean_sq = sum((v - 32768) ** 2 for v in vals) / float(n)
    return mean_sq ** 0.5


def google_stt(pcm, lang, timeout=STT_TIMEOUT):
    """(transcript, confidence) — ('', 0.0) when no speech was
    recognised, (None, 0.0) when the request itself errored."""
    url = ("https://www.google.com/speech-api/v2/recognize"
           "?output=json&lang=%s&key=%s&client=chromium"
           "&maxresults=3&pfilter=2"
           % (urllib.parse.quote(lang), _CHROMIUM_KEY))
    req = urllib.request.Request(
        url, data=pcm,
        headers={"Content-Type": "audio/l16; rate=16000"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception:
        return None, 0.0
    segs, confs = [], []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        for res in obj.get("result", []):
            alts = res.get("alternative", [])
            if alts and alts[0].get("transcript"):
                segs.append(alts[0]["transcript"])
                if isinstance(alts[0].get("confidence"), (int, float)):
                    confs.append(float(alts[0]["confidence"]))
    text = " ".join(segs).strip()
    if not text:
        return "", 0.0
    return text, (sum(confs) / len(confs) if confs else 0.5)


def _translate_gtx_host(text, src, dst, host, timeout):
    """One gtx attempt against one Google host. Text or None."""
    url = ("https://%s/translate_a/single"
           "?client=gtx&sl=%s&tl=%s&dt=t&q=%s"
           % (host, urllib.parse.quote(src), urllib.parse.quote(dst),
              urllib.parse.quote(text)))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    out = "".join(seg[0] for seg in data[0] if seg[0])
    return out.strip() or None


def _translate_mymemory(text, src, dst, timeout=8):
    """MyMemory free API (no key). Text or None. The anonymous quota
    is 5,000 chars/day per IP and the shared Render/VM IPs hit HTTP
    429 on it; passing de=<email> raises it to 50,000 chars/day
    (documented MyMemory behaviour — de is only a quota key, no
    account, no verification). quotaFinished sits at the TOP level
    of the response, not inside responseData."""
    url = ("https://api.mymemory.translated.net/get?q=%s"
           "&langpair=%s|%s&de=xfree-fa@example.com"
           % (urllib.parse.quote(text), urllib.parse.quote(src),
              urllib.parse.quote(dst)))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("responseStatus") != 200:
        return None
    if data.get("quotaFinished"):
        return None
    rdata = data.get("responseData") or {}
    out = (rdata.get("translatedText") or "").strip()
    if not out or "MYMEMORY WARNING" in out or "NO QUERY" in out:
        return None
    return out


def _candidate_down(name):
    return (time.time() - _TRANSLATE_DOWN.get(name, 0)) < 60


def _translate_one(name, text, src, dst, timeout, force=False):
    """One candidate, honouring the 60 s down-cache unless forced
    (health probes force). Returns text or None."""
    if not force and _candidate_down(name):
        return None
    try:
        if name == "gtx-apis":
            out = _translate_gtx_host(
                text, src, dst, "translate.googleapis.com", timeout)
        elif name == "gtx-com":
            out = _translate_gtx_host(
                text, src, dst, "translate.google.com", timeout)
        elif name == "mymemory":
            out = _translate_mymemory(text, src, dst, timeout)
        else:
            return None
    except Exception as exc:  # noqa: BLE001 - record, then report None
        out = None
        _LAST_TRANSLATE_ERROR[name] = (
            "%s: %s" % (type(exc).__name__, exc))[:120]
    if out:
        _TRANSLATE_DOWN.pop(name, None)
        _LAST_TRANSLATE_ERROR.pop(name, None)
        return out
    _LAST_TRANSLATE_ERROR.setdefault(name, "returned-no-text")
    _TRANSLATE_DOWN[name] = time.time()
    return None


def google_translate(text, src, dst="fa", timeout=TRANSLATE_TIMEOUT):
    """Translate via a fallback chain (v3.3). Render's shared egress
    IP is rate-limited/timed-out on translate.googleapis.com gtx
    (verified live 2026-11-22: 2x 10 s timeouts), so one dead host
    must not kill dubbing: gtx on translate.googleapis.com, then
    gtx on translate.google.com, then MyMemory (no key). Returns
    translated text, or None when every candidate failed."""
    global _LAST_TRANSLATE_ENGINE
    for name in ("gtx-apis", "gtx-com", "mymemory"):
        out = _translate_one(
            name, text, src, dst,
            TRANSLATE_CANDIDATE_TIMEOUTS[name])
        if out:
            _LAST_TRANSLATE_ENGINE = name
            return out
    _LAST_TRANSLATE_ENGINE = None
    return None


def google_tts_fallback(text, lang="fa", timeout=15):
    """Google translate_tts. NOTE: verified DEAD for Persian
    (HTTP 400 for tl=fa/fa-IR, every text, 2026-11-22) while tl=en
    and tl=ar return real MP3s. Kept only as a fallback for
    non-Persian text; returns MP3 bytes or None."""
    if len(text) > 180:
        text = text[:180]
    url = ("https://translate.google.com/translate_tts?ie=UTF-8"
           "&tl=%s&client=tw-ob&q=%s"
           % (urllib.parse.quote(lang), urllib.parse.quote(text)))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36")})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
        if data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
            return data
        return None
    except Exception:
        return None


# ======================================================================
# /dub/segment pipeline
# ======================================================================
def _f0_autocorr(window, rate=SEGMENT_SAMPLE_RATE):
    """Fundamental frequency of one 2048-sample window via autocorrelation.
    Returns F0 in Hz, or None when the frame is unvoiced/noisy.
    Lag range 53..228 samples = 70..300 Hz (adult speech)."""
    n = len(window)
    if n < 2048:
        return None
    x = [v - (sum(window) / n) for v in window]
    energy = sum(v * v for v in x)
    if energy <= 0:
        return None
    best_lag, best_corr = 0, 0.0
    for lag in range(rate // 300, rate // 70 + 1):
        c = 0.0
        for i in range(n - lag):
            c += x[i] * x[i + lag]
        c /= energy
        if c > best_corr:
            best_corr, best_lag = c, lag
    if best_corr < 0.5 or best_lag == 0:
        return None
    return rate / best_lag


def detect_gender(pcm):
    """Speaker gender from voice pitch (F0). Returns
    ("male"|"female"|"unknown", f0_hz or None).
    Median F0 over the loudest voiced frames; <155 Hz = male,
    >=155 Hz = female. Pure stdlib, ~0.2 s per chunk."""
    import array as _array
    n = len(pcm) // 2
    if n < 4096:
        return ("unknown", None)
    samples = _array.array("h")
    samples.frombytes(pcm[:n * 2])
    if samples.itemsize != 2:
        return ("unknown", None)
    # Loudest 2048-sample window: speech, not background music/noise.
    best_off, best_e = 0, -1.0
    step = max(1, (n - 2048) // 8)
    for off in range(0, n - 2048 + 1, step):
        e = sum(v * v for v in samples[off:off + 2048])
        if e > best_e:
            best_e, best_off = e, off
    f0s = []
    for off in (best_off, max(0, n // 2 - 1024), max(0, n - 2048)):
        f0 = _f0_autocorr(list(samples[off:off + 2048]))
        if f0:
            f0s.append(f0)
    if not f0s:
        return ("unknown", None)
    f0s.sort()
    f0 = f0s[len(f0s) // 2]
    return ("female" if f0 >= GENDER_F0_THRESHOLD else "male",
            round(f0, 1))


def dub_segment(pcm, t0=0.0, want_tts=True):
    """One ~2 s PCM chunk -> dub result dict (JSON-safe).
    want_tts=False skips TTS (subtitles-only path: the Persian text
    comes back as soon as translation is ready, ~2 s faster).
    Never raises: failures come back as error fields, never a fake
    "dubbing" state."""
    t_start = time.time()
    timings = {}
    if len(pcm) > SEGMENT_MAX_SECONDS * SEGMENT_SAMPLE_RATE * 2:
        return {"t0": t0, "error": "segment too long",
                "silent": False, "audio_b64": None}
    rms = _pcm_rms(pcm)
    if rms < SILENCE_RMS:
        return {"t0": t0, "silent": True, "source_text": "",
                "fa_text": "", "audio_b64": None,
                "ms": int((time.time() - t_start) * 1000)}
    # 0. Gender: pitch (F0) of the speaker -> matching Persian voice.
    # Runs before STT so the voice is known even if words fail.
    gender, f0_hz = detect_gender(pcm)
    voice = (TTS_VOICE_MALE if gender == "male" else TTS_VOICE_FEMALE)
    # 1. STT: English first (videos are mostly English speech);
    #    Persian fallback when nothing was recognised.
    t = time.time()
    text, conf = google_stt(pcm, "en-US")
    src_lang = "en"
    if text == "":
        text, conf = google_stt(pcm, "fa-IR")
        src_lang = "fa"
    timings["stt_ms"] = int((time.time() - t) * 1000)
    if text is None or text == "":
        return {"t0": t0, "silent": False, "source_text": "",
                "fa_text": "", "audio_b64": None,
                "gender": gender, "f0_hz": f0_hz, "voice": voice,
                "error": "no speech recognised" if text == ""
                         else "stt failed",
                "ms": int((time.time() - t_start) * 1000),
                "timings": timings}
    # 2. Translate to Persian (skip when already Persian).
    t = time.time()
    if src_lang == "fa":
        fa_text = text
    else:
        fa_text = google_translate(text, "en", "fa")
    timings["translate_ms"] = int((time.time() - t) * 1000)
    timings["translate_engine"] = _LAST_TRANSLATE_ENGINE
    if not fa_text:
        return {"t0": t0, "silent": False, "source_text": text,
                "fa_text": "", "audio_b64": None,
                "error": "translate failed",
                "ms": int((time.time() - t_start) * 1000),
                "timings": timings}
    if not want_tts:
        # Subtitles-only: text is the product; no TTS round-trip.
        return {"t0": t0, "silent": False, "source_text": text,
                "source_lang": src_lang, "confidence": conf,
                "fa_text": fa_text, "audio_b64": None,
                "audio_dur": 0.0, "tts_engine": None,
                "gender": gender, "f0_hz": f0_hz, "voice": voice,
                "translate_engine": _LAST_TRANSLATE_ENGINE,
                "error": None,
                "ms": int((time.time() - t_start) * 1000),
                "timings": timings}
    # 3. TTS: Edge neural Persian voice; Google TTS fallback;
    #    text-only when both fail (JS speaks it on-device if it can).
    t = time.time()
    audio_b64 = None
    audio_dur = 0.0
    tts_engine = None
    try:
        mp3, audio_dur = edge_tts(fa_text, voice=voice,
                                  rate=TTS_CATCHUP_RATE)
        audio_b64 = base64.b64encode(mp3).decode("ascii")
        tts_engine = "edge-" + voice
    except Exception:
        mp3 = google_tts_fallback(fa_text, "fa")
        if mp3:
            audio_b64 = base64.b64encode(mp3).decode("ascii")
            audio_dur = len(mp3) * 8 / 64000.0
            tts_engine = "google-tts-fa"
    timings["tts_ms"] = int((time.time() - t) * 1000)
    return {"t0": t0, "silent": False, "source_text": text,
            "source_lang": src_lang, "confidence": conf,
            "fa_text": fa_text, "audio_b64": audio_b64,
            "audio_dur": audio_dur, "tts_engine": tts_engine,
            "gender": gender, "f0_hz": f0_hz, "voice": voice,
            "translate_engine": _LAST_TRANSLATE_ENGINE,
            "error": None if audio_b64 else "tts failed (text only)",
            "ms": int((time.time() - t_start) * 1000),
            "timings": timings}


# ======================================================================
# v3.4 subtitles-only endpoints: /dub/stt (STT only, no translate,
# no TTS) and /dub/translate (last-resort server translate).
# The app flow is: capture PCM -> /dub/stt -> translate ON THE
# PHONE (gtx/MyMemory direct from the user's IP) -> subtitle overlay.
# Original audio is never touched: no ducking, no muting, no TTS.
# ======================================================================
def stt_only(pcm, t0=0.0):
    """One ~2 s PCM chunk -> {source_text, source_lang, ...}.
    STT only: no translate, no TTS. Never raises."""
    _stat("http_stt_calls")
    t_start = time.time()
    timings = {}
    if len(pcm) > SEGMENT_MAX_SECONDS * SEGMENT_SAMPLE_RATE * 2:
        return {"t0": t0, "error": "segment too long",
                "silent": False, "source_text": ""}
    rms = _pcm_rms(pcm)
    if rms < SILENCE_RMS:
        return {"t0": t0, "silent": True, "source_text": "",
                "source_lang": "", "confidence": 0.0,
                "ms": int((time.time() - t_start) * 1000)}
    t = time.time()
    text, conf = google_stt(pcm, "en-US")
    src_lang = "en"
    if text == "":
        text, conf = google_stt(pcm, "fa-IR")
        src_lang = "fa"
    timings["stt_ms"] = int((time.time() - t) * 1000)
    if text is None or text == "":
        return {"t0": t0, "silent": False, "source_text": "",
                "source_lang": src_lang, "confidence": 0.0,
                "error": "no speech recognised" if text == ""
                         else "stt failed",
                "ms": int((time.time() - t_start) * 1000),
                "timings": timings}
    return {"t0": t0, "silent": False, "source_text": text,
            "source_lang": src_lang, "confidence": conf,
            "error": None,
            "ms": int((time.time() - t_start) * 1000),
            "timings": timings}


def translate_only(text, src="en", dst="fa"):
    """Server-side translate (last resort for the phone). Uses the
    same chain as before; from Render it is usually 429 (shared
    egress IP burned) — the phone's direct gtx call is the real
    path. Returns {fa_text, engine, error}."""
    if not text or not text.strip():
        return {"fa_text": "", "engine": None, "error": "empty text"}
    if src == dst:
        return {"fa_text": text, "engine": "none",
                "error": None}
    out = google_translate(text, src, dst)
    if out:
        return {"fa_text": out, "engine": _LAST_TRANSLATE_ENGINE,
                "error": None}
    return {"fa_text": "", "engine": None,
            "error": "translate failed"}


# ======================================================================
# /dub/health — live probe of each free service (cached 60 s).
# ======================================================================
_HEALTH_CACHE = {"at": 0.0, "data": None}
_HEALTH_LOCK = threading.Lock()


def _probe(name, fn):
    t = time.time()
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - health must never raise
        ok, detail = False, str(exc)[:120]
    return {"ok": ok, "ms": int((time.time() - t) * 1000),
            "detail": detail}


def _probe_stt():
    # 0.1 s of silence is rejected by the recogniser, so probe with a
    # real 1 s tone-free PCM of near-silence: expect 200 + empty result.
    pcm = b"\x00\x00" * 16000
    url = ("https://www.google.com/speech-api/v2/recognize"
           "?output=json&lang=en-US&key=%s&client=chromium"
           % _CHROMIUM_KEY)
    req = urllib.request.Request(
        url, data=pcm,
        headers={"Content-Type": "audio/l16; rate=16000"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", "replace")
        return resp.status == 200, body[:80]


def _probe_translate():
    """Probe every translate candidate (forced, bypassing the
    down-cache) and report which host works from this machine."""
    results = {}
    for name in ("gtx-apis", "gtx-com", "mymemory"):
        out = _translate_one(
            name, "Hello", "en", "fa",
            TRANSLATE_CANDIDATE_TIMEOUTS[name], force=True)
        results[name] = bool(out)
    ok = any(results.values())
    detail = ",".join("%s:%s" % (k, "ok" if v else "fail")
                      for k, v in results.items())
    return ok, detail


def _probe_translate_one(name):
    out = _translate_one(
        name, "Hello", "en", "fa",
        TRANSLATE_CANDIDATE_TIMEOUTS[name], force=True)
    detail = out or _LAST_TRANSLATE_ERROR.get(name, "")
    return bool(out), detail


def _probe_tts():
    mp3, dur = edge_tts("سلام", timeout=20)
    return len(mp3) > 1000, "%d bytes, %.1fs" % (len(mp3), dur)


def dub_health():
    with _HEALTH_LOCK:
        if (_HEALTH_CACHE["data"] is not None
                and time.time() - _HEALTH_CACHE["at"] < 60):
            # v3.10 FIX: the probes are cached for 60 s (they are
            # slow), but the stats snapshot must NEVER be cached —
            # the whole point of the counters is live diagnosis.
            # Before this fix, a health call cached the stats and
            # every later call within 60 s returned the stale
            # snapshot, which made a working WS path look dead
            # (counters frozen at 0 while captions were flowing).
            data = dict(_HEALTH_CACHE["data"])
            data["stats"] = dict(_STATS)
            return data
        data = {
            "dub_version": DUB_VERSION,
            # v3.10: live counters — where the subtitle chain
            # actually breaks (see _STATS above).
            "stats": dict(_STATS),
            "services": {
                "stt_google_v2": _probe("stt", _probe_stt),
                "translate": _probe("translate", _probe_translate),
                "translate_gtx": _probe(
                    "translate_gtx",
                    lambda: _probe_translate_one("gtx-apis")),
                "translate_gtx_com": _probe(
                    "translate_gtx_com",
                    lambda: _probe_translate_one("gtx-com")),
                "translate_mymemory": _probe(
                    "translate_mymemory",
                    lambda: _probe_translate_one("mymemory")),
                "tts_edge_fa": _probe("tts", _probe_tts),
                # v3.7: boolean only — the key is never probed, never
                # logged, never returned. "ok" here means configured.
                "gemini_live": {
                    "ok": gemini_configured(),
                    "detail": ("configured (GEMINI_API_KEY set)"
                               if gemini_configured() else
                               "not configured — set GEMINI_API_KEY "
                               "in the Render dashboard"),
                },
            },
            "notes": ("Google translate_tts tl=fa returns HTTP 400 "
                      "(verified 2026-11-22); Edge neural TTS is the "
                      "Persian voice. STT uses the public Chromium demo "
                      "key, no private key anywhere."),
        }
        # Overall ok = STT + at least one translate host + TTS.
        # Individual candidate keys are diagnostic only.
        data["ok"] = (
            data["services"]["stt_google_v2"]["ok"]
            and data["services"]["translate"]["ok"]
            and data["services"]["tts_edge_fa"]["ok"])
        _HEALTH_CACHE["at"] = time.time()
        _HEALTH_CACHE["data"] = data
        return data


# ======================================================================
# Mounting: app.py calls handle_dub(self) from do_GET/do_POST before
# the reverse-proxy fallback. Returns True when the path was handled.
# ======================================================================
def handle_dub(handler):
    """handler is a BaseHTTPRequestHandler (proxy/app.py Handler).
    POST /dub/segment accepts EITHER raw s16le PCM (Content-Type
    audio/l16, t0 in query) OR JSON {"pcm_b64": ..., "t0": ...}.
    Returns True if this request was a /dub/* route."""
    parsed = urllib.parse.urlparse(handler.path)
    if parsed.path == "/dub/ws" and handler.command == "GET":
        # v3.6: WebSocket live subtitles (streaming transport).
        handle_dub_ws(handler)
        return True
    if parsed.path == "/dub/gemini-ws" and handler.command == "GET":
        # v3.7: Gemini Live Translate relay (server-side key).
        handle_dub_gemini_ws(handler)
        return True
    if parsed.path == "/dub/gemini-status" and handler.command == "GET":
        # v3.7: only a boolean — the key is NEVER returned, logged
        # or echoed anywhere.
        body = json.dumps({
            "configured": gemini_configured(),
            "model": _GEMINI_MODEL,
        }).encode("utf-8")
        _send_json(handler, 200, body)
        return True
    if parsed.path == "/dub/gemini-test" and handler.command == "GET":
        # v3.9: a real Gemini request from this server (see
        # gemini_test): raw HTTP status + error, never the key.
        body = json.dumps(gemini_test(),
                          ensure_ascii=False).encode("utf-8")
        _send_json(handler, 200, body)
        return True
    if parsed.path == "/dub/gemini-diag" and handler.command == "GET":
        # v3.10: live Gemini Live WS handshake + setup probe.
        body = json.dumps(gemini_diag(),
                          ensure_ascii=False).encode("utf-8")
        _send_json(handler, 200, body)
        return True
    if parsed.path == "/dub/health" and handler.command == "GET":
        body = json.dumps(dub_health(), ensure_ascii=False).encode("utf-8")
        _send_json(handler, 200, body)
        return True
    if parsed.path == "/dub/stt" and handler.command == "POST":
        # v3.4 subtitles-only: PCM -> transcript, no translate/TTS.
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4 * 1024 * 1024:
            _send_json(handler, 400, json.dumps(
                {"error": "empty or too large body"}).encode("utf-8"))
            return True
        raw = handler.rfile.read(length)
        query = urllib.parse.parse_qs(parsed.query)
        t0 = 0.0
        try:
            t0 = float((query.get("t0") or ["0"])[0])
        except ValueError:
            t0 = 0.0
        # v3.11: HTTP-fallback chunks carry the phone's session id
        # + a per-session sequence number + a unique chunk id, so
        # the phone log, the proxy log and the caption can be tied
        # together, and a retried chunk is recognisable (same id).
        # Fallback chunks (ChatGPT #5) are 4 s with 0.75 s overlap:
        # start_ms/end_ms carry the original audio time, and prev
        # carries the previous chunk's text as context. The keyless
        # Web Speech v2 STT endpoint takes no context parameter, so
        # prev is logged only (documented, not silently dropped).
        sid = (query.get("sid") or ["-"])[0][:32]
        seq = (query.get("seq") or ["-"])[0]
        cid = (query.get("id") or ["-"])[0][:48]
        start_ms = (query.get("start_ms") or ["-"])[0]
        end_ms = (query.get("end_ms") or ["-"])[0]
        prev = (query.get("prev") or [""])[0][:80]
        if start_ms != "-":
            try:
                t0 = float(start_ms) / 1000.0
            except ValueError:
                pass
        if seq != "-" or cid != "-":
            _stat("http_stt_fallback")
        print("dub: [stt sid=%s seq=%s id=%s] t0=%.1f "
              "start_ms=%s end_ms=%s bytes=%d prev=%.40s"
              % (sid, seq, cid, t0, start_ms, end_ms, len(raw),
                 prev), flush=True)
        ctype = (handler.headers.get("Content-Type") or "")
        if "application/json" in ctype:
            try:
                payload = json.loads(raw.decode("utf-8"))
                pcm = base64.b64decode(payload.get("pcm_b64") or "")
                t0 = float(payload.get("t0") or t0)
            except Exception:
                _send_json(handler, 400, json.dumps(
                    {"error": "bad json body"}).encode("utf-8"))
                return True
        else:
            pcm = raw
        result = stt_only(pcm, t0)
        _send_json(handler, 200, json.dumps(
            result, ensure_ascii=False).encode("utf-8"))
        return True
    if parsed.path == "/dub/translate" and handler.command == "POST":
        # v3.4: last-resort server translate for the phone (the
        # phone's direct gtx call is the real path; this chain is
        # usually 429 from Render's shared egress IP).
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            _send_json(handler, 400, json.dumps(
                {"error": "empty or too large body"}).encode("utf-8"))
            return True
        try:
            payload = json.loads(
                handler.rfile.read(length).decode("utf-8"))
        except Exception:
            _send_json(handler, 400, json.dumps(
                {"error": "bad json body"}).encode("utf-8"))
            return True
        result = translate_only(
            payload.get("text") or "",
            payload.get("src") or "en",
            payload.get("dst") or "fa")
        _send_json(handler, 200, json.dumps(
            result, ensure_ascii=False).encode("utf-8"))
        return True
    if parsed.path == "/dub/segment" and handler.command == "POST":
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4 * 1024 * 1024:
            _send_json(handler, 400, json.dumps(
                {"error": "empty or too large body"}).encode("utf-8"))
            return True
        raw = handler.rfile.read(length)
        query = urllib.parse.parse_qs(parsed.query)
        t0 = 0.0
        try:
            t0 = float((query.get("t0") or ["0"])[0])
        except ValueError:
            t0 = 0.0
        # ?tts=0 skips TTS (subtitles-only: text back sooner).
        want_tts = (query.get("tts") or ["1"])[0] != "0"
        ctype = (handler.headers.get("Content-Type") or "")
        if "application/json" in ctype:
            try:
                payload = json.loads(raw.decode("utf-8"))
                pcm = base64.b64decode(payload.get("pcm_b64") or "")
                t0 = float(payload.get("t0") or t0)
            except Exception:
                _send_json(handler, 400, json.dumps(
                    {"error": "bad json body"}).encode("utf-8"))
                return True
        else:
            pcm = raw
        result = dub_segment(pcm, t0, want_tts=want_tts)
        _send_json(handler, 200, json.dumps(
            result, ensure_ascii=False).encode("utf-8"))
        return True
    return False


# ======================================================================
# v3.6 WebSocket live subtitles: GET /dub/ws (upgrade).
#
# Why WebSocket: the HTTP POST /dub/stt path paid a fresh request
# (TLS already warm, but a full HTTP round-trip + headers) per 2 s
# chunk, and chunks were processed strictly in series. The WS keeps
# ONE persistent connection: the app streams 16 kHz s16le PCM as
# binary frames, the server STTs 1.0 s windows (0.5 s interim
# windows) in a 2-worker pool concurrently, and sends back JSON:
#   {"type":"final","t0":<video seconds>,"dur":1.0,
#    "text":...,"lang":"en"|"fa","conf":0.98}
#   {"type":"interim","t0":...,"dur":0.5,"text":...,"lang":...}
#   {"type":"error","error":"stt failed"}
# Translation still happens ON THE PHONE (Render's egress IP is
# burned for gtx/MyMemory) — the WS only does STT. Timestamps are
# precise: the client sends {"op":"mark","t0":<video.currentTime>}
# once at start; each window's t0 = mark + audio seconds consumed.
#
# True streaming STT (Google streaming API) is NOT free without a
# key, so this is the closest real thing: 1 s windows + 0.5 s
# interim windows over a persistent socket, concurrent STT. No
# subtitle is ever invented: silent windows are skipped, empty STT
# sends nothing. Audio Playback Capture (MediaProjection) is NOT
# used: it captures OTHER apps' audio and needs a system consent
# dialog the user never approved; our video plays inside our own
# WebView, so the app's JS AudioContext capture (no permission
# needed) is the correct path. Pure stdlib; no new dependency.
# ======================================================================
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WS_CHUNK_SAMPLES = 16000   # 1.0 s final window at 16 kHz
_WS_HALF_SAMPLES = 8000     # 0.5 s interim window
_WS_MAX_IDLE = 90           # close a dead connection after 90 s


def _ws_send_frame(sock, lock, payload, opcode=0x1):
    """One unmasked server text frame (opcode 0x1) or binary (0x2)."""
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    header = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    with lock:
        sock.sendall(bytes(header) + data)


def _ws_send_json(sock, lock, obj):
    _ws_send_frame(sock, lock, json.dumps(obj, ensure_ascii=False))


def _ws_recv_exact(sock, n):
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise EOFError("websocket closed")
        out += chunk
    return out


def _ws_recv_frame(sock):
    """One client message, reassembled across continuation frames.
    Returns (opcode, payload bytes). Client frames are always masked
    (RFC 6455).

    v3.10 ROOT FIX: a middlebox on the live path (Render/Cloudflare
    edge) splits any large WS frame into 4096-byte CONTINUATION
    frames (opcode 0x0). The old code returned only the first frame
    and silently dropped every continuation — so a 32,000-byte audio
    frame arrived as 4,096 bytes and 7/8 of the audio never reached
    Gemini/STT. Proved live 2026-11-22: one 32,000-byte frame raised
    ws_audio_bytes by exactly 4,096; a 1,000-byte frame by 1,000.
    Locally (loopback, no middlebox) frames were never split, which
    is why every local test passed and live subtitles starved. This
    is very likely the real reason live subtitles never worked on the
    phone: the relay only ever heard the first 0.128 s of each 1 s
    audio chunk. Reassembly happens here, so both WS handlers
    (/dub/ws and /dub/gemini-ws) and the Gemini reader are fixed at
    once. Control frames (ping/pong/close) are never fragmented in
    practice; a non-continuation frame arriving mid-message ends the
    reassembly and is dropped (documented, should not happen)."""
    h = _ws_recv_exact(sock, 2)
    op = h[0] & 0x0F
    fin = bool(h[0] & 0x80)
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _ws_recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _ws_recv_exact(sock, 8))[0]
    mask = _ws_recv_exact(sock, 4) if h[1] & 0x80 else None
    payload = _ws_recv_exact(sock, ln) if ln else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    while not fin:
        h2 = _ws_recv_exact(sock, 2)
        op2 = h2[0] & 0x0F
        fin = bool(h2[0] & 0x80)
        ln2 = h2[1] & 0x7F
        if ln2 == 126:
            ln2 = struct.unpack(">H", _ws_recv_exact(sock, 2))[0]
        elif ln2 == 127:
            ln2 = struct.unpack(">Q", _ws_recv_exact(sock, 8))[0]
        mask2 = _ws_recv_exact(sock, 4) if h2[1] & 0x80 else None
        part = _ws_recv_exact(sock, ln2) if ln2 else b""
        if mask2:
            part = bytes(b ^ mask2[i % 4] for i, b in enumerate(part))
        if op2 == 0x0:  # continuation: append to the message
            payload += part
        else:  # unexpected mid-message frame: stop, drop it
            break
    return op, payload


def handle_dub_ws(handler):
    """WebSocket upgrade + streaming STT loop. Runs inside the
    handler thread until the client closes or 90 s idle."""
    import concurrent.futures as _futures

    key = handler.headers.get("Sec-WebSocket-Key") or ""
    accept = base64.b64encode(hashlib.sha1(
        (key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
    # v3.11: shared session id + stage logs (ChatGPT #4).
    session = _new_session(handler)
    _dlog(session, "WS_CONNECT", "path=/dub/ws")
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    _stat("ws_connects")
    _dlog(session, "WS_HANDSHAKE_OK", "101")
    sock = handler.connection
    sock.settimeout(5)
    send_lock = threading.Lock()
    executor = _futures.ThreadPoolExecutor(max_workers=2)
    state = {"pcm": bytearray(), "samples": 0, "mark_t0": 0.0,
             "interim_sent": False, "last_final": "",
             "closed": False, "last_rx": time.time(),
             "audio_bytes": 0, "got_audio": False,
             "disconnect": "unknown"}

    def _send(obj):
        if state["closed"]:
            return
        try:
            _ws_send_json(sock, send_lock, obj)
        except Exception:
            state["closed"] = True

    def _run_stt(pcm, t0, dur, kind):
        """STT one window in a worker; send interim/final JSON."""
        # v3.9 diagnostics: the worker used to fail silently on live
        # Render (stt-done with text "" — indistinguishable from an
        # empty transcript, because google_stt returns None on error
        # and the debug printed ""). The exact error is now reported
        # so a dead STT path can be told apart from silence.
        _send({"type": "debug", "event": "worker-start", "kind": kind})
        _send({"type": "debug", "event": "stt-start", "kind": kind})
        if state["closed"]:
            return
        if _pcm_rms(pcm) < SILENCE_RMS:
            return  # silent: skip, never invent
        _stat("ws_stt_calls")
        try:
            text, conf = google_stt(pcm, "en-US")
        except Exception as exc:  # noqa: BLE001 - report, never crash
            _send({"type": "debug", "event": "stt-error", "kind": kind,
                   "error": "%s: %s" % (type(exc).__name__, exc)[:200]})
            return
        if text is None:
            _send({"type": "debug", "event": "stt-error", "kind": kind,
                   "error": "google_stt returned None (request failed)"})
            return
        _send({"type": "debug", "event": "stt-done", "kind": kind,
               "text": text})
        src = "en"
        if text == "":
            _stat("ws_stt_empty")
            _stat("ws_stt_calls")
            try:
                text, conf = google_stt(pcm, "fa-IR")
            except Exception as exc:  # noqa: BLE001
                _send({"type": "debug", "event": "stt-error",
                       "kind": kind,
                       "error": "%s: %s" % (type(exc).__name__, exc)[:200]})
                return
            src = "fa"
            if text is None:
                return
        if state["closed"] or text is None or text == "":
            return
        text = text.strip()
        if kind == "final":
            norm = " ".join(text.lower().split())
            if norm == state["last_final"]:
                return  # dedup: same words as the previous window
            state["last_final"] = norm
            # v3.9: server-side translate attempt — when the server
            # CAN translate, send Persian straight away (lang fa);
            # the phone then shows it without its own gtx call.
            # When it cannot (Render's burned IP), the English text
            # is sent as before and the phone translates.
            if src == "en":
                try:
                    fa = google_translate(text, "en", "fa")
                except Exception:
                    fa = None
                if fa:
                    _send({"type": kind, "t0": round(t0, 2), "dur": dur,
                           "text": fa, "lang": "fa",
                           "conf": round(conf, 2)})
                    _stat("subtitles_sent")
                    return
        _send({"type": kind, "t0": round(t0, 2), "dur": dur,
               "text": text, "lang": src, "conf": round(conf, 2)})
        _stat("subtitles_sent")

    def _feed(pcm_bytes):
        """Append PCM; fire interim at 0.5 s, final at 1.0 s."""
        if not state.get("fed"):
            # Diagnostic, sent synchronously from the handler thread
            # (like the pong): proves the binary path is reached.
            # The app ignores unknown message types.
            state["fed"] = True
            try:
                _ws_send_json(sock, send_lock,
                              {"type": "debug", "event": "feed-start"})
            except Exception:
                pass
        state["pcm"] += pcm_bytes
        state["samples"] = len(state["pcm"]) // 2
        base_t0 = state["mark_t0"]
        if (not state["interim_sent"]
                and state["samples"] >= _WS_HALF_SAMPLES):
            state["interim_sent"] = True
            half = bytes(state["pcm"][:_WS_HALF_SAMPLES * 2])
            executor.submit(_run_stt, half, base_t0, 0.5, "interim")
        while state["samples"] >= _WS_CHUNK_SAMPLES:
            chunk = bytes(state["pcm"][:_WS_CHUNK_SAMPLES * 2])
            del state["pcm"][:_WS_CHUNK_SAMPLES * 2]
            state["samples"] = len(state["pcm"]) // 2
            win_t0 = base_t0
            state["mark_t0"] = base_t0 + 1.0
            state["interim_sent"] = False
            executor.submit(_run_stt, chunk, win_t0, 1.0, "final")

    try:
        while not state["closed"]:
            try:
                op, payload = _ws_recv_frame(sock)
            except socket.timeout:
                if time.time() - state["last_rx"] > _WS_MAX_IDLE:
                    state["disconnect"] = "idle-90s"
                    break
                # keep-alive ping every ~20 s of silence
                try:
                    _ws_send_frame(sock, send_lock, b"", opcode=0x9)
                except Exception:
                    state["disconnect"] = "ping-failed"
                    break
                continue
            except (EOFError, OSError):
                state["disconnect"] = "eof"
                break
            state["last_rx"] = time.time()
            if op == 0x8:  # close
                try:
                    code = struct.unpack(">H", payload[:2])[0] \
                        if len(payload) >= 2 else 0
                except Exception:
                    code = 0
                state["disconnect"] = "client-close code=%d" % code
                try:
                    _ws_send_frame(sock, send_lock, payload, opcode=0x8)
                except Exception:
                    pass
                break
            if op == 0x9:  # ping -> pong
                try:
                    _ws_send_frame(sock, send_lock, payload, opcode=0xA)
                except Exception:
                    break
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x1:  # text control: {"op":"mark","t0":12.3}
                try:
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("op") == "mark":
                        state["mark_t0"] = float(msg.get("t0") or 0.0)
                        state["pcm"] = bytearray()
                        state["samples"] = 0
                        state["interim_sent"] = False
                except Exception:
                    pass
                continue
            if op == 0x2 and payload:  # binary PCM s16le 16 kHz mono
                _stat("ws_audio_packets")
                _stat("ws_audio_bytes", len(payload))
                # v3.11 stage logs (ChatGPT #4).
                state["audio_bytes"] += len(payload)
                if not state["got_audio"]:
                    state["got_audio"] = True
                    _dlog(session, "WS_RECEIVED_AUDIO",
                          "first=%d bytes" % len(payload))
                elif state["audio_bytes"] // 200000 > (
                        state["audio_bytes"] - len(payload)) // 200000:
                    _dlog(session, "WS_AUDIO_BYTES",
                          "total=%d" % state["audio_bytes"])
                # v3.6.1: _feed MUST be inside try/except — it used
                # to sit outside the loop's try, so any exception
                # here (e.g. a worker-submit failure) killed the
                # handler thread silently: the client saw the 101
                # and the pong, then total silence, no STT, no
                # keep-alive pings (proved live 2026-11-22). The
                # error is now reported to the client instead.
                try:
                    _feed(payload)
                except Exception as exc:  # noqa: BLE001 - report it
                    try:
                        _ws_send_json(sock, send_lock, {
                            "type": "error",
                            "error": "feed failed: %s" % exc})
                    except Exception:
                        pass
                    break
    finally:
        state["closed"] = True
        _stat("ws_disconnects")
        _dlog(session, "WS_DISCONNECTED",
              "reason=%s audio_bytes=%d" % (
                  state.get("disconnect", "unknown"),
                  state.get("audio_bytes", 0)))
        executor.shutdown(wait=False, cancel_futures=True)
        try:
            sock.close()
        except Exception:
            pass
        handler.close_connection = True


# ======================================================================
# v3.7 Gemini Live Translate relay: GET /dub/gemini-ws (upgrade).
#
# SECURITY (user order 2026-11-22: "save the key on the server,
# think of the security"): the user's Gemini API key lives ONLY as
# the GEMINI_API_KEY environment variable on the Render service
# (render.yaml declares it sync:false, so it is set in the Render
# dashboard and never lands in git). The proxy reads it from the
# environment at runtime and uses it ONLY for this Gemini Live
# subtitle translation. The key is NEVER baked into the APK, NEVER
# sent to any client, NEVER written to any file, NEVER logged, and
# NEVER returned by any endpoint — /dub/gemini-status and
# /dub/health report only the boolean "configured". The app does
# NOT store the key and has NO key field. The handshake request
# carries the key in its query string, so the URL is never logged
# and handshake failures are raised with safe_error=True (no
# response body in the exception).
#
# METHOD (verified live 2026-11-22 in v3.6 research, same as Namia
# / LivDub): capture the video's own audio -> stream 16 kHz PCM16
# over WebSocket to Google Gemini Live Translate
# (gemini-3.5-live-translate-preview, translationConfig target fa)
# -> simultaneous Persian captions back (~3 s behind speech).
# The proxy bridges the app's WS to Gemini's WS; the app falls
# back to /dub/ws (or HTTP /dub/stt) whenever this bridge is
# unavailable or unconfigured, so subtitles still work with NO
# key at all — the Gemini path is an upgrade, not a dependency.
# ======================================================================
_GEMINI_HOST = "generativelanguage.googleapis.com"
_GEMINI_MODEL = "models/gemini-3.5-live-translate-preview"
_GEMINI_WS_PATH = ("/ws/google.ai.generativelanguage.v1beta."
                   "GenerativeService.BidiGenerateContent")
_GEMINI_SETUP = {
    "setup": {
        "model": _GEMINI_MODEL,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "translationConfig": {
                "targetLanguageCode": "fa",
                "echoTargetLanguage": True,
            },
        },
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    },
}
_GEMINI_MAX_IDLE = 90  # close a dead connection after 90 s


def gemini_key():
    """Read the key from the environment only. The value is never
    logged, never returned, never stored anywhere else."""
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def gemini_configured():
    return bool(gemini_key())


def gemini_test():
    """v3.9: a REAL Gemini request from this server — because
    "configured: true" proves nothing (it only means the env var
    is set). POSTs a tiny generateContent call with the key as the
    documented `x-goog-api-key` header and returns the raw HTTP
    status + error code/message. The key is NEVER returned, logged
    or echoed. Returns a JSON-safe dict."""
    if not gemini_configured():
        return {"ok": False, "http_status": 0,
                "error": "not_configured",
                "detail": "GEMINI_API_KEY is not set on the server"}
    url = ("https://generativelanguage.googleapis.com/v1beta/"
           "models/gemini-3.5-flash-lite:generateContent")
    body = json.dumps({
        "contents": [{"parts": [{"text": "Say OK"}]}],
        "generationConfig": {"maxOutputTokens": 5},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": gemini_key()})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        cands = data.get("candidates") or []
        text = ""
        try:
            text = cands[0]["content"]["parts"][0].get("text", "")
        except Exception:
            text = ""
        return {"ok": True, "http_status": resp.status,
                "error": None, "reply": text.strip()[:40]}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
            obj = json.loads(raw)
            err = obj.get("error") or {}
            detail = "%s: %s" % (err.get("status", ""),
                                 err.get("message", ""))[:240]
        except Exception:
            detail = raw[:240] if 'raw' in dir() else ""
        return {"ok": False, "http_status": exc.code,
                "error": "http_%d" % exc.code, "detail": detail}
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"ok": False, "http_status": 0,
                "error": "%s: %s" % (type(exc).__name__, exc)[:200],
                "detail": ""}


def gemini_diag():
    """v3.10: prove the Gemini Live relay works from THIS server,
    without a phone. Opens the Gemini WS, sends the setup, waits
    up to 12 s for setupComplete. Returns the stage reached and a
    safe error string — never the key, never response bodies."""
    if not gemini_configured():
        return {"ok": False, "stage": "not_configured",
                "error": "GEMINI_API_KEY is not set on the server"}
    gsock = None
    try:
        path = (_GEMINI_WS_PATH + "?key="
                + urllib.parse.quote(gemini_key(), safe=""))
        gsock = _ws_connect(_GEMINI_HOST, path,
                            {"x-goog-api-key": gemini_key()},
                            timeout=15, safe_error=True)
        gsock.settimeout(12)
        _ws_send_text(gsock, json.dumps(_GEMINI_SETUP))
        end = time.time() + 12
        while time.time() < end:
            try:
                op, payload = _ws_recv_frame(gsock)
            except socket.timeout:
                continue
            if op in (0x1, 0x2) and payload:
                try:
                    m = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(m, dict) and "setupComplete" in m:
                    return {"ok": True, "stage": "setup_complete",
                            "error": None}
                if isinstance(m, dict) and "error" in m:
                    err = m.get("error") or {}
                    detail = err.get("message", err) \
                        if isinstance(err, dict) else str(err)
                    return {"ok": False, "stage": "gemini_error",
                            "error": "%s" % detail}
        return {"ok": False, "stage": "setup_timeout",
                "error": "no setupComplete within 12 s"}
    except Exception as exc:
        return {"ok": False, "stage": "handshake_failed",
                "error": "%s" % type(exc).__name__}
    finally:
        if gsock is not None:
            try:
                gsock.close()
            except Exception:
                pass


def _gemini_parse_out(text):
    """Extract (text, turn_complete, lang) from one Gemini Live JSON
    message. Primary: serverContent.outputTranscription (Persian,
    lang "fa"). Fallback: serverContent.inputTranscription (the
    source-language transcript, lang "en") — the phone then
    translates it on-phone instead of showing nothing. Returns
    (None, False, "en") when the message carries no transcript.
    v3.10: the old version only read outputTranscription, so a
    session that only ever produced inputTranscription looked
    completely dead from the outside."""
    try:
        msg = json.loads(text)
    except Exception:
        return None, False, "en"
    if not isinstance(msg, dict):
        return None, False, "en"
    sc = msg.get("serverContent") or {}
    if not isinstance(sc, dict):
        return None, False, "en"
    done = bool(sc.get("turnComplete"))
    ot = sc.get("outputTranscription") or {}
    fa = ot.get("text") if isinstance(ot, dict) else None
    if fa:
        return str(fa), done, "fa"
    it = sc.get("inputTranscription") or {}
    src = it.get("text") if isinstance(it, dict) else None
    if src:
        return str(src), done, "en"
    return None, done, "en"


def handle_dub_gemini_ws(handler):
    """Bridge app WS <-> Gemini Live WS. Runs inside the handler
    thread until either side closes or 90 s idle."""
    key = handler.headers.get("Sec-WebSocket-Key") or ""
    accept = base64.b64encode(hashlib.sha1(
        (key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
    # v3.11: shared session id + stage logs (ChatGPT #4).
    session = _new_session(handler)
    _dlog(session, "WS_CONNECT", "path=/dub/gemini-ws")
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    _stat("ws_connects")
    _dlog(session, "WS_HANDSHAKE_OK", "101")
    sock = handler.connection
    sock.settimeout(5)
    send_lock = threading.Lock()
    gsend_lock = threading.Lock()  # serialises writes to the Gemini socket
    state = {"closed": False, "last_rx": time.time(),
             "mark_t0": 0.0, "last_final": "", "last_sent": "",
             "audio_bytes": 0, "got_audio": False,
             "gemini_bytes": 0, "disconnect": "unknown"}

    def _send(obj):
        if state["closed"]:
            return
        try:
            _ws_send_json(sock, send_lock, obj)
        except Exception:
            state["closed"] = True

    if not gemini_configured():
        # No key on the server yet — tell the app so it falls back
        # to the no-key path instead of hanging.
        _stat("ws_disconnects")
        _dlog(session, "WS_DISCONNECTED",
              "reason=gemini_not_configured")
        _send({"type": "error", "error": "gemini_not_configured"})
        try:
            _ws_send_frame(sock, send_lock, b"", opcode=0x8)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        handler.close_connection = True
        return

    # --- open the Gemini Live session (key in memory only) ---
    gsock = None
    try:
        # v3.9: the key is sent BOTH as the documented
        # `x-goog-api-key` header AND as ?key= (the v3.6-verified
        # form). The URL is never logged; the header is never
        # logged either. safe_error=True keeps any server error
        # body out of the exception.
        path = (_GEMINI_WS_PATH + "?key="
                + urllib.parse.quote(gemini_key(), safe=""))
        gsock = _ws_connect(_GEMINI_HOST, path,
                            {"x-goog-api-key": gemini_key()},
                            timeout=15, safe_error=True)
        gsock.settimeout(30)
        _ws_send_text(gsock, json.dumps(_GEMINI_SETUP))
        # Wait for setupComplete (<= 10 s).
        got_setup = False
        end = time.time() + 10
        gsock.settimeout(max(1.0, end - time.time()))
        while time.time() < end:
            try:
                op, payload = _ws_recv_frame(gsock)
            except socket.timeout:
                continue
            if op in (0x1, 0x2) and payload:
                try:
                    m = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(m, dict) and "setupComplete" in m:
                    got_setup = True
                    break
                if isinstance(m, dict) and "error" in m:
                    break
        gsock.settimeout(30)
        if not got_setup:
            raise RuntimeError("gemini setup not completed")
        _stat("gemini_sessions")
        _dlog(session, "GEMINI_CONNECTED", "setup_complete")
        # v3.17 PRE-WARM: tell the app the Gemini session is
        # genuinely ready (setupComplete received) — the phone
        # shows SESSION_READY only on this message, never on the
        # bare socket open. A pre-warmed session sends no audio
        # until the user taps, so the tap only needs to start
        # the audio flow.
        _send({"type": "ready"})
    except Exception:
        # Generic error only — never the exception text (it could
        # carry server response bytes) and never the key.
        _stat("gemini_errors")
        _stat("ws_disconnects")
        _dlog(session, "WS_DISCONNECTED",
              "reason=gemini-setup-failed")
        _send({"type": "error", "error": "gemini_error"})
        try:
            _ws_send_frame(sock, send_lock, b"", opcode=0x8)
        except Exception:
            pass
        if gsock is not None:
            try:
                gsock.close()
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass
        handler.close_connection = True
        return

    def _gemini_reader():
        """Gemini -> app: Persian captions out of outputTranscription."""
        # v3.9: do NOT kill the session after 30 s of Gemini
        # silence (the old code broke on the first 30 s socket
        # timeout and sent gemini_closed, even though Gemini can
        # legitimately take longer on a slow link). Wait up to 90 s
        # without any Gemini message, like the WS path.
        last_msg = time.time()
        try:
            while not state["closed"]:
                try:
                    op, payload = _ws_recv_frame(gsock)
                except socket.timeout:
                    if time.time() - last_msg > _GEMINI_MAX_IDLE:
                        break
                    continue
                except (EOFError, OSError):
                    break
                if state["closed"]:
                    break
                last_msg = time.time()
                if op == 0x8:  # close
                    break
                if op == 0x9:  # ping -> pong
                    try:
                        # _ws_send_frame locks internally; do NOT
                        # wrap it in gsend_lock (deadlock).
                        _ws_send_frame(gsock, gsend_lock, b"",
                                       opcode=0xA)
                    except Exception:
                        break
                    continue
                if op == 0xA:  # pong
                    continue
                if op not in (0x1, 0x2) or not payload:
                    continue
                try:
                    text = payload.decode("utf-8")
                except Exception:
                    continue
                _stat("gemini_messages")
                # v3.17 GOAWAY (round-2 #2): Gemini Live sessions
                # live ~10 min; before closing, the server sends
                # {"goAway": {"timeLeft": "60s"}}. Forward it so
                # the phone can re-warm instead of discovering a
                # dead session at tap time. No transcript here —
                # check before the _gemini_parse_out skip.
                try:
                    _gm = json.loads(text)
                    if isinstance(_gm, dict) and "goAway" in _gm:
                        _ga = _gm.get("goAway") or {}
                        _tl = _ga.get("timeLeft", "?") \
                            if isinstance(_ga, dict) else "?"
                        _dlog(session, "GEMINI_GOAWAY",
                              "timeLeft=%s" % _tl)
                        _send({"type": "goaway",
                               "timeLeft": str(_tl)})
                        continue
                except Exception:
                    pass
                fa, done, lang = _gemini_parse_out(text)
                if not fa:
                    continue
                fa = fa.strip()
                if not fa or fa == state["last_sent"]:
                    continue  # dedup: same line twice
                state["last_sent"] = fa
                if lang == "fa":
                    _stat("gemini_transcripts")
                else:
                    # Source-language transcript only: forward it
                    # as lang "en" so the phone translates it
                    # on-phone instead of showing nothing.
                    _stat("gemini_input_transcripts")
                # v3.11 stage logs (ChatGPT #4).
                _dlog(session, "GEMINI_TRANSCRIPT_RECEIVED",
                      "lang=%s done=%s text=%.40s"
                      % (lang, done, fa))
                if done:
                    norm = " ".join(fa.lower().split())
                    if norm == state["last_final"]:
                        continue
                    state["last_final"] = norm
                    _send({"type": "final", "t0": round(
                        state["mark_t0"], 2), "text": fa, "lang": lang})
                    _stat("subtitles_sent")
                else:
                    _send({"type": "interim", "t0": round(
                        state["mark_t0"], 2), "text": fa, "lang": lang})
                    _stat("subtitles_sent")
                _dlog(session, "WS_TRANSCRIPT_SENT",
                      "type=%s lang=%s"
                      % ("final" if done else "interim", lang))
        except Exception:
            pass
        finally:
            if not state["closed"]:
                _send({"type": "error", "error": "gemini_closed"})
            state["closed"] = True
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    reader = threading.Thread(target=_gemini_reader, daemon=True)
    reader.start()
    try:
        while not state["closed"]:
            try:
                op, payload = _ws_recv_frame(sock)
            except socket.timeout:
                if time.time() - state["last_rx"] > _GEMINI_MAX_IDLE:
                    state["disconnect"] = "idle-90s"
                    break
                try:
                    _ws_send_frame(sock, send_lock, b"", opcode=0x9)
                except Exception:
                    state["disconnect"] = "ping-failed"
                    break
                continue
            except (EOFError, OSError):
                state["disconnect"] = "eof"
                break
            state["last_rx"] = time.time()
            if op == 0x8:  # close
                try:
                    code = struct.unpack(">H", payload[:2])[0] \
                        if len(payload) >= 2 else 0
                except Exception:
                    code = 0
                state["disconnect"] = "client-close code=%d" % code
                try:
                    _ws_send_frame(sock, send_lock, payload, opcode=0x8)
                except Exception:
                    pass
                break
            if op == 0x9:  # ping -> pong
                try:
                    _ws_send_frame(sock, send_lock, payload, opcode=0xA)
                except Exception:
                    break
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x1 and payload:  # text control: mark t0
                try:
                    msg = json.loads(payload.decode("utf-8"))
                    if msg.get("op") == "mark":
                        state["mark_t0"] = float(msg.get("t0") or 0.0)
                except Exception:
                    pass
                continue
            if op == 0x2 and payload:  # binary PCM s16le 16 kHz mono
                _stat("ws_audio_packets")
                _stat("ws_audio_bytes", len(payload))
                # v3.11 stage logs (ChatGPT #4).
                state["audio_bytes"] += len(payload)
                if not state["got_audio"]:
                    state["got_audio"] = True
                    _dlog(session, "WS_RECEIVED_AUDIO",
                          "first=%d bytes" % len(payload))
                try:
                    # _ws_send_text does a single sendall with no
                    # internal lock, so serialise it here.
                    with gsend_lock:
                        _ws_send_text(gsock, json.dumps({
                            "realtimeInput": {"audio": {
                                "mimeType": "audio/pcm;rate=16000",
                                "data": base64.b64encode(
                                    payload).decode("ascii"),
                            }},
                        }))
                    _stat("gemini_audio_bytes", len(payload))
                    state["gemini_bytes"] += len(payload)
                    if state["gemini_bytes"] // 200000 > (
                            state["gemini_bytes"]
                            - len(payload)) // 200000:
                        _dlog(session, "GEMINI_AUDIO_SENT",
                              "total=%d" % state["gemini_bytes"])
                except Exception:
                    state["disconnect"] = "gemini-send-failed"
                    break
    finally:
        state["closed"] = True
        _stat("ws_disconnects")
        _dlog(session, "WS_DISCONNECTED",
              "reason=%s audio_bytes=%d gemini_bytes=%d" % (
                  state.get("disconnect", "unknown"),
                  state.get("audio_bytes", 0),
                  state.get("gemini_bytes", 0)))
        try:
            gsock.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        handler.close_connection = True


def _send_json(handler, code, body):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


# ======================================================================
# Standalone smoke test: python3 dub.py [port]
# ======================================================================
if __name__ == "__main__":
    import sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print("dub:", fmt % args, flush=True)

        def do_GET(self):
            if not handle_dub(self):
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if not handle_dub(self):
                self.send_response(404)
                self.end_headers()

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10001
    print("dub smoke server on port %d" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), _H).serve_forever()
