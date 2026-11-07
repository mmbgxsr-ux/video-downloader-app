#!/usr/bin/env python3
"""أداة تحميل الفيديوهات برابط — تعمل على جهازك مع واجهة عربية.

التشغيل:
    pip install yt-dlp
    python video_downloader.py

ثم يفتح المتصفح تلقائياً على http://127.0.0.1:8765
يُنصح بتثبيت ffmpeg لدمج الصوت والصورة وتحويل MP3.
استخدمها فقط للمحتوى الذي تملكه أو يُسمح لك بتحميله.
"""
import json, mimetypes, os, re, shutil, threading, uuid, webbrowser, urllib.request
from urllib.parse import urlparse, parse_qs, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
JOBS = {}
MAX_JOBS = 50
MAX_BODY = 16 * 1024
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
ALLOWED_ORIGINS = {"http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
DIRECT_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".mp3")

FORMATS = {
    "best": "bv*+ba/b",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "audio": "bestaudio/best",
}
# Used when ffmpeg is missing: a single pre-merged file, still honouring the height.
NO_FFMPEG_FORMATS = {
    "best": "b[ext=mp4]/b",
    "720": "b[height<=720][ext=mp4]/b[height<=720]/b",
    "480": "b[height<=480][ext=mp4]/b[height<=480]/b",
    "audio": "bestaudio/best",
}


def _android_activity():
    try:
        from jnius import autoclass
        return autoclass("org.kivy.android.PythonActivity").mActivity
    except Exception:
        return None


def get_out_dir():
    """Folder the app can always write to (app-specific storage on Android)."""
    base = os.path.dirname(os.path.abspath(__file__))
    act = _android_activity()
    if act is not None:
        try:
            d = act.getExternalFilesDir(None) or act.getFilesDir()
            base = str(d.getAbsolutePath())
        except Exception:
            pass
    path = os.path.join(base, "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def publish_to_downloads(path):
    """Android 10+: copy the finished file into the public Download/VideoDL folder
    through MediaStore (no storage permission needed). Returns the public path
    shown to the user, or None if not on Android / it failed."""
    act = _android_activity()
    if act is None:
        return None
    try:
        from jnius import autoclass
        if autoclass("android.os.Build$VERSION").SDK_INT < 29:
            return None
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        ContentValues = autoclass("android.content.ContentValues")
        Downloads = autoclass("android.provider.MediaStore$Downloads")
        values = ContentValues()
        values.put("_display_name", name)
        values.put("mime_type", mime)
        values.put("relative_path", "Download/VideoDL")
        resolver = act.getContentResolver()
        uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            return None
        pfd = resolver.openFileDescriptor(uri, "w")
        fd = pfd.detachFd()
        with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.remove(path)
        return "Download/VideoDL/" + name
    except Exception:
        return None


def direct_download(job, url, out_dir):
    name = unquote(os.path.basename(urlparse(url).path)).strip(". ") or "video.mp4"
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    path = os.path.join(out_dir, name)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                job["progress"] = round(done * 100 / total, 1)
    return path


def ytdlp_download(job, url, fmt, out_dir, yt_dlp):
    has_ff = shutil.which("ffmpeg") is not None
    if fmt not in FORMATS:
        fmt = "best"
    if not has_ff and fmt == "audio":
        job["note"] = "ffmpeg غير متوفر: سيُحفظ الصوت بصيغته الأصلية وليس MP3"

    def hook(d):
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                job["progress"] = round(d["downloaded_bytes"] * 100 / tot, 1)

    opts = {
        "format": (FORMATS if has_ff else NO_FFMPEG_FORMATS)[fmt],
        "outtmpl": os.path.join(out_dir, "%(title).150B.%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
    }
    if fmt == "audio" and has_ff:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    if has_ff and fmt != "audio":
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), info)
            dl = ((info or {}).get("requested_downloads") or [{}])[0]
            path = dl.get("filepath") or y.prepare_filename(info)
    except Exception:
        if url.lower().split("?")[0].endswith(DIRECT_EXTS):
            return direct_download(job, url, out_dir)
        raise
    return path


def run_job(jid, url, fmt):
    job = JOBS[jid]
    try:
        out_dir = get_out_dir()
        try:
            import yt_dlp
        except ImportError:
            yt_dlp = None
        if yt_dlp is None:
            job["note"] = "yt-dlp غير مثبّت: تحميل مباشر فقط (pip install yt-dlp)"
            path = direct_download(job, url, out_dir)
        else:
            path = ytdlp_download(job, url, fmt, out_dir, yt_dlp)
        job["file"] = (publish_to_downloads(path) if path else None) or path
        job["status"], job["progress"] = "done", 100
    except Exception as e:
        job["status"], job["error"] = "error", str(e)[:300]


def _trim_jobs():
    """Keep memory bounded: drop the oldest finished jobs."""
    for k in [k for k, v in JOBS.items() if v.get("status") != "running"]:
        if len(JOBS) <= MAX_JOBS:
            break
        JOBS.pop(k, None)


PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>تحميل الفيديوهات</title>
<style>
body{font-family:Tahoma,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;display:grid;place-items:center;min-height:100vh}
.card{background:#1e293b;padding:28px;border-radius:16px;width:min(560px,92vw)}
h1{margin:0 0 18px;font-size:22px}input,select,button{width:100%;padding:12px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:inherit;font-size:16px;margin-bottom:12px;box-sizing:border-box}
input{direction:ltr;text-align:left}button{background:#3b82f6;border:0;cursor:pointer;font-weight:bold}button:disabled{opacity:.5}
.bar{height:10px;background:#0f172a;border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:#22c55e;transition:width .3s}
#msg{margin-top:12px;font-size:14px;word-break:break-all}small{color:#94a3b8}
</style></head><body><div class="card"><h1>⬇️ تحميل فيديو برابط</h1>
<input id="url" placeholder="الصق رابط الفيديو هنا" type="url">
<select id="fmt"><option value="best">أفضل جودة (MP4)</option><option value="720">720p</option><option value="480">480p</option><option value="audio">صوت فقط (MP3)</option></select>
<button id="go">ابدأ التحميل</button><div class="bar"><i id="p"></i></div><div id="msg"></div>
<small>يُحفظ الملف في مجلد downloads بجانب السكربت (وعلى أندرويد في Download/VideoDL). حمّل فقط ما يحق لك تحميله.</small></div>
<script>
const $=id=>document.getElementById(id);
$('go').onclick=async()=>{
  const url=$('url').value.trim(); if(!/^https?:\\/\\//i.test(url)){$('msg').textContent='أدخل رابطاً صحيحاً يبدأ بـ http أو https';return}
  $('go').disabled=true;$('p').style.width='0';$('msg').textContent='جارٍ البدء...';
  let id;
  try{
    const r=await fetch('/start',{method:'POST',body:JSON.stringify({url,fmt:$('fmt').value})});
    const j=await r.json(); id=j.id; if(!id) throw new Error(j.error||'bad response');
  }catch(e){$('go').disabled=false;$('msg').textContent='❌ تعذّر البدء: '+e.message;return}
  const t=setInterval(async()=>{
    let s;
    try{ s=await (await fetch('/status?id='+id)).json() }catch(e){return}
    $('p').style.width=s.progress+'%';
    $('msg').textContent=(s.note?s.note+' — ':'')+'التقدم: '+s.progress+'%';
    if(s.status==='done'){clearInterval(t);$('go').disabled=false;$('msg').textContent='✅ تم التحميل: '+(s.file||'')}
    if(s.status==='error'){clearInterval(t);$('go').disabled=false;$('msg').textContent='❌ فشل: '+s.error}
  },800);
};
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", status=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _local_only(self):
        """Reject requests from other sites/hosts (CSRF and DNS-rebinding guard)."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def do_GET(self):
        if not self._local_only():
            return self._send(json.dumps({"error": "forbidden"}), status=403)
        u = urlparse(self.path)
        if u.path == "/status":
            jid = parse_qs(u.query).get("id", [""])[0]
            self._send(json.dumps(JOBS.get(jid, {"status": "error", "error": "غير موجود", "progress": 0})))
        else:
            self._send(PAGE, "text/html")

    def do_POST(self):
        if not self._local_only():
            return self._send(json.dumps({"error": "forbidden"}), status=403)
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                raise ValueError("too large")
            data = json.loads(self.rfile.read(n) or "{}")
            if not isinstance(data, dict):
                raise ValueError("bad body")
        except ValueError:
            return self._send(json.dumps({"error": "bad request"}), status=400)
        url = str(data.get("url", ""))
        if not re.match(r"^https?://", url, re.I):
            return self._send(json.dumps({"error": "bad url"}), status=400)
        _trim_jobs()
        jid = uuid.uuid4().hex[:8]
        JOBS[jid] = {"status": "running", "progress": 0}
        threading.Thread(target=run_job, args=(jid, url, str(data.get("fmt", "best"))), daemon=True).start()
        self._send(json.dumps({"id": jid}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"افتح: http://127.0.0.1:{PORT}  (Ctrl+C للإيقاف)")
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
