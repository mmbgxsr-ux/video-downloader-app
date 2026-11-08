#!/usr/bin/env python3
"""أداة تحميل الفيديوهات برابط — تعمل على جهازك مع واجهة عربية.

التشغيل:
    pip install yt-dlp
    python video_downloader.py

ثم يفتح المتصفح تلقائياً على http://127.0.0.1:8765
يُنصح بتثبيت ffmpeg لدمج الصوت والصورة وتحويل MP3.
استخدمها فقط للمحتوى الذي تملكه أو يُسمح لك بتحميله.
"""
import html, http.client, json, mimetypes, os, re, shutil, subprocess, threading, time, uuid, webbrowser
import urllib.error, urllib.request
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
JOBS = {}  # id -> job dict (running jobs and finished history)
MAX_JOBS = 100
MAX_PARALLEL = 2
MAX_PLAYLIST = 50
SLOTS = threading.BoundedSemaphore(MAX_PARALLEL)
MAX_BODY = 16 * 1024
FINAL = ("done", "error", "cancelled")
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
ALLOWED_ORIGINS = {"http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
DIRECT_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".mp3")
LAST_ACTIVITY = time.time()  # last HTTP request; lets the Android service stop when idle
_LOADED = False
_SAVE_LOCK = threading.Lock()

STD_FORMATS = ("best", "audio")


def valid_fmt(fmt):
    return fmt in STD_FORMATS or (fmt.isdigit() and 100 <= int(fmt) <= 4320)


def fmt_selector(fmt, has_ff):
    """yt-dlp format string. Without ffmpeg only a single pre-merged file is possible."""
    if fmt == "audio":
        return "bestaudio/best"
    if fmt == "best":
        return "bv*+ba/b" if has_ff else "b[ext=mp4]/b"
    h = int(fmt)
    if has_ff:
        return "bv*[height<=%d]+ba/b[height<=%d]" % (h, h)
    return "b[height<=%d][ext=mp4]/b[height<=%d]/b" % (h, h)


def _hum(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- Android glue
def _android_context():
    """Activity (UI process) or Service (background process): both are Contexts."""
    try:
        from jnius import autoclass
    except Exception:
        return None
    for cls, field in (("org.kivy.android.PythonActivity", "mActivity"),
                       ("org.kivy.android.PythonService", "mService")):
        try:
            ctx = getattr(autoclass(cls), field)
            if ctx is not None:
                return ctx
        except Exception:
            pass
    return None


def data_dir():
    base = os.path.dirname(os.path.abspath(__file__))
    ctx = _android_context()
    if ctx is not None:
        try:
            d = ctx.getExternalFilesDir(None) or ctx.getFilesDir()
            base = str(d.getAbsolutePath())
        except Exception:
            pass
    return base


def get_out_dir():
    path = os.path.join(data_dir(), "downloads")
    os.makedirs(path, exist_ok=True)
    return path


_FF = {"checked": False, "exe": None, "loc": None}


def find_ffmpeg():
    """Return the usable ffmpeg executable (or None). On Android the build ships it as
    libffmpeg.so in the native library dir (the only place an app may execute from);
    a symlink named `ffmpeg` is created in the app's private dir for yt-dlp."""
    if _FF["checked"]:
        return _FF["exe"]
    _FF["checked"] = True
    cands = []  # (executable, ffmpeg_location for yt-dlp or None)
    w = shutil.which("ffmpeg")
    if w:
        cands.append((w, None))
    ctx = _android_context()
    if ctx is not None:
        try:
            lib = str(ctx.getApplicationInfo().nativeLibraryDir) + "/libffmpeg.so"
            if os.path.isfile(lib):
                bindir = os.path.join(str(ctx.getFilesDir().getAbsolutePath()), "bin")
                os.makedirs(bindir, exist_ok=True)
                link = os.path.join(bindir, "ffmpeg")
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(lib, link)
                cands.append((link, bindir))
        except Exception:
            pass
    for exe, loc in cands:
        try:
            if subprocess.run([exe, "-version"], capture_output=True, timeout=15).returncode == 0:
                _FF["exe"], _FF["loc"] = exe, loc
                break
        except Exception:
            pass
    return _FF["exe"]


def publish_to_downloads(path):
    """Android 10+: copy the finished file into the public Download/VideoDL folder
    through MediaStore (no storage permission needed).
    Returns {"display", "uri", "mime"} or None when not on Android / it failed."""
    ctx = _android_context()
    if ctx is None:
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
        resolver = ctx.getContentResolver()
        uri = resolver.insert(Downloads.EXTERNAL_CONTENT_URI, values)
        if uri is None:
            return None
        try:
            pfd = resolver.openFileDescriptor(uri, "w")
            fd = pfd.detachFd()
            with open(path, "rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        except Exception:
            try:  # drop the half-written public file
                resolver.delete(uri, None, None)
            except Exception:
                pass
            raise
        os.remove(path)
        return {"display": "Download/VideoDL/" + name, "uri": str(uri.toString()), "mime": mime}
    except Exception:
        return None


def android_view(job, share=False):
    """Open or share a finished file through its MediaStore uri."""
    ctx = _android_context()
    uri_s = job.get("uri")
    if ctx is None or not uri_s:
        raise RuntimeError("not available")
    from jnius import autoclass, cast
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    uri = Uri.parse(uri_s)
    mime = job.get("mime") or "*/*"
    flags = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK
    if share:
        inner = Intent(Intent.ACTION_SEND)
        inner.setType(mime)
        inner.putExtra(Intent.EXTRA_STREAM, cast("android.os.Parcelable", uri))
        inner.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        intent = Intent.createChooser(inner, "Share")
    else:
        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(uri, mime)
    intent.addFlags(flags)
    ctx.startActivity(intent)


# -------------------------------------------------------------------- history
def _history_path():
    return os.path.join(data_dir(), "history.json")


def _ensure_loaded():
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        with open(_history_path(), encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return
    for j in items if isinstance(items, list) else []:
        if not (isinstance(j, dict) and j.get("id")):
            continue
        if j.get("status") in ("queued", "running"):
            # The process died mid-download: offer to resume instead of losing it.
            j["status"], j["progress"] = "error", j.get("progress", 0)
            j["error"] = "انقطع التحميل قبل أن يكتمل. اضغط «إعادة المحاولة» لاستكماله."
            j.pop("cancel", None)
        if j.get("status") in FINAL:
            JOBS.setdefault(j["id"], j)


def save_history():
    items = list(JOBS.values())
    items.sort(key=lambda j: j.get("created", 0))
    try:
        with _SAVE_LOCK:
            tmp = _history_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items[-MAX_JOBS:], f, ensure_ascii=False)
            os.replace(tmp, _history_path())
    except Exception:
        pass


def _trim_jobs():
    """Keep memory bounded: drop the oldest finished jobs."""
    finished = sorted((j for j in list(JOBS.values()) if j.get("status") in FINAL),
                      key=lambda j: j.get("created", 0))
    while len(JOBS) > MAX_JOBS and finished:
        JOBS.pop(finished.pop(0)["id"], None)


def is_busy():
    return any(j.get("status") in ("queued", "running") for j in list(JOBS.values()))


# ------------------------------------------------------------------ downloads
def direct_download(job, url, out_dir):
    name = unquote(os.path.basename(urlparse(url).path)).strip(". ") or "video.mp4"
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    if job.get("title") == job.get("url"):
        job["title"] = name
    path = os.path.join(out_dir, name)
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):  # never overwrite an earlier download
        n += 1
        path = "%s (%d)%s" % (stem, n, ext)
    part = path + ".part"  # kept after a failure so a retry resumes where it stopped
    last_err = None
    try:
        for attempt in range(4):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            headers = {"User-Agent": "Mozilla/5.0"}
            if job.get("referer"):
                headers["Referer"] = job["referer"]
            if have:
                headers["Range"] = "bytes=%d-" % have
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    if have and getattr(r, "status", 200) != 206:
                        have = 0  # the server ignored the range: start over
                    cr = r.headers.get("Content-Range") or ""
                    cl = r.headers.get("Content-Length")
                    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                        total = int(cr.rsplit("/", 1)[1])
                    else:
                        total = have + int(cl) if cl and cl.isdigit() else 0
                    done, t0, last_t = have, time.time(), 0
                    with open(part, "ab" if have else "wb") as f:
                        while True:
                            if job.get("cancel"):
                                raise Cancelled()
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                job["progress"] = round(done * 100 / total, 1)
                            now = time.time()
                            if now - last_t > 1:
                                last_t = now
                                job["speed"] = _hum((done - have) / max(now - t0, 0.001)) + "/s"
                os.replace(part, path)
                job.pop("speed", None)
                return path
            except Cancelled:
                raise
            except urllib.error.HTTPError as e:
                if e.code == 416 and os.path.exists(part):  # already complete
                    os.replace(part, path)
                    return path
                raise
            except (OSError, http.client.HTTPException) as e:
                last_err = e
                for _ in range(2 * (attempt + 1)):  # back off, but stay cancellable
                    if job.get("cancel"):
                        raise Cancelled()
                    time.sleep(1)
        raise last_err
    except Cancelled:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        raise


def ytdlp_download(job, url, fmt, out_dir, yt_dlp):
    has_ff = find_ffmpeg() is not None
    if not valid_fmt(fmt):
        fmt = "best"
    if not has_ff and fmt == "audio":
        job["note"] = "ffmpeg غير متوفر: سيُحفظ الصوت بصيغته الأصلية وليس MP3"

    def hook(d):
        if job.get("cancel"):
            raise Cancelled()
        title = (d.get("info_dict") or {}).get("title")
        if title and job.get("title") == job.get("url"):
            job["title"] = title
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                job["progress"] = round(d["downloaded_bytes"] * 100 / tot, 1)
            if d.get("speed"):
                job["speed"] = _hum(d["speed"]) + "/s"

    opts = {
        "format": fmt_selector(fmt, has_ff),
        "outtmpl": os.path.join(out_dir, "%(title).150B.%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "continuedl": True,  # a retry resumes the .part file left by an interrupted run
    }
    if _FF["loc"]:
        opts["ffmpeg_location"] = _FF["loc"]
    if job.get("referer"):
        opts["http_headers"] = {"Referer": job["referer"]}
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
            if info and info.get("title"):
                job["title"] = info["title"]
    except Exception:
        if not job.get("cancel") and url.lower().split("?")[0].endswith(DIRECT_EXTS):
            return direct_download(job, url, out_dir)
        raise
    return path



# ------------------------------------------------------- friendly error text
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# (lower-case fragments of the raw message, Arabic explanation); first match wins
ERROR_RULES = [
    (("private video", "video is private"), "الفيديو خاص ولا يمكن تحميله دون تسجيل الدخول إلى الحساب المسموح له."),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "الفيديو مقيّد بالعمر ويتطلب تسجيل الدخول."),
    (("not a bot", "confirm you’re not", "captcha"), "الموقع يطلب التحقق من أنك لست روبوتًا. جرّب لاحقًا أو من شبكة أخرى."),
    (("drm",), "الفيديو محمي بـ DRM ولا يمكن تحميله."),
    (("unsupported url",), "الرابط غير مدعوم مباشرة. جرّب زر «ابحث عن الفيديو في الصفحة»."),
    (("members-only", "members only", "join this channel", "premium", "requires payment", "rental"),
     "المحتوى للمشتركين أو المدفوع فقط."),
    (("not available in your country", "geo restrict", "geo-restrict", "blocked it in your country"),
     "الفيديو غير متاح في بلدك."),
    (("live event will begin", "premieres in", "is upcoming", "this live event"), "بث مباشر لم يبدأ بعد أو لم ينتهِ."),
    (("video unavailable", "this video is not available", "no longer available", "has been removed",
      "been terminated", "does not exist", "video has been deleted", "content isn't available"),
     "الفيديو غير متاح أو حُذف."),
    (("requested format is not available", "no video formats found"),
     "الجودة المطلوبة غير متاحة لهذا الفيديو. جرّب خيارًا آخر."),
    (("ffmpeg",), "هذه الصيغة تحتاج ffmpeg وهو غير متوفر. جرّب جودة أخرى."),
    (("http error 403", "forbidden"), "الموقع رفض الطلب (403). قد يتطلب تسجيل دخول أو يحجب التحميل."),
    (("http error 404", "not found"), "الرابط غير موجود (404)."),
    (("http error 429", "too many requests"), "طلبات كثيرة. انتظر قليلًا ثم أعد المحاولة."),
    (("no space left",), "لا توجد مساحة كافية على الجهاز."),
    (("name resolution", "network is unreachable", "timed out", "timeout", "connection refused",
      "connection reset", "unable to download webpage", "urlopen error", "getaddrinfo", "ssl"),
     "تعذّر الاتصال بالموقع أو بالإنترنت. تحقق من الاتصال وأعد المحاولة."),
]


def friendly_error(raw):
    """Arabic explanation for common yt-dlp / network errors; falls back to the cleaned original."""
    text = _ANSI.sub("", str(raw)).strip()
    low = text.lower()
    for needles, msg in ERROR_RULES:
        if any(n in low for n in needles):
            return msg
    text = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?", "", text)
    return "حدث خطأ: " + text[:200]


def run_job(jid):
    job = JOBS[jid]
    with SLOTS:  # at most MAX_PARALLEL downloads at once, the rest wait as "queued"
        if job.get("status") != "queued":  # cancelled while waiting
            return
        job["status"] = "running"
        save_history()
        try:
            out_dir = get_out_dir()
            try:
                import yt_dlp
            except ImportError:
                yt_dlp = None
            if yt_dlp is None:
                job["note"] = "yt-dlp غير مثبّت: تحميل مباشر فقط (pip install yt-dlp)"
                path = direct_download(job, job["url"], out_dir)
            else:
                path = ytdlp_download(job, job["url"], job["fmt"], out_dir, yt_dlp)
            pub = publish_to_downloads(path) if path else None
            job["file"] = pub["display"] if pub else path
            job["can_open"] = bool(pub)
            if pub:
                job["uri"], job["mime"] = pub["uri"], pub["mime"]
            job["status"], job["progress"] = "done", 100
        except Exception as e:
            if job.get("cancel") or isinstance(e, Cancelled):
                job["status"] = "cancelled"
            else:
                job["status"], job["error"] = "error", friendly_error(e)
                job["error_raw"] = _ANSI.sub("", str(e))[:300]
        finally:
            job.pop("speed", None)
            save_history()


def new_job(url, fmt, referer=None, title=None):
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {"id": jid, "url": url, "fmt": fmt, "title": title or url, "status": "queued",
                 "progress": 0, "created": time.time()}
    if referer:
        JOBS[jid]["referer"] = referer
    _trim_jobs()
    save_history()
    threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    return jid


def retry_job(jid):
    """Start the same download again (it resumes a partial file when possible)."""
    old = JOBS.get(jid)
    if not old or old.get("status") not in ("error", "cancelled"):
        return None
    title = old.get("title")
    new = new_job(old["url"], old.get("fmt", "best"), old.get("referer"),
                  None if title == old["url"] else title)
    JOBS.pop(jid, None)
    save_history()
    return new


def _need_ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp غير مثبّت")


def probe(url, playlist=False):
    """Inspect a link: title, whether it is a playlist, and the qualities really available."""
    yt_dlp = _need_ytdlp()
    has_ff = find_ffmpeg() is not None
    opts = {"quiet": True, "skip_download": True, "socket_timeout": 15,
            "noplaylist": not playlist, "extract_flat": "in_playlist" if playlist else False}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False) or {}
    entries = [e for e in (info.get("entries") or []) if e]
    out = {"title": info.get("title") or url, "ffmpeg": has_ff, "heights": [],
           "is_playlist": bool(info.get("entries")), "count": len(entries[:MAX_PLAYLIST])}
    hs = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
        if not has_ff and f.get("acodec") == "none":
            continue  # without ffmpeg only pre-merged (video+audio) files can be saved
        hs.add(int(h))
    out["heights"] = sorted(hs, reverse=True)
    return out


def playlist_items(url):
    """[(url, title)] for a playlist link (a single video gives one item)."""
    yt_dlp = _need_ytdlp()
    opts = {"quiet": True, "skip_download": True, "socket_timeout": 15,
            "noplaylist": False, "extract_flat": "in_playlist"}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False) or {}
    if not info.get("entries"):
        return [(url, None)]
    items = []
    for e in info["entries"]:
        if not e:
            continue
        u = e.get("webpage_url") or e.get("url") or ""
        if not re.match(r"^https?://", u, re.I):
            if e.get("ie_key") == "Youtube" and e.get("id"):
                u = "https://www.youtube.com/watch?v=" + e["id"]
            else:
                continue
        items.append((u, (e.get("title") or None)))
        if len(items) >= MAX_PLAYLIST:
            break
    return items


def cancel_job(jid):
    job = JOBS.get(jid)
    if not job or job.get("status") in FINAL:
        return False
    job["cancel"] = True
    if job["status"] == "queued":
        job["status"] = "cancelled"
        save_history()
    return True


def clear_finished():
    for k in [k for k, v in list(JOBS.items()) if v.get("status") in FINAL]:
        JOBS.pop(k, None)
    save_history()


def public_view(j):
    keys = ("id", "title", "url", "status", "progress", "speed", "note", "error", "file", "can_open", "created")
    return {k: j.get(k) for k in keys if k in j}



# ----------------------------------------------------------- page link finder
UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
_EXT = "mp4|m4v|webm|mkv|mov|m3u8|mpd|mp3|m4a"
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:%s)(?:\?[^\s\"'<>\\]*)?" % _EXT, re.I)
ATTR_RE = re.compile(r"""(?:src|data-src|data-video|data-url|href|content)\s*=\s*["']([^"']+?\.(?:%s)(?:\?[^"']*)?)["']""" % _EXT, re.I)
IFRAME_RE = re.compile(r"""<iframe\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
EMBED_HOSTS = ("youtube.com", "youtube-nocookie.com", "youtu.be", "vimeo.com",
               "dailymotion.com", "facebook.com", "streamable.com", "twitch.tv")
KIND_ORDER = {"فيديو": 0, "بث HLS": 1, "مضمّن": 2, "بث DASH": 3, "صوت": 4}


def _kind(u):
    ext = os.path.splitext(urlparse(u).path)[1].lower()
    if ext == ".m3u8":
        return "بث HLS"
    if ext == ".mpd":
        return "بث DASH"
    if ext in (".mp3", ".m4a"):
        return "صوت"
    return "فيديو"


def scan_page(url):
    """Fetch a page and list the video/audio links found in it."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity",
                                               "Accept": "text/html,application/xhtml+xml,*/*"})
    with urllib.request.urlopen(req, timeout=15) as r:
        final = r.geturl()
        ctype = (r.headers.get("Content-Type") or "").lower()
        charset = r.headers.get_content_charset() or "utf-8"
        raw = r.read(2_000_000)
    items, seen = [], set()

    def add(u, kind):
        u = html.unescape(u).strip()
        full = urljoin(final, u)
        if not re.match(r"^https?://", full, re.I) or full in seen:
            return
        seen.add(full)
        name = unquote(os.path.basename(urlparse(full).path)) or urlparse(full).netloc
        items.append({"url": full, "kind": kind, "name": name[:120]})

    if ctype.startswith(("video/", "audio/")) or "mpegurl" in ctype:
        add(final, _kind(final))
        return items
    text = raw.decode(charset, "replace")
    # URLs inside inline JSON are often escaped.
    text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002F", "/")
    for m in ATTR_RE.finditer(text):
        add(m.group(1), _kind(m.group(1)))
    for m in URL_RE.finditer(text):
        add(m.group(0), _kind(m.group(0)))
    for m in IFRAME_RE.finditer(text):
        src = urljoin(final, html.unescape(m.group(1)))
        host = (urlparse(src).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in EMBED_HOSTS):
            add(src, "مضمّن")
    items.sort(key=lambda i: KIND_ORDER.get(i["kind"], 9))
    return items[:30]


# ------------------------------------------------------------------------ UI
PAGE = r"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>تحميل الفيديوهات</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--bd:#334155;--mut:#94a3b8;--ok:#22c55e;--pri:#3b82f6;--sec:#334155}
:root[data-theme=light]{--bg:#f1f5f9;--card:#ffffff;--fg:#0f172a;--bd:#cbd5e1;--mut:#64748b;--sec:#e2e8f0}
body{font-family:Tahoma,Arial,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px;min-height:100vh;box-sizing:border-box}
.card{background:var(--card);padding:22px;border-radius:16px;width:min(560px,100%);margin:0 auto 14px;box-sizing:border-box}
h1{margin:0;font-size:22px}h2{margin:0 0 10px;font-size:17px;display:flex;justify-content:space-between;align-items:center}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
input,select,button{width:100%;padding:12px;border-radius:10px;border:1px solid var(--bd);background:var(--bg);color:var(--fg);font-size:16px;margin-bottom:12px;box-sizing:border-box}
input[type=url]{direction:ltr;text-align:left}
button{background:var(--pri);color:#fff;border:0;cursor:pointer;font-weight:bold}button:disabled{opacity:.5}
.bar{height:8px;background:var(--bg);border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:var(--ok);transition:width .3s}
#msg{font-size:14px;word-break:break-word;min-height:18px}small{color:var(--mut)}
.it{border-top:1px solid var(--bd);padding:12px 0}.it:first-child{border-top:0}
.t{font-size:15px;word-break:break-word;margin-bottom:4px}.m{font-size:13px;color:var(--mut);margin-bottom:6px;word-break:break-word}
.row{display:flex;gap:8px;margin-top:8px}.row button{margin:0;padding:8px;font-size:14px;background:var(--sec);color:var(--fg)}
.alt{background:var(--sec);color:var(--fg)}
.fnd{border-top:1px solid var(--bd);padding:10px 0;display:flex;gap:8px;align-items:center}.fnd .n{flex:1;min-width:0;font-size:13px;word-break:break-all}.fnd .n b{color:var(--ok);margin-inline-end:6px}.fnd button{width:auto;margin:0;padding:8px 14px;font-size:14px}
.link{width:auto;margin:0;padding:4px 10px;font-size:13px;background:var(--sec);color:var(--fg)}
.ur{display:flex;gap:8px}.ur input{flex:1;min-width:0}.ur button{width:auto;padding:12px 14px;background:var(--sec);color:var(--fg)}
.fr{display:flex;gap:8px}.fr select{flex:1;min-width:0}.fr button{width:auto;white-space:nowrap}
.ck{display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:12px}.ck input{width:auto;margin:0}
</style></head><body>
<div class="card"><div class="top"><h1>⬇️ تحميل فيديو برابط</h1><button class="link" id="thm" title="تبديل المظهر">🌓</button></div>
<div class="ur"><input id="url" placeholder="الصق رابط الفيديو هنا" type="url"><button id="paste" title="لصق من الحافظة">📋</button><button id="xurl" title="مسح">✕</button></div>
<label class="ck"><input type="checkbox" id="pl"> قائمة تشغيل: حمّل كل فيديوهاتها (حتى 50)</label>
<div class="fr"><select id="fmt"></select><button id="probe" class="alt">فحص الجودات</button></div>
<button id="go">ابدأ التحميل</button><button id="scan" class="alt">ابحث عن الفيديو في الصفحة</button><div id="msg"></div><div id="found"></div>
<small>يُحفظ الملف في مجلد downloads بجانب السكربت (وعلى أندرويد في Download/VideoDL). حمّل فقط ما يحق لك تحميله.</small></div>
<div class="card"><h2><span>التحميلات</span><button class="link" id="clr">مسح السجل</button></h2><div id="list"></div></div>
<script>
const $=id=>document.getElementById(id);
const post=(p,b)=>fetch(p,{method:'POST',body:JSON.stringify(b||{})}).then(r=>r.json());
const ST={queued:'في الانتظار',running:'جارٍ التحميل',done:'✅ تم',error:'❌ فشل',cancelled:'أُلغي'};
function el(tag,text,cls){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
/* theme */
function setTheme(t){document.documentElement.dataset.theme=t;try{localStorage.setItem('th',t)}catch(e){}}
(function(){let t=null;try{t=localStorage.getItem('th')}catch(e){}
 if(!t)t=(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches)?'light':'dark';setTheme(t)})();
$('thm').onclick=()=>setTheme(document.documentElement.dataset.theme==='light'?'dark':'light');
/* quality list */
const DEF=[['best','أفضل جودة (MP4)'],['720','720p'],['480','480p'],['audio','صوت فقط (MP3)']];
function fillFmt(opts){const s=$('fmt');s.textContent='';opts.forEach(o=>{const e=el('option',o[1]);e.value=o[0];s.appendChild(e)})}
fillFmt(DEF);
function getUrl(){const v=$('url').value.trim();const m=v.match(/https?:\/\/\S+/i);return m?m[0]:v}
$('url').addEventListener('input',()=>fillFmt(DEF));
$('xurl').onclick=()=>{$('url').value='';$('found').textContent='';$('msg').textContent='';fillFmt(DEF)};
$('paste').onclick=async()=>{
  try{const t=await navigator.clipboard.readText();
    if(t&&t.trim()){const m=t.match(/https?:\/\/\S+/i);$('url').value=m?m[0]:t.trim();fillFmt(DEF);return}
  }catch(e){}
  $('msg').textContent='تعذّر القراءة من الحافظة تلقائياً — اضغط مطولاً على الحقل ثم «لصق»';$('url').focus()};
$('probe').onclick=async()=>{
  const url=getUrl();
  if(!/^https?:\/\//i.test(url)){$('msg').textContent='أدخل رابطاً صحيحاً أولاً';return}
  $('probe').disabled=true;$('msg').textContent='جارٍ فحص الجودات...';
  try{
    const j=await post('/probe',{url,playlist:$('pl').checked});
    if(j.error)throw new Error(j.error);
    let o=DEF;
    if(j.heights.length){
      o=[['best','أفضل جودة متاحة']];
      j.heights.forEach(h=>o.push([String(h),h+'p']));
      o.push(['audio',j.ffmpeg?'صوت فقط (MP3)':'صوت فقط (الصيغة الأصلية)']);
    }
    fillFmt(o);
    if(j.is_playlist)$('msg').textContent='قائمة تشغيل: '+j.title+' — '+j.count+' فيديو (الجودة المختارة تُطبَّق على الكل)';
    else $('msg').textContent='✔ '+j.title+(j.ffmpeg?'':' — بدون ffmpeg تظهر الجودات الجاهزة فقط');
  }catch(e){$('msg').textContent='❌ تعذّر الفحص: '+e.message}
  $('probe').disabled=false};
/* jobs */
let last='';
function render(jobs){
  const s=JSON.stringify(jobs); if(s===last)return; last=s;
  const box=$('list'); box.textContent='';
  if(!jobs.length){box.appendChild(el('small','لا توجد تحميلات بعد'));return}
  jobs.forEach(j=>{
    const it=el('div',null,'it');
    it.appendChild(el('div',j.title||j.url,'t'));
    let meta=ST[j.status]||j.status;
    if(j.status==='running')meta+=' '+j.progress+'%'+(j.speed?' · '+j.speed:'');
    if(j.note)meta+=' — '+j.note;
    if(j.status==='done'&&j.file)meta+=' — '+j.file;
    if(j.status==='error'&&j.error)meta+=' — '+j.error;
    it.appendChild(el('div',meta,'m'));
    if(j.status==='running'||j.status==='queued'){
      const b=el('div',null,'bar'),i=el('i');i.style.width=(j.progress||0)+'%';b.appendChild(i);it.appendChild(b);
      const r=el('div',null,'row'),c=el('button','إلغاء');c.onclick=()=>post('/cancel',{id:j.id});r.appendChild(c);it.appendChild(r);
    }else if(j.status==='error'||j.status==='cancelled'){
      const r=el('div',null,'row'),y=el('button','إعادة المحاولة / استكمال');
      y.onclick=async()=>{y.disabled=true;await post('/retry',{id:j.id});refresh()};r.appendChild(y);
      if(j.status==='error'){const c=el('button','ابحث عن الفيديو في الصفحة');
        c.onclick=()=>{$('url').value=j.url;window.scrollTo(0,0);scan(j.url)};r.appendChild(c)}
      it.appendChild(r);
    }else if(j.status==='done'&&j.can_open){
      const r=el('div',null,'row'),o=el('button','فتح'),h=el('button','مشاركة');
      o.onclick=()=>post('/open',{id:j.id});h.onclick=()=>post('/open',{id:j.id,share:true});
      r.appendChild(o);r.appendChild(h);it.appendChild(r);
    }
    box.appendChild(it);
  });
}
async function refresh(){try{render(await (await fetch('/jobs')).json())}catch(e){}}
async function start(url,extra){
  if(!/^https?:\/\//i.test(url)){$('msg').textContent='أدخل رابطاً صحيحاً يبدأ بـ http أو https';return}
  $('go').disabled=true;$('msg').textContent=($('pl').checked&&!extra)?'جارٍ قراءة القائمة...':'جارٍ البدء...';
  try{
    const j=await post('/start',Object.assign({url,fmt:$('fmt').value,playlist:!extra&&$('pl').checked},extra||{}));
    if(!j.id)throw new Error(j.error||'bad response');
    $('msg').textContent=j.count>1?('أُضيف '+j.count+' فيديو إلى القائمة'):'أُضيف إلى القائمة';$('url').value='';fillFmt(DEF);
  }catch(e){$('msg').textContent='❌ تعذّر البدء: '+e.message}
  $('go').disabled=false; refresh();
}
async function scan(url){
  const box=$('found'); box.textContent='';
  if(!/^https?:\/\//i.test(url)){$('msg').textContent='أدخل رابط الصفحة أولاً';return}
  $('msg').textContent='جارٍ البحث في الصفحة...';
  try{
    const j=await post('/scan',{url});
    if(j.error)throw new Error(j.error);
    if(!j.items.length){$('msg').textContent='لم أجد روابط فيديو في هذه الصفحة (قد يكون محمياً أو يُحمَّل بجافاسكربت)';return}
    $('msg').textContent='وجدت '+j.items.length+' رابطاً — اختر ما تريد تحميله:';
    j.items.forEach(it=>{
      const r=el('div',null,'fnd'),n=el('div',null,'n');
      n.appendChild(el('b',it.kind));n.appendChild(document.createTextNode(it.name));
      const b=el('button','تحميل');
      b.onclick=()=>{b.disabled=true;start(it.url,{referer:url,title:it.name})};
      r.appendChild(n);r.appendChild(b);box.appendChild(r);
    });
  }catch(e){$('msg').textContent='❌ تعذّر فحص الصفحة: '+e.message}
}
$('go').onclick=()=>start(getUrl());
$('scan').onclick=()=>scan(getUrl());
$('clr').onclick=async()=>{await post('/clear');refresh()};
const q=new URLSearchParams(location.search).get('u');
if(q){history.replaceState(null,'','/');$('url').value=q;start(q)}
refresh();setInterval(refresh,1000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json", status=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False), status=status)

    def _local_only(self):
        """Reject requests from other sites/hosts (CSRF and DNS-rebinding guard)."""
        global LAST_ACTIVITY
        LAST_ACTIVITY = time.time()
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def do_GET(self):
        if not self._local_only():
            return self._json({"error": "forbidden"}, 403)
        _ensure_loaded()
        u = urlparse(self.path)
        if u.path == "/jobs":
            jobs = sorted(list(JOBS.values()), key=lambda j: -j.get("created", 0))
            self._json([public_view(j) for j in jobs])
        elif u.path == "/status":
            jid = parse_qs(u.query).get("id", [""])[0]
            j = JOBS.get(jid)
            self._json(public_view(j) if j else {"status": "error", "error": "غير موجود", "progress": 0})
        else:
            self._send(PAGE, "text/html")

    def do_POST(self):
        if not self._local_only():
            return self._json({"error": "forbidden"}, 403)
        _ensure_loaded()
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                raise ValueError("too large")
            data = json.loads(self.rfile.read(n) or "{}")
            if not isinstance(data, dict):
                raise ValueError("bad body")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        path = urlparse(self.path).path
        if path == "/start":
            url = str(data.get("url", ""))
            if not re.match(r"^https?://", url, re.I):
                return self._json({"error": "bad url"}, 400)
            ref = str(data.get("referer") or "")
            ref = ref if re.match(r"^https?://", ref, re.I) else None
            title = str(data.get("title") or "")[:150] or None
            fmt = str(data.get("fmt", "best"))
            if data.get("playlist"):
                try:
                    items = playlist_items(url)
                except Exception as e:
                    return self._json({"error": friendly_error(e)}, 502)
                if not items:
                    return self._json({"error": "لم أجد فيديوهات في القائمة"}, 502)
                ids = [new_job(u, fmt, ref, t) for u, t in items]
                return self._json({"id": ids[0], "ids": ids, "count": len(ids)})
            jid = new_job(url, fmt, ref, title)
            return self._json({"id": jid, "ids": [jid], "count": 1})
        if path == "/probe":
            url = str(data.get("url", ""))
            if not re.match(r"^https?://", url, re.I):
                return self._json({"error": "bad url"}, 400)
            try:
                return self._json(probe(url, bool(data.get("playlist"))))
            except Exception as e:
                return self._json({"error": friendly_error(e)}, 502)
        if path == "/retry":
            nid = retry_job(str(data.get("id", "")))
            return self._json({"id": nid} if nid else {"error": "not retryable"}, 200 if nid else 404)
        if path == "/scan":
            url = str(data.get("url", ""))
            if not re.match(r"^https?://", url, re.I):
                return self._json({"error": "bad url"}, 400)
            try:
                return self._json({"items": scan_page(url)})
            except Exception as e:
                return self._json({"error": str(e)[:200] or e.__class__.__name__}, 502)
        if path == "/cancel":
            return self._json({"ok": cancel_job(str(data.get("id", "")))})
        if path == "/clear":
            clear_finished()
            return self._json({"ok": True})
        if path == "/open":
            job = JOBS.get(str(data.get("id", "")))
            if not job:
                return self._json({"error": "not found"}, 404)
            try:
                android_view(job, share=bool(data.get("share")))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)
        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def start_server():
    """Bind first, then serve in a thread (so the port is listening on return)."""
    server = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"افتح: http://127.0.0.1:{PORT}  (Ctrl+C للإيقاف)")
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
