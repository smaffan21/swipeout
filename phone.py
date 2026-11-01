"""Phone (MTP) support via the Windows Shell. Paths look like  mtp:<Device>\\<Storage>\\folder\\file."""
import heapq
import os
import tempfile
import threading
import time

import pythoncom
import win32com.client
from win32com.shell import shell, shellcon

try:
    import wpd
except Exception:  # noqa: BLE001 - comtypes missing or WPD unavailable: Shell-only (slower) scanning
    wpd = None

PREFIX = "mtp:"
THIS_PC = 17
PREVIEW_DIR = os.path.join(tempfile.gettempdir(), "swipeout-preview")


def is_phone(path):
    return path.lower().startswith(PREFIX)


def _com():
    pythoncom.CoInitialize()
    return win32com.client.Dispatch("Shell.Application")


def _parts(vpath):
    return [p for p in vpath[len(PREFIX):].split("\\") if p]


def _join(*parts):
    head, *rest = parts
    return PREFIX + head + ("\\" + "\\".join(rest) if rest else "\\")


def _devices(sh):
    return [i for i in sh.Namespace(THIS_PC).Items() if not i.IsFileSystem and i.IsFolder]


def resolve(vpath, sh=None):
    """Return the Shell FolderItem for a phone path."""
    sh = sh or _com()
    parts = _parts(vpath)
    if not parts:
        raise FileNotFoundError("No device in path")
    cur = next((d for d in _devices(sh) if d.Name == parts[0]), None)
    if cur is None:
        raise FileNotFoundError(f"Phone '{parts[0]}' isn't connected (unlock it and pick File Transfer).")
    for name in parts[1:]:
        folder = cur.GetFolder
        nxt = folder.ParseName(name)
        if nxt is None or nxt.Name != name:
            nxt = next((i for i in folder.Items() if i.Name == name), None)
        if nxt is None:
            raise FileNotFoundError(f"'{name}' not found on the phone")
        cur = nxt
    return cur


def _shell_item(fi):
    return shell.SHCreateItemFromIDList(shell.SHGetIDListFromObject(fi._oleobj_), shell.IID_IShellItem)


def _size(fi):
    try:
        return int(fi.ExtendedProperty("System.Size") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _mtime(fi):
    try:
        d = fi.ExtendedProperty("System.DateModified")
        return d.timestamp() if d else 0
    except Exception:  # noqa: BLE001
        return 0


def _ext(name):
    return os.path.splitext(name)[1].lower()


# ---------------------------------------------------------------- info

def phones():
    sh = _com()
    out = []
    for d in _devices(sh):
        storages = []
        try:
            for s in d.GetFolder.Items():
                if not s.IsFolder:
                    continue
                cap = s.ExtendedProperty("System.Capacity")
                free = s.ExtendedProperty("System.FreeSpace")
                if cap is None:
                    continue
                path = _join(d.Name, s.Name)
                tops = {i.Name for i in s.GetFolder.Items() if i.IsFolder}
                spots = [n for n in ("DCIM", "Download", "Movies", "Pictures", "Music", "Recordings", "Documents") if n in tops]
                storages.append({"name": s.Name, "path": path, "total": int(cap), "free": int(free or 0),
                                 "used": int(cap) - int(free or 0),
                                 "spots": [{"label": n, "path": path.rstrip("\\") + "\\" + n} for n in spots]})
        except Exception:  # noqa: BLE001
            continue
        if storages:
            out.append({"name": d.Name, "path": _join(d.Name), "storages": storages})
    return out


# ---------------------------------------------------------------- safety

def protection_reason(vpath):
    parts = _parts(vpath)
    if len(parts) <= 2:
        return "That's the whole phone or a whole storage - open it and delete things inside."
    if len(parts) == 3 and parts[2].lower() == "android":
        return "The Android system folder holds every app's data. Open it to look, but it can't be deleted whole."
    return None


def caution(vpath):
    parts = [p.lower() for p in _parts(vpath)]
    if len(parts) >= 3 and parts[2] == "android":
        return "App data: deleting can reset or break that app (game downloads in 'obb' will re-download)."
    return None


# ---------------------------------------------------------------- scanning

class PhoneScanJob:
    """Progressive scan: the top-level list is available quickly, folder sizes fill in as they're weighed."""

    TOP_FILES = 500

    def __init__(self, root):
        parts = _parts(root)
        self.root = _join(*parts) if len(parts) == 1 else PREFIX + "\\".join(parts)
        self.cancelled = False
        self.done = False
        self.error = None
        self.files = 0
        self.bytes = 0
        self.current = ""
        self.started = time.time()
        self.lock = threading.Lock()
        self.children = None
        self.heap = []
        self.counter = 0
        self.errors = 0
        self.gone = set()

    @property
    def result(self):
        if self.children is None:
            return None
        with self.lock:
            kids = [dict(c) for c in self.children if c["path"].lower() not in self.gone]
            top = [dict(r) for _, _, r in sorted(self.heap, key=lambda x: x[0], reverse=True)
                   if not self._is_gone(r["path"])]
        kids.sort(key=lambda c: -c["size"])
        return {"root": self.root, "total": sum(c["size"] for c in kids), "cloud": 0, "errors": self.errors,
                "children": kids, "topFiles": top, "partial": not self.done, "phone": True,
                "elapsed": time.time() - self.started}

    def _is_gone(self, path):
        p = path.lower()
        return any(p == g or p.startswith(g + "\\") for g in self.gone)

    def forget(self, vpath):
        with self.lock:
            self.gone.add(vpath.lower().rstrip("\\"))

    def status(self):
        return {"root": self.root, "done": self.done, "error": self.error, "files": self.files,
                "bytes": self.bytes, "current": self.current, "elapsed": time.time() - self.started}

    def run(self):
        try:
            self._scan()
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        self.done = True

    def _record(self, name, size, path, mtime=0):
        return {"name": name, "path": path, "isDir": False, "size": size, "mtime": mtime,
                "ext": _ext(name), "cloud": False, "protected": protection_reason(path), "caution": caution(path)}

    def _push_top(self, rec):
        self.counter += 1
        item = (rec["size"], self.counter, rec)
        if len(self.heap) < self.TOP_FILES:
            heapq.heappush(self.heap, item)
        elif rec["size"] > self.heap[0][0]:
            heapq.heapreplace(self.heap, item)

    def _walker(self, sh):
        """Return (list_children, root_handle, close). list_children(handle) -> iterable of (name, is_dir, size, handle)."""
        parts = _parts(self.root)
        if wpd:
            try:
                dev_fi = next(d for d in _devices(sh) if d.Name == parts[0])
                dev = wpd.Device(wpd.pnp_id_from_shell_path(dev_fi.Path))
                root_id = dev.find(parts[1:])

                def list_wpd(obj_id):
                    for c in dev.children(obj_id):
                        try:
                            name, is_dir, size = dev.info(c)
                        except Exception:  # noqa: BLE001
                            self.errors += 1
                            continue
                        yield name, is_dir, size, c
                return list_wpd, root_id, dev.close
            except Exception:  # noqa: BLE001 - fall back to the (slower) Shell
                pass

        def list_shell(fi):
            for c in fi.GetFolder.Items():
                yield c.Name, bool(c.IsFolder), 0 if c.IsFolder else _size(c), c
        return list_shell, resolve(self.root, sh), lambda: None

    def _scan(self):
        sh = _com()
        base = self.root.rstrip("\\")
        list_children, root_handle, close = self._walker(sh)
        try:
            self._walk(base, list_children, root_handle, sh)
        finally:
            close()

    def _walk(self, base, list_children, root_handle, sh):
        kids = []
        for name, is_dir, size, handle in list_children(root_handle):
            path = base + "\\" + name
            if is_dir:
                kids.append({"name": name, "path": path, "isDir": True, "size": 0, "files": 0, "mtime": 0,
                             "ext": "", "cloud": False, "pending": True,
                             "protected": protection_reason(path), "caution": caution(path), "_h": handle})
            else:
                rec = self._record(name, size, path)
                kids.append(rec)
                self.files += 1
                self.bytes += size
                self._push_top(rec)
        if len(kids) <= 400:  # dates for the top level come from the Shell; too slow for huge folders
            try:
                dates = {fi.Name: _mtime(fi) for fi in resolve(self.root, sh).GetFolder.Items()}
                for c in kids:
                    c["mtime"] = dates.get(c["name"], 0)
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            self.children = [{k: v for k, v in c.items() if k != "_h"} for c in kids]
        by_path = {c["path"]: c for c in self.children}
        # the Android system folder is slow and rarely the answer, so weigh it last
        order = sorted((c for c in kids if c["isDir"]), key=lambda c: c["name"].lower() == "android")
        for c in order:
            live = by_path[c["path"]]
            stack = [(c["_h"], c["path"])]
            while stack:
                if self.cancelled:
                    raise RuntimeError("Scan cancelled")
                handle, path = stack.pop()
                self.current = path[len(PREFIX):]
                try:
                    for name, is_dir, size, sub in list_children(handle):
                        sub_path = path + "\\" + name
                        if is_dir:
                            stack.append((sub, sub_path))
                            continue
                        rec = self._record(name, size, sub_path)
                        with self.lock:
                            live["size"] += size
                            live["files"] += 1
                            self._push_top(rec)
                        self.files += 1
                        self.bytes += size
                        if self.cancelled:
                            raise RuntimeError("Scan cancelled")
                except RuntimeError:
                    raise
                except Exception:  # noqa: BLE001
                    self.errors += 1
            with self.lock:
                live["pending"] = False


# ---------------------------------------------------------------- file access

def stat(vpath):
    fi = resolve(vpath)
    return {"isDir": bool(fi.IsFolder), "size": _size(fi), "name": fi.Name}


def open_stream(vpath):
    """Return (IStream, size) for reading a phone file."""
    fi = resolve(vpath)
    if fi.IsFolder:
        raise IsADirectoryError(vpath)
    st = _shell_item(fi).BindToHandler(None, shell.BHID_Stream, pythoncom.IID_IStream)
    return st, _size(fi)


def read_head(vpath, n):
    st, _ = open_stream(vpath)
    return st.Read(n)


def peek(vpath):
    fi = resolve(vpath)
    base = vpath.rstrip("\\")
    if fi.IsFolder:
        items, images = [], []
        for c in fi.GetFolder.Items():
            is_dir = bool(c.IsFolder)
            size = 0 if is_dir else _size(c)
            items.append({"name": c.Name, "isDir": is_dir, "size": size})
            if not is_dir and _ext(c.Name) in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} \
                    and size < 12 * 1024 * 1024 and len(images) < 6:
                images.append(base + "\\" + c.Name)
            if len(items) >= 400:
                break
        items.sort(key=lambda i: (not i["isDir"], -i["size"]))
        return {"kind": "dir", "items": items[:40], "count": len(items), "images": images}
    raw = read_head(vpath, 6000)
    if b"\0" in raw[:2000]:
        return {"kind": "binary"}
    return {"kind": "text", "text": raw.decode("utf-8", errors="replace")}


def delete(vpath):
    """Permanently delete from the phone (MTP has no Recycle Bin)."""
    fi = resolve(vpath)
    op = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell.IID_IFileOperation)
    op.SetOperationFlags(shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI)
    op.DeleteItem(_shell_item(fi), None)
    op.PerformOperations()
    if op.GetAnyOperationsAborted():
        raise OSError("Cancelled - item was not deleted.")
    parent = resolve(vpath.rsplit("\\", 1)[0])
    if any(i.Name == fi.Name for i in parent.GetFolder.Items()):
        raise OSError("The phone refused to delete it (it may be in use or read-only).")


def copy_local(vpath):
    """Copy a phone file to a local temp folder (used to open it in its default app)."""
    st, size = open_stream(vpath)
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    dest = os.path.join(PREVIEW_DIR, _parts(vpath)[-1])
    with open(dest, "wb") as f:
        while True:
            chunk = st.Read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return dest


def reveal(vpath):
    """Open the containing folder of a phone item in Explorer."""
    parent = vpath.rsplit("\\", 1)[0] if len(_parts(vpath)) > 1 else vpath
    fi = resolve(parent)
    return fi.Path


def clear_preview_cache():
    if os.path.isdir(PREVIEW_DIR):
        for name in os.listdir(PREVIEW_DIR):
            try:
                os.remove(os.path.join(PREVIEW_DIR, name))
            except OSError:
                pass
