"""v3.19: transcode-once HLS light renditions for xfree videos.

WHY: the site serves ONE rendition, ~4.27 Mbps. On a line below
~4.3 Mbps live playback is physically impossible without a lighter
rendition. This module transcodes each video ONCE (first request)
into three HLS renditions (240/360/480p) + a progressive 360p MP4,
caches them on the proxy's ephemeral disk (LRU, 700 MB cap), and
serves them from cache afterwards. No durable store exists without
a card (R2/Bunny are out), and no video bytes leave OUR proxy.

MEASURED (VM, ffmpeg 8.1.2, real 337 s video, 720x1152, 4.27 Mbps):
one ffmpeg invocation, 3 HLS renditions + 360p MP4, veryfast,
threads 1/output: 156 s wall = 2.16x real-time; 85 segments of 4 s
per rendition; 80 MB total on disk; 360p MP4 10.5 MB. First 4 s
segment is ready after ~2 s of transcode. Egress per full play:
240p ~10 MB, 360p ~13 MB, 480p ~46 MB vs 177 MB original.

SUBTITLE SAFETY (the reason the player swap is safe): the app's
DubBridge reads video.currentSrc for the subtitle engine. An MSE
(hls.js) attach would make currentSrc a blob: URL and break
subtitles. The injected player script therefore overrides
video.currentSrc (own getter) to keep returning the ORIGINAL mp4
URL while the element plays HLS; the engine keeps decoding the
original file's audio (the proven v3.9 path) on the same timeline.
Fallback chain: HLS -> light 360p MP4 -> original mp4. The original
mp4 always plays if anything else fails.

LIMITS (honest): first play of a video waits for the first segment
(~2-5 s on this VM; slower on Railway's smaller container). A job
queued behind another transcode falls back to the original mp4 for
that play (the light version is ready for the NEXT play). If the
Railway container OOMs on ffmpeg (exit 137), the job fails and the
original mp4 plays — the /lite-status endpoint + Railway logs show
it. The cache is ephemeral: a redeploy wipes it.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
HLS_CACHE_DIR = os.environ.get("HLS_CACHE_DIR", "/tmp/hls-cache")
HLS_CACHE_CAP = 700 * 1024 * 1024  # 700 MB LRU cap (Railway free
# disk is ~1 GB per deployment; keep a safety margin).
HLS_MASTER_WAIT_S = 8  # block a master.m3u8 request this long for
# the first segment (under hls.js's 10 s manifest timeout, so a
# retry gets a clean 503 instead of an aborted long poll).
HLS_FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
HLS_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

# (name, box_w, box_h, v_kbps, maxrate_kbps, bufsize_kbps, a_kbps,
#  bandwidth for the master playlist = maxrate + audio + margin)
RENDITIONS = [
    ("240", 426, 240, 300, 400, 800, 64, 464000),
    ("360", 640, 360, 600, 750, 1200, 64, 814000),
    ("480", 854, 480, 1000, 1200, 2000, 96, 1296000),
]

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
_CDN_RE = re.compile(
    r"^https://cdn\.xfree\.com/xfree-prod/[A-Za-z0-9]/[A-Za-z0-9]/"
    r"[A-Za-z0-9]/([A-Za-z0-9][A-Za-z0-9_-]{7,63})/full\.mp4$")


def valid_name(name):
    return bool(name) and bool(_NAME_RE.match(name))


def source_url(name):
    """The site's video URL is deterministic from the media name
    (not signed, not time-limited), so the cache key is stable."""
    return ("https://cdn.xfree.com/xfree-prod/%s/%s/%s/%s/full.mp4"
            % (name[0], name[1], name[2], name))


def ffmpeg_available():
    return shutil.which(HLS_FFMPEG) is not None


def _fit_dims(src_w, src_h, box_w, box_h):
    """What ffmpeg's scale=<box>:force_original_aspect_ratio=
    decrease:force_divisible_by=2 produces: fit inside the box,
    then round UP to an even number (verified: 720x1152 ->
    300x480, 226x360, 150x240)."""
    if not src_w or not src_h:
        return None
    f = min(box_w / float(src_w), box_h / float(src_h))
    w = int(src_w * f)
    h = int(src_h * f)
    if w % 2:
        w += 1
    if h % 2:
        h += 1
    return (w, h)


def _probe(src):
    """One ffprobe call for video dims + audio presence (reads
    only the header). Returns (width, height, has_audio); on any
    failure (None, None, True) so the caller falls back to
    no-RESOLUTION + assume audio."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-user_agent", HLS_UA,
             "-show_entries", "stream=codec_type,width,height",
             "-of", "csv=p=0", src],
            capture_output=True, text=True, timeout=25).stdout
        w = h = None
        has_audio = False
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if not parts:
                continue
            if parts[0] == "video":
                nums = [p for p in parts[1:] if p.isdigit()]
                if len(nums) >= 2:
                    w, h = int(nums[0]), int(nums[1])
            elif parts[0] == "audio":
                has_audio = True
        return (w, h, has_audio)
    except Exception:
        return (None, None, True)


# ----------------------------------------------------------------------
# Job store (one ffmpeg at a time; FIFO queue)
# ----------------------------------------------------------------------
class _Job(object):
    __slots__ = ("name", "src", "dir", "state", "error", "dims",
                 "has_audio", "created", "last_used", "proc")

    def __init__(self, name):
        self.name = name
        self.src = source_url(name)
        self.dir = os.path.join(HLS_CACHE_DIR, name)
        self.state = "queued"  # queued | running | done | failed
        self.error = ""
        self.dims = {}  # rend name -> (w, h)
        self.has_audio = True
        self.created = time.time()
        self.last_used = time.time()
        self.proc = None


_JOBS = {}
_JOBS_LOCK = threading.Lock()
_QUEUE = []
_QUEUE_LOCK = threading.Lock()
_WORKER_STARTED = False


def _job_dir_size(path):
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _cache_total_bytes():
    total = 0
    try:
        for name in os.listdir(HLS_CACHE_DIR):
            total += _job_dir_size(os.path.join(HLS_CACHE_DIR, name))
    except OSError:
        pass
    return total


def _evict_lru(except_name=None):
    """Delete oldest DONE/FAILED jobs until under the cap. Never
    evict a queued/running job."""
    try:
        total = _cache_total_bytes()
        if total <= HLS_CACHE_CAP:
            return
        with _JOBS_LOCK:
            cands = [(j.last_used, j.name) for j in _JOBS.values()
                     if j.state in ("done", "failed")
                     and j.name != except_name]
        for _ts, name in sorted(cands):
            if total <= HLS_CACHE_CAP:
                break
            path = os.path.join(HLS_CACHE_DIR, name)
            size = _job_dir_size(path)
            try:
                shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
            with _JOBS_LOCK:
                _JOBS.pop(name, None)
            total -= size
    except Exception:
        pass


def _ffmpeg_cmd(job):
    """One decode -> 3 HLS renditions + 360p faststart MP4 (the
    filtergraph benchmarked at 2.16x real-time on the VM). threads 1
    per output: the container is small; 4 outputs already oversub-
    scribe one vCPU. -nostdin so ffmpeg never blocks on stdin."""
    d = job.dir
    audio_maps = []
    if job.has_audio:
        audio_maps = ["-map", "0:a:0"]
    cmd = [HLS_FFMPEG, "-hide_banner", "-loglevel", "error",
           "-nostdin", "-y", "-user_agent", HLS_UA,
           "-i", job.src,
           "-filter_complex",
           "[0:v]scale=854:480:force_original_aspect_ratio=decrease:"
           "force_divisible_by=2,split=3[v480pre][v360pre][v240pre];"
           "[v480pre]copy[v480];"
           "[v360pre]scale=640:360:force_original_aspect_ratio=decrease:"
           "force_divisible_by=2,split=2[v360h][v360m];"
           "[v240pre]scale=426:240:force_original_aspect_ratio=decrease:"
           "force_divisible_by=2[v240]",
           "-max_muxing_queue_size", "1024"]
    # 480p HLS
    cmd += ["-map", "[v480]"] + audio_maps + [
        "-c:v:0", "libx264", "-preset", "veryfast", "-threads", "1",
        "-b:v:0", "1000k", "-maxrate:v:0", "1200k",
        "-bufsize:v:0", "2000k"]
    if job.has_audio:
        cmd += ["-c:a:0", "aac", "-b:a:0", "96k"]
    cmd += ["-g", "96", "-keyint_min", "96", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-hls_time", "4", "-hls_playlist_type", "event",
            "-hls_segment_filename", os.path.join(d, "480_%03d.ts"),
            os.path.join(d, "480.m3u8")]
    # 360p HLS
    cmd += ["-map", "[v360h]"] + audio_maps + [
        "-c:v:1", "libx264", "-preset", "veryfast", "-threads", "1",
        "-b:v:1", "600k", "-maxrate:v:1", "750k",
        "-bufsize:v:1", "1200k"]
    if job.has_audio:
        cmd += ["-c:a:1", "aac", "-b:a:1", "64k"]
    cmd += ["-g", "96", "-keyint_min", "96", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-hls_time", "4", "-hls_playlist_type", "event",
            "-hls_segment_filename", os.path.join(d, "360_%03d.ts"),
            os.path.join(d, "360.m3u8")]
    # 240p HLS
    cmd += ["-map", "[v240]"] + audio_maps + [
        "-c:v:2", "libx264", "-preset", "veryfast", "-threads", "1",
        "-b:v:2", "300k", "-maxrate:v:2", "400k",
        "-bufsize:v:2", "800k"]
    if job.has_audio:
        cmd += ["-c:a:2", "aac", "-b:a:2", "64k"]
    cmd += ["-g", "96", "-keyint_min", "96", "-sc_threshold", "0",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-hls_time", "4", "-hls_playlist_type", "event",
            "-hls_segment_filename", os.path.join(d, "240_%03d.ts"),
            os.path.join(d, "240.m3u8")]
    # 360p progressive MP4 (faststart; only complete when the job
    # finishes, so it is the repeat-play / old-WebView fallback).
    cmd += ["-map", "[v360m]"] + audio_maps + [
        "-c:v:3", "libx264", "-preset", "veryfast", "-threads", "1",
        "-b:v:3", "600k", "-maxrate:v:3", "750k",
        "-bufsize:v:3", "1200k"]
    if job.has_audio:
        cmd += ["-c:a:3", "aac", "-b:a:3", "64k"]
    cmd += ["-movflags", "+faststart", os.path.join(d, "360.mp4")]
    return cmd


def _run_job(job):
    """Worker body: probe, transcode, mark done/failed. Runs in the
    single worker thread."""
    job.state = "running"
    try:
        os.makedirs(job.dir, exist_ok=True)
        w, h, has_audio = _probe(job.src)
        job.has_audio = has_audio
        for (rname, bw, bh, _vb, _mr, _bs, _ab, _bw2) in RENDITIONS:
            dims = _fit_dims(w, h, bw, bh)
            if dims:
                job.dims[rname] = dims
        cmd = _ffmpeg_cmd(job)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        job.proc = proc
        _out, err = proc.communicate()
        job.proc = None
        if proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "ignore")[-500:]
            raise RuntimeError("ffmpeg exit %d: %s"
                               % (proc.returncode, tail))
        # The 360.mp4 must exist and be non-trivial, else the job
        # produced nothing usable.
        mp4 = os.path.join(job.dir, "360.mp4")
        if not os.path.exists(mp4) or os.path.getsize(mp4) < 10000:
            raise RuntimeError("360.mp4 missing after transcode")
        job.state = "done"
    except Exception as exc:
        job.state = "failed"
        job.error = str(exc)[:300]
    _evict_lru(except_name=job.name)


def _worker():
    while True:
        job = None
        with _QUEUE_LOCK:
            if _QUEUE:
                job = _QUEUE.pop(0)
        if job is None:
            time.sleep(0.5)
            continue
        _run_job(job)


def _ensure_worker():
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        t = threading.Thread(target=_worker, daemon=True,
                             name="hls-worker")
        t.start()


def ensure_job(name):
    """Return the job for this video, creating + queueing it if
    new. Never blocks on the transcode."""
    _ensure_worker()
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        if job is None:
            try:
                os.makedirs(HLS_CACHE_DIR, exist_ok=True)
            except OSError:
                pass
            job = _Job(name)
            _JOBS[name] = job
            with _QUEUE_LOCK:
                _QUEUE.append(job)
            _evict_lru(except_name=name)
        job.last_used = time.time()
        return job


def get_job(name):
    with _JOBS_LOCK:
        return _JOBS.get(name)


def master_playlist(job):
    """Static master playlist. RESOLUTION comes from the probe
    (exact); BANDWIDTH is the rendition's maxrate + audio + margin.
    hls.js starts at the lowest rendition and adapts by measured
    throughput."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for (rname, _bw, _bh, _vb, _mr, _bs, _ab, bw) in RENDITIONS:
        attrs = "BANDWIDTH=%d" % bw
        dims = job.dims.get(rname)
        if dims:
            attrs += ",RESOLUTION=%dx%d" % dims
        lines.append("#EXT-X-STREAM-INF:" + attrs)
        lines.append(rname + ".m3u8")
    return ("\n".join(lines) + "\n").encode("utf-8")


def wait_for_first_segment(job, timeout=HLS_MASTER_WAIT_S):
    """True once the 240p playlist exists with at least one
    segment listed (the point playback can start)."""
    deadline = time.time() + timeout
    path = os.path.join(job.dir, "240.m3u8")
    while time.time() < deadline:
        if job.state == "failed":
            return False
        try:
            with open(path, "r") as f:
                if "#EXTINF" in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def status_dict():
    jobs = []
    with _JOBS_LOCK:
        items = list(_JOBS.values())
    for j in items:
        jobs.append({"name": j.name, "state": j.state,
                     "error": j.error,
                     "bytes": _job_dir_size(j.dir)})
    return {"ffmpeg": ffmpeg_available(),
            "cache_dir": HLS_CACHE_DIR,
            "cache_cap_bytes": HLS_CACHE_CAP,
            "cache_bytes": _cache_total_bytes(),
            "jobs": jobs}


# ----------------------------------------------------------------------
# Injected player script (xfree video pages only; NOT tik/red).
# Runs in the page: swaps the main player's src to the light HLS
# master (native HLS if the WebView supports it, else hls.js from
# this proxy), keeps video.currentSrc returning the ORIGINAL mp4 URL
# so the app's subtitle engine keeps working, and falls back
# HLS -> light 360p MP4 -> original mp4 on any failure. Never throws
# out of the page; a failure just leaves the original video playing.
# ----------------------------------------------------------------------
LITE_PLAYER_JS = r"""
(function(){try{
if(window.__xfLite)return;window.__xfLite=1;
function nameOf(src){
 if(!src)return null;
 var m=src.match(/\/video\?u=([^&]+)/);
 if(m){try{src=decodeURIComponent(m[1]);}catch(e){}}
 m=src.match(/xfree-prod\/[A-Za-z0-9]\/[A-Za-z0-9]\/[A-Za-z0-9]\/([A-Za-z0-9][A-Za-z0-9_-]{7,63})\/full\.mp4/);
 return m?m[1]:null;
}
function rawSrc(v){
 var s=v.getAttribute&&v.getAttribute('src');
 if(s)return s;
 try{var so=v.querySelector('source');if(so&&so.getAttribute('src'))return so.getAttribute('src');}catch(e){}
 if(!v.__xfLiteName){return v.currentSrc||v.src||'';}
 return '';
}
function isMain(v){if(!v)return false;if(v.id==='feed-video-element')return true;try{var r=v.getBoundingClientRect();return r.width>=300&&r.height>=200;}catch(e){return false;}}
var hlsLoading=false;
function arm(v){
 try{
  if(!isMain(v))return;
  var src=rawSrc(v);
  var name=nameOf(src);
  if(!name)return;
  if(v.__xfLiteName===name)return;
  if(v.__xfHls){try{v.__xfHls.destroy();}catch(e){}v.__xfHls=null;}
  v.__xfLiteName=name;
  var orig=src;
  v.__xfLiteOrig=orig;
  var master='/lite/'+name+'/master.m3u8';
  var mp4='/lite/'+name+'/360.mp4';
  var t0=v.currentTime||0;
  var wasPlaying=!v.paused&&!v.ended;
  try{Object.defineProperty(v,'currentSrc',{configurable:true,get:function(){return v.__xfLiteOrig||'';}});}catch(e){}
  function playIfWas(){if(!wasPlaying)return;try{var p=v.play();if(p&&p.catch)p.catch(function(){});}catch(e){}}
  function seekIf(){if(t0){try{v.currentTime=t0;}catch(e){}}}
  function restoreOrig(){try{delete v.currentSrc;}catch(e){}v.src=orig;v.load();seekIf();playIfWas();}
  function fallbackMp4(){
   try{delete v.currentSrc;}catch(e){}
   v.addEventListener('error',function onErr(){v.removeEventListener('error',onErr);restoreOrig();},{once:true});
   v.addEventListener('loadedmetadata',function(){seekIf();playIfWas();},{once:true});
   v.src=mp4;v.load();
  }
  function useHls(){
   if(v.canPlayType&&v.canPlayType('application/vnd.apple.mpegurl')!==''){
    v.src=master;v.load();
    v.addEventListener('loadedmetadata',function(){seekIf();playIfWas();},{once:true});
    v.addEventListener('error',function(){fallbackMp4();},{once:true});
    return;
   }
   if(window.Hls&&Hls.isSupported()){
    var h=new Hls({maxBufferLength:30,manifestLoadingMaxRetry:6,levelLoadingMaxRetry:6,fragLoadingMaxRetry:6});
    v.__xfHls=h;
    h.loadSource(master);h.attachMedia(v);
    h.on(Hls.Events.MANIFEST_PARSED,function(){seekIf();playIfWas();});
    h.on(Hls.Events.ERROR,function(ev,d){if(d&&d.fatal){try{h.destroy();}catch(e){}v.__xfHls=null;fallbackMp4();}});
    return;
   }
   if(!hlsLoading){hlsLoading=true;var s=document.createElement('script');s.src='/hls.js';s.onload=function(){hlsLoading=false;useHls();};s.onerror=function(){hlsLoading=false;};document.head.appendChild(s);}
   else{setTimeout(useHls,500);}
  }
  useHls();
 }catch(e){}
}
function scan(){try{var vs=document.querySelectorAll('video');for(var i=0;i<vs.length;i++){arm(vs[i]);}}catch(e){}}
var scanPending=false;
function scheduleScan(){if(scanPending)return;scanPending=true;setTimeout(function(){scanPending=false;scan();},0);}
try{new MutationObserver(function(){scheduleScan();}).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['src']});}catch(e){}
scan();
}catch(e){}})();
"""
