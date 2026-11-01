"""Declutter - local server. Run: python server.py"""
import ctypes
import heapq
import json
import mimetypes
import os
import shutil
import string
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import phone
except ImportError:  # pywin32 missing: PC-only mode
    phone = None

PORT = int(os.environ.get("DECLUTTER_PORT", "8765"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
HOME = os.path.expanduser("~")
TOP_FILES = 500

CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS

mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")


# ---------------------------------------------------------------- safety

def _norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


SYS_DRIVE = os.environ.get("SystemDrive", "C:")
PROTECTED_TREES = [_norm(p) for p in [
    os.environ.get("WINDIR", SYS_DRIVE + "\\Windows"),
    os.environ.get("ProgramFiles", SYS_DRIVE + "\\Program Files"),
    os.environ.get("ProgramFiles(x86)", SYS_DRIVE + "\\Program Files (x86)"),
    os.environ.get("ProgramData", SYS_DRIVE + "\\ProgramData"),
    SYS_DRIVE + "\\$Recycle.Bin",
    SYS_DRIVE + "\\System Volume Information",
    SYS_DRIVE + "\\Recovery",
    SYS_DRIVE + "\\Boot",
]]
PROTECTED_EXACT = {_norm(p) for p in [
    SYS_DRIVE + "\\Users", HOME,
    os.path.join(HOME, "AppData"), os.path.join(HOME, "AppData", "Local"),
    os.path.join(HOME, "AppData", "Roaming"), os.path.join(HOME, "AppData", "LocalLow"),
    os.path.join(HOME, "Desktop"), os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Downloads"), os.path.join(HOME, "Pictures"),
    os.path.join(HOME, "Videos"), os.path.join(HOME, "Music"),
    os.path.join(HOME, "OneDrive"),
]}
PROTECTED_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp", "ntuser.dat"}


def protection_reason(path):
    n = _norm(path)
    if len(n) <= 3 or os.path.dirname(n) == n:
        return "Drive roots can't be binned."
    if n in PROTECTED_EXACT:
        return "This is a core user folder - open it and bin things inside instead."
    for t in PROTECTED_TREES:
        if n == t or n.startswith(t + "\\"):
            return "System / installed-program files. Uninstall apps via Windows Settings instead."
    if os.path.basename(n) in PROTECTED_NAMES or os.path.basename(n).startswith("ntuser.dat"):
        return "Windows system file."
    return None


def caution(path):
    n = _norm(path)
    if n.startswith(_norm(os.path.join(HOME, "AppData")) + "\\"):
        return "AppData: apps store settings here. Temp/cache folders are usually safe; others may break an app."
    return None


# ---------------------------------------------------------------- windows shell

IS64 = struct.calcsize("P") == 8


class SHFILEOPSTRUCTW(ctypes.Structure):
    _pack_ = 8 if IS64 else 1
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


class SHQUERYRBINFO(ctypes.Structure):
    _pack_ = 8 if IS64 else 1
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


shell32 = ctypes.windll.shell32 if sys.platform == "win32" else None


def send_to_recycle_bin(path):
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x400, 0x4000
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError(f"Windows refused (code {rc:#x}). The file may be open in another program.")
    if op.fAnyOperationsAborted or os.path.exists(path):
        raise OSError("Cancelled - item was not removed.")


def recycle_bin_info():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(SHQUERYRBINFO)
    if shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return {"size": 0, "items": 0}
    return {"size": info.i64Size, "items": info.i64NumItems}


def empty_recycle_bin():
    shell32.SHEmptyRecycleBinW(None, None, 0)  # flags 0 -> Windows shows its own confirmation


# ---------------------------------------------------------------- scanning

def is_link(entry):
    try:
        return entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction())
    except OSError:
        return True


class ScanJob:
    def __init__(self, root):
        self.root = root
        self.cancelled = False
        self.done = False
        self.error = None
        self.files = 0
        self.bytes = 0
        self.current = ""
        self.result = None
        self.started = time.time()

    def status(self):
        return {"root": self.root, "done": self.done, "error": self.error, "files": self.files,
                "bytes": self.bytes, "current": self.current, "elapsed": time.time() - self.started}

    def run(self):
        try:
            self.result = self._scan()
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        self.done = True

    def _file_record(self, entry, st, cloud):
        return {"name": entry.name, "path": entry.path, "isDir": False, "size": 0 if cloud else st.st_size,
                "logical": st.st_size, "mtime": st.st_mtime, "ext": os.path.splitext(entry.name)[1].lower(),
                "cloud": cloud}

    def _scan(self):
        root = self.root
        heap = []  # min-heap of (size, counter, record)
        counter = 0
        children = []
        total = cloud_total = errors = 0

        def push_top(size, rec):
            nonlocal counter
            counter += 1
            item = (size, counter, rec)
            if len(heap) < TOP_FILES:
                heapq.heappush(heap, item)
            elif size > heap[0][0]:
                heapq.heapreplace(heap, item)

        with os.scandir(root) as it:
            top_entries = list(it)

        for entry in top_entries:
            if self.cancelled:
                raise RuntimeError("Scan cancelled")
            try:
                if is_link(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    size = cloud = nfiles = 0
                    latest = 0
                    stack = [entry.path]
                    while stack:
                        if self.cancelled:
                            raise RuntimeError("Scan cancelled")
                        d = stack.pop()
                        self.current = d
                        try:
                            with os.scandir(d) as sub:
                                for e in sub:
                                    try:
                                        if is_link(e):
                                            continue
                                        if e.is_dir(follow_symlinks=False):
                                            stack.append(e.path)
                                            continue
                                        st = e.stat(follow_symlinks=False)
                                        is_cloud = bool(getattr(st, "st_file_attributes", 0) & CLOUD_ATTRS)
                                        nfiles += 1
                                        self.files += 1
                                        latest = max(latest, st.st_mtime)
                                        if is_cloud:
                                            cloud += st.st_size
                                        else:
                                            size += st.st_size
                                            self.bytes += st.st_size
                                            push_top(st.st_size, self._file_record(e, st, False))
                                    except OSError:
                                        errors += 1
                        except OSError:
                            errors += 1
                    try:
                        mt = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        mt = latest
                    children.append({"name": entry.name, "path": entry.path, "isDir": True, "size": size,
                                     "logical": size + cloud, "files": nfiles, "mtime": max(mt, latest),
                                     "ext": "", "cloud": cloud > 0 and size == 0, "cloudBytes": cloud})
                    total += size
                    cloud_total += cloud
                else:
                    st = entry.stat(follow_symlinks=False)
                    is_cloud = bool(getattr(st, "st_file_attributes", 0) & CLOUD_ATTRS)
                    rec = self._file_record(entry, st, is_cloud)
                    children.append(rec)
                    self.files += 1
                    if is_cloud:
                        cloud_total += st.st_size
                    else:
                        total += st.st_size
                        self.bytes += st.st_size
                        push_top(st.st_size, rec)
            except OSError:
                errors += 1

        children.sort(key=lambda c: c["size"], reverse=True)
        top = [r for _, _, r in sorted(heap, key=lambda x: x[0], reverse=True)]
        for item in children + top:
            item["protected"] = protection_reason(item["path"])
            item["caution"] = caution(item["path"])
        return {"root": root, "total": total, "cloud": cloud_total, "errors": errors,
                "children": children, "topFiles": top, "elapsed": time.time() - self.started}


JOB = {"job": None}
JOB_LOCK = threading.Lock()


# ---------------------------------------------------------------- peeking

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg", ".ico"}


def peek(path):
    if os.path.isdir(path):
        items, images = [], []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        size = 0 if is_dir else e.stat(follow_symlinks=False).st_size
                        items.append({"name": e.name, "isDir": is_dir, "size": size})
                        if not is_dir and os.path.splitext(e.name)[1].lower() in IMAGE_EXT and len(images) < 6:
                            attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                            if not attrs & CLOUD_ATTRS:
                                images.append(e.path)
                    except OSError:
                        pass
                    if len(items) >= 400:
                        break
        except OSError as ex:
            return {"kind": "dir", "error": str(ex), "items": [], "images": []}
        items.sort(key=lambda i: (not i["isDir"], -i["size"]))
        return {"kind": "dir", "items": items[:40], "count": len(items), "images": images}
    ext = os.path.splitext(path)[1].lower()
    if ext in {".zip", ".jar", ".apk", ".docx", ".xlsx", ".pptx", ".nupkg", ".whl"}:
        try:
            with zipfile.ZipFile(path) as z:
                infos = sorted(z.infolist(), key=lambda i: -i.file_size)
                return {"kind": "zip", "count": len(infos),
                        "items": [{"name": i.filename, "size": i.file_size, "isDir": i.is_dir()} for i in infos[:40]]}
        except Exception as ex:  # noqa: BLE001
            return {"kind": "zip", "error": str(ex), "items": []}
    try:
        with open(path, "rb") as f:
            raw = f.read(6000)
        if b"\0" in raw[:2000]:
            return {"kind": "binary"}
        return {"kind": "text", "text": raw.decode("utf-8", errors="replace")}
    except OSError as ex:
        return {"kind": "error", "error": str(ex)}


# ---------------------------------------------------------------- http

def drives():
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            try:
                u = shutil.disk_usage(root)
                out.append({"path": root, "total": u.total, "used": u.used, "free": u.free})
            except OSError:
                pass
    return out


def quick_spots():
    spots = [("Downloads", "Downloads"), ("Desktop", "Desktop"), ("Videos", "Videos"), ("Documents", "Documents"),
             ("Pictures", "Pictures"), ("Music", "Music"), ("AppData Local", os.path.join("AppData", "Local")),
             ("Home", "")]
    out = [{"label": label, "path": os.path.join(HOME, rel) if rel else HOME} for label, rel in spots]
    out.append({"label": "Temp", "path": os.environ.get("TEMP", os.path.join(HOME, "AppData", "Local", "Temp"))})
    return [s for s in out if os.path.isdir(s["path"])]


PHONES = {"data": [], "ts": 0.0, "lock": threading.Lock()}


def list_phones(max_age=20.0):
    """Phone list, cached so a busy phone never stalls the page."""
    if not phone:
        return []
    if time.time() - PHONES["ts"] < max_age:
        return PHONES["data"]
    with PHONES["lock"]:
        if time.time() - PHONES["ts"] >= max_age:
            try:
                PHONES["data"] = phone.phones()
            except Exception:  # noqa: BLE001
                PHONES["data"] = []
            PHONES["ts"] = time.time()
    return PHONES["data"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = url.path
        if route == "/api/info":
            return self._json({"drives": drives(), "spots": quick_spots(), "bin": recycle_bin_info(), "home": HOME})
        if route == "/api/phones":
            return self._json({"phones": list_phones(0 if q.get("fresh") else 20.0)})
        if route == "/api/scan/status":
            job = JOB["job"]
            if not job:
                return self._json({"idle": True})
            st = job.status()
            result = job.result
            if result:
                st["result"] = result
            return self._json(st)
        if route == "/api/peek":
            path = q.get("path", "")
            if phone and phone.is_phone(path):
                try:
                    return self._json(phone.peek(path))
                except Exception as ex:  # noqa: BLE001
                    return self._json({"kind": "error", "error": str(ex)})
            return self._json(peek(path))
        if route == "/api/file":
            return self._send_file(q.get("path", ""))
        return self._static(route)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Declutter") != "1":
            return self._json({"error": "forbidden"}, 403)
        route = urlparse(self.path).path
        data = self._body()
        if route == "/api/scan":
            path = data.get("path", "")
            if phone and phone.is_phone(path):
                job = phone.PhoneScanJob(path)
            else:
                path = os.path.abspath(path)
                if not os.path.isdir(path):
                    return self._json({"error": "That folder doesn't exist."}, 400)
                job = ScanJob(path)
            with JOB_LOCK:
                if JOB["job"] and not JOB["job"].done:
                    JOB["job"].cancelled = True
                JOB["job"] = job
            threading.Thread(target=job.run, daemon=True).start()
            return self._json({"ok": True})
        if route == "/api/scan/cancel":
            if JOB["job"]:
                JOB["job"].cancelled = True
            return self._json({"ok": True})
        if route == "/api/trash":
            path = data.get("path", "")
            if phone and phone.is_phone(path):
                return self._delete_phone(path)
            reason = protection_reason(path)
            if reason:
                return self._json({"error": reason}, 400)
            if not os.path.lexists(path):
                return self._json({"error": "Already gone."}, 404)
            for attempt in range(3):
                try:
                    send_to_recycle_bin(path)
                    break
                except OSError as ex:
                    if attempt == 2 or "Cancelled" in str(ex):
                        return self._json({"error": str(ex)}, 500)
                    time.sleep(0.8)
            return self._json({"ok": True, "bin": recycle_bin_info(), "drives": drives()})
        if route == "/api/reveal":
            path = data.get("path", "")
            if phone and phone.is_phone(path):
                try:
                    subprocess.Popen(["explorer", phone.reveal(path)])
                except Exception as ex:  # noqa: BLE001
                    return self._json({"error": str(ex)}, 500)
            elif os.path.exists(path):
                subprocess.Popen(f'explorer /select,"{os.path.abspath(path)}"')
            return self._json({"ok": True})
        if route == "/api/open":
            path = data.get("path", "")
            if phone and phone.is_phone(path):
                try:
                    path = phone.copy_local(path)
                except Exception as ex:  # noqa: BLE001
                    return self._json({"error": str(ex)}, 500)
            if os.path.exists(path):
                os.startfile(path)  # noqa: S606
            return self._json({"ok": True})
        if route == "/api/empty-bin":
            empty_recycle_bin()
            return self._json({"ok": True, "bin": recycle_bin_info(), "drives": drives()})
        return self._json({"error": "not found"}, 404)

    def _delete_phone(self, path):
        reason = phone.protection_reason(path)
        if reason:
            return self._json({"error": reason}, 400)
        try:
            phone.delete(path)
        except FileNotFoundError:
            return self._json({"error": "Already gone."}, 404)
        except Exception as ex:  # noqa: BLE001
            return self._json({"error": str(ex)}, 500)
        job = JOB["job"]
        if job and hasattr(job, "forget"):
            job.forget(path)
        return self._json({"ok": True, "bin": recycle_bin_info(), "drives": drives(), "phones": list_phones(0)})

    def _static(self, route):
        rel = "index.html" if route in ("/", "") else route.lstrip("/")
        full = os.path.abspath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if phone and phone.is_phone(path):
            try:
                st, size = phone.open_stream(path)
            except Exception as ex:  # noqa: BLE001
                return self._json({"error": str(ex)}, 404)
            return self._stream(path, size, lambda n: st.Read(n), lambda pos: st.Seek(pos, 0), None)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        f = open(path, "rb")  # noqa: SIM115
        try:
            self._stream(path, os.path.getsize(path), f.read, f.seek, f)
        finally:
            f.close()

    def _stream(self, path, size, read, seek, _handle):
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].partition("-")
            if s:
                start = int(s)
                end = int(e) if e else end
            elif e:
                start = max(0, size - int(e))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1 if size else 0
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            if start:
                seek(start)
            remaining = length
            while remaining > 0:
                chunk = read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except Exception:  # noqa: BLE001 - client went away or the phone stream failed mid-read
            pass


def main():
    if phone:
        phone.clear_preview_cache()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Declutter running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
