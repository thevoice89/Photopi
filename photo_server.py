#!/usr/bin/env python3
"""
Simple HTTP server for viewing photos from the Raspberry Pi Camera
Serves photos on the local network: gallery at /, remote control at /live.

The pages are self-contained (no external fonts or scripts), so they work on
the camera's hotspot, which has no Internet access.
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit, parse_qs, unquote
import os
import re
import shutil
import threading
from pathlib import Path
import socket
import json
import time
import datetime
import zipfile
import subprocess

PHOTOS_DIR = Path.home() / "photos"
PORT = 8080
UDP_PORT = 12345
SHARED_MEM_PREVIEW = "/tmp/camera_preview.jpg"
SHARED_MEM_STATUS = "/tmp/camera_status.json"
# Grid thumbnails, generated on first request: a 12 MP original is ~2 MB and
# the old gallery downloaded every one of them just to draw the grid.
THUMB_DIR = Path.home() / ".cache" / "photopi" / "thumbs"
THUMB_SIZE = 480
# The camera app keeps writing its status while it runs; older than this, it
# is considered stopped.
STATUS_STALE = 4.0
# Commands relayed to camera_app.py over UDP (anything else is refused)
COMMAND_RE = re.compile(r"^(START_REMOTE|STOP_REMOTE|HEARTBEAT|CAPTURE|WAKE|STANDBY|"
                        r"ISO_UP|ISO_DOWN|SHUTTER_UP|SHUTTER_DOWN|BW|PEAK|ZOOM)"
                        r"( [0-9.]{1,8}){0,2}$|^(ISO|SHUTTER|ZOOM_AT)( [0-9.]{1,8}){1,2}$")

# At most two thumbnails decode at once: the camera app needs the other cores.
_thumb_slots = threading.Semaphore(2)
_thumb_locks = {}
_thumb_locks_guard = threading.Lock()
# EXIF readouts by (name, mtime), for the viewer's info line
_exif_cache = {}

try:
    from PIL import Image
except ImportError:  # thumbnails fall back to the originals
    Image = None


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    daemon_threads = True


def photo_path(name):
    """Path of a photo from a client-supplied name, or None (no paths, only .jpg files here)."""
    name = Path(unquote(name)).name
    path = PHOTOS_DIR / name
    if not name or path.suffix.lower() != ".jpg" or not path.is_file():
        return None
    return path


def list_photos():
    photos = []
    for p in PHOTOS_DIR.glob("*.jpg"):
        try:
            st = p.stat()
        except OSError:
            continue
        photos.append({"name": p.name, "time": st.st_mtime, "size": st.st_size})
    photos.sort(key=lambda p: p["name"], reverse=True)
    return photos


def library_version():
    """Changes whenever a photo is added or removed (directory mtime)."""
    try:
        return str(PHOTOS_DIR.stat().st_mtime_ns)
    except OSError:
        return "0"


def thumbnail(path):
    """Cached JPEG thumbnail for `path` (JPEG draft mode decodes at 1/8 scale)."""
    out = THUMB_DIR / path.name
    try:
        if out.stat().st_mtime >= path.stat().st_mtime:
            return out
    except OSError:
        pass
    if Image is None:
        return path
    with _thumb_locks_guard:
        lock = _thumb_locks.setdefault(path.name, threading.Lock())
    with lock, _thumb_slots:
        if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
            return out
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as im:
            im.draft("RGB", (THUMB_SIZE, THUMB_SIZE))
            img = im.convert("RGB")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
        tmp = out.with_name(out.name + ".tmp")
        img.save(tmp, "JPEG", quality=82)
        os.replace(tmp, out)
    with _thumb_locks_guard:
        _thumb_locks.pop(path.name, None)
    return out


def photo_info(path):
    """Exposure, ISO and size from EXIF (cached)."""
    st = path.stat()
    key = (path.name, st.st_mtime)
    info = _exif_cache.get(key)
    if info is not None:
        return info
    info = {"name": path.name, "size": st.st_size, "time": st.st_mtime}
    if Image is not None:
        try:
            with Image.open(path) as im:
                info["width"], info["height"] = im.size
                exif = im.getexif()
                sub = exif.get_ifd(0x8769)
                if sub.get(33434):
                    exposure = float(sub[33434])
                    info["exposure"] = (f"1/{round(1 / exposure)} s" if exposure < 0.5
                                        else f"{exposure:.1f} s")
                if sub.get(34855):
                    info["iso"] = int(sub[34855])
                stamp = sub.get(36867) or exif.get(306)
                if stamp:
                    info["taken"] = str(stamp)
        except Exception as e:
            print(f"EXIF error for {path.name}: {e}")
    if len(_exif_cache) > 512:
        _exif_cache.clear()
    _exif_cache[key] = info
    return info


def remove_thumbnail(name):
    try:
        (THUMB_DIR / name).unlink()
    except OSError:
        pass


def camera_status():
    """The camera app's status file, plus whether it is fresh."""
    try:
        with open(SHARED_MEM_STATUS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"online": False}
    age = time.time() - data.get("t", 0)
    data["online"] = age < STATUS_STALE
    data["age"] = round(age, 1)
    return data


class _ZipStream:
    """Write-only file object for zipfile that streams into the HTTP response."""

    def __init__(self, wfile):
        self.wfile = wfile
        self.offset = 0

    def write(self, data):
        self.wfile.write(data)
        self.offset += len(data)
        return len(data)

    def tell(self):
        return self.offset

    def flush(self):
        self.wfile.flush()


class PhotoHandler(SimpleHTTPRequestHandler):
    """Custom handler to serve photos"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PHOTOS_DIR), **kwargs)

    def log_message(self, fmt, *args):
        # Thumbnails, status polls and stream frames would flood the journal
        if not self.path.startswith(("/thumb/", "/api/status", "/api/photos", "/api/command")):
            super().log_message(fmt, *args)

    def end_headers(self):
        if self.path.startswith(("/api/", "/stream.mjpg", "/preview.jpg")) or self.path in ("/", "/live"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # ---------------- responses ----------------

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type="image/jpeg", cache=True):
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                if cache:
                    self.send_header("Cache-Control", "public, max-age=604800")
                self.end_headers()
                shutil.copyfileobj(f, self.wfile)
        except OSError:
            self.send_error(404, "File not found")

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length).decode("utf-8") if length else ""

    def read_json(self):
        try:
            return json.loads(self.read_body() or "{}")
        except ValueError:
            return {}

    # ---------------- GET ----------------

    def do_GET(self):
        """Handle GET requests"""
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/":
            self.send_html(GALLERY_HTML)
        elif path == "/live":
            self.send_html(LIVE_HTML)
        elif path == "/api/status":
            self.send_json(camera_status())
        elif path == "/api/photos":
            version = library_version()
            if query.get("v", [""])[0] == version:
                self.send_json({"version": version, "unchanged": True})
                return
            try:
                disk = shutil.disk_usage(PHOTOS_DIR)
                free, total = disk.free, disk.total
            except OSError:
                free = total = None
            self.send_json({"version": version, "photos": list_photos(), "free": free, "total": total})
        elif path.startswith("/api/info/"):
            photo = photo_path(path[len("/api/info/"):])
            if photo is None:
                self.send_error(404, "Photo not found")
            else:
                self.send_json(photo_info(photo))
        elif path.startswith("/thumb/"):
            photo = photo_path(path[len("/thumb/"):])
            if photo is None:
                self.send_error(404, "Photo not found")
                return
            try:
                self.send_file(thumbnail(photo))
            except Exception as e:
                print(f"Thumbnail error for {photo.name}: {e}")
                self.send_file(photo)
        elif path == "/stream.mjpg":
            self.serve_mjpeg_stream()
        elif path == "/preview.jpg":
            self.send_file(Path(SHARED_MEM_PREVIEW), cache=False)
        else:
            if path.lower().endswith(".jpg") and "download" in query:
                photo = photo_path(path)
                if photo is None:
                    self.send_error(404, "Photo not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(photo.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{photo.name}"')
                self.end_headers()
                with open(photo, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            # Fallback to serving files (the originals)
            super().do_GET()

    # ---------------- POST ----------------

    def do_POST(self):
        """Handle POST requests (for delete, download zip, commands, and system control)"""
        path = urlsplit(self.path).path
        if path.startswith('/delete/'):
            # Single photo, from a plain form (kept for old bookmarks)
            photo = photo_path(path[len('/delete/'):])
            if photo is None:
                self.send_error(404, "File not found")
                return
            try:
                photo.unlink()
                remove_thumbnail(photo.name)
                print(f"Deleted file: {photo.name}")
                self.send_response(303)
                self.send_header('Location', '/')
                self.end_headers()
            except OSError as e:
                self.send_error(500, f"Error deleting file: {e}")

        elif path in ('/api/delete', '/delete_multiple'):
            data = self.read_json()
            deleted, errors = [], []
            for name in data.get('files', []):
                photo = photo_path(str(name))
                if photo is None:
                    errors.append(name)
                    continue
                try:
                    photo.unlink()
                    remove_thumbnail(photo.name)
                    deleted.append(photo.name)
                    print(f"Deleted: {photo.name}")
                except OSError:
                    errors.append(name)
            self.send_json({'deleted': deleted, 'errors': errors})

        elif path == '/download_zip':
            # Form post (native download, nothing buffered in the browser) or JSON
            body = self.read_body()
            if self.headers.get("Content-Type", "").startswith("application/json"):
                try:
                    names = json.loads(body or "{}").get("files", [])
                except ValueError:
                    names = []
            else:
                names = parse_qs(body).get("files", [])
            photos = [p for p in (photo_path(str(n)) for n in names) if p is not None]
            if not photos:
                self.send_error(400, "No photos selected")
                return
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', f'attachment; filename="foto_{ts}.zip"')
            # No Content-Length: the archive is streamed and the connection
            # closed at the end. JPEGs are stored, not deflated (they do not
            # compress), and nothing is held in memory: the old version built
            # the whole ZIP in RAM, which a few dozen 2 MB photos could exhaust
            # on a Pi Zero.
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                with zipfile.ZipFile(_ZipStream(self.wfile), 'w', zipfile.ZIP_STORED) as zf:
                    for photo in photos:
                        zf.write(photo, photo.name)
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif path == '/system/shutdown':
            self.send_json({"status": "shutting down"})
            print("Shutdown requested via web")
            subprocess.Popen(['sudo', 'shutdown', 'now'])

        elif path == '/system/reboot':
            self.send_json({"status": "rebooting"})
            print("Reboot requested via web")
            subprocess.Popen(['sudo', 'reboot'])

        elif path == '/api/command':
            command = str(self.read_json().get('command', '')).strip()
            if not COMMAND_RE.match(command):
                self.send_error(400, "Unknown command")
                return
            self.send_udp_command(command)
            self.send_json({"status": "ok"})

        else:
            self.send_error(404, "Not found")

    def send_udp_command(self, command):
        """Send command to camera app via UDP"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(command.encode('utf-8'), ('127.0.0.1', UDP_PORT))
        except Exception as e:
            print(f"UDP Error: {e}")

    def serve_mjpeg_stream(self):
        """Serve MJPEG stream from shared memory.

        A frame is sent only when the camera app has written a new one; the
        app encodes them only while someone is watching, so the stream checks
        in every few seconds.
        """
        self.send_response(200)
        self.send_header('Age', '0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
        self.end_headers()

        last_mtime = None
        last_ping = 0.0
        last_sent = 0.0
        frame = None
        try:
            while True:
                now = time.monotonic()
                if now - last_ping > 3.0:
                    last_ping = now
                    self.send_udp_command("STREAM")
                try:
                    mtime = os.stat(SHARED_MEM_PREVIEW).st_mtime_ns
                except OSError:
                    mtime = None
                if mtime is not None and mtime != last_mtime:
                    last_mtime = mtime
                    with open(SHARED_MEM_PREVIEW, 'rb') as f:
                        frame = f.read()
                elif frame is None or now - last_sent < 5.0:
                    time.sleep(0.04)
                    continue
                # (no new frame for 5 s, e.g. standby: resend the last one,
                # which also notices a client that has gone away)
                last_sent = now

                self.wfile.write(b'--FRAME\r\n')
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(frame))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b'\r\n')
        except Exception:
            pass  # Client disconnected


# ---------------------------------------------------------------------------
# Pages. One design with the camera's own screen: graphite surfaces, a single
# amber accent for active states, system fonts (nothing to download).
# ---------------------------------------------------------------------------

BASE_CSS = r"""
:root {
  --bg: #0b0c0f; --s1: #15171c; --s2: #1d2027; --s3: #2a2e37; --line: rgba(255,255,255,.08);
  --line2: rgba(255,255,255,.14); --text: #f5f6f8; --mid: #9aa1ae; --low: #686f7d;
  --accent: #ffb020; --accent-ink: #1a1204; --ok: #30c77b; --danger: #ee4444; --info: #3884e8;
  --radius: 16px; --ease: cubic-bezier(.2,.8,.2,1);
  --safe-b: env(safe-area-inset-bottom, 0px); --safe-t: env(safe-area-inset-top, 0px);
  color-scheme: dark;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body {
  font: 15px/1.4 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
button { font: inherit; color: inherit; }
.num { font-variant-numeric: tabular-nums; }
.label { font-size: 11px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--low); }
svg.i { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; flex: none; }
.icon-btn {
  width: 44px; height: 44px; border-radius: 50%; border: 1px solid var(--line); background: rgba(255,255,255,.05);
  display: inline-flex; align-items: center; justify-content: center; cursor: pointer; color: var(--text);
  transition: background .15s, transform .1s; text-decoration: none; flex: none;
}
.icon-btn:hover { background: rgba(255,255,255,.1); }
.icon-btn:active { transform: scale(.94); }
.icon-btn.on { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
.pill {
  display: inline-flex; align-items: center; gap: 8px; height: 32px; padding: 0 14px; border-radius: 999px;
  background: rgba(20,22,27,.72); border: 1px solid var(--line); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
  font-size: 13px; font-weight: 600; white-space: nowrap;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--low); flex: none; }
.dot.live { background: #ff4d4d; box-shadow: 0 0 0 0 rgba(255,77,77,.6); animation: pulse 1.8s infinite; }
.dot.ok { background: var(--ok); } .dot.warn { background: var(--accent); } .dot.off { background: var(--low); }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(255,77,77,.55); } 70% { box-shadow: 0 0 0 9px rgba(255,77,77,0); } 100% { box-shadow: 0 0 0 0 rgba(255,77,77,0); } }
.btn {
  height: 48px; padding: 0 20px; border-radius: 14px; border: 1px solid var(--line2); background: var(--s2);
  display: inline-flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer; font-weight: 600;
  transition: background .15s, transform .1s, opacity .15s; text-decoration: none; color: var(--text);
}
.btn:hover { background: var(--s3); }
.btn:active { transform: scale(.97); }
.btn:disabled { opacity: .4; pointer-events: none; }
.btn.primary { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
.btn.danger { background: var(--danger); color: #fff; border-color: transparent; }
.btn.ghost-danger { color: #ff8a8a; }
/* Toast */
.toast {
  position: fixed; left: 50%; bottom: calc(24px + var(--safe-b)); transform: translate(-50%, 20px); opacity: 0;
  pointer-events: none; transition: opacity .25s var(--ease), transform .25s var(--ease); z-index: 1000;
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
body.select .toast, .viewer.open ~ .toast { bottom: calc(92px + var(--safe-b)); }
/* Dialogs */
.scrim {
  position: fixed; inset: 0; background: rgba(5,6,8,.6); -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px);
  display: flex; align-items: flex-end; justify-content: center; padding: 16px; padding-bottom: calc(16px + var(--safe-b));
  opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 900;
}
.scrim.open { opacity: 1; pointer-events: auto; }
.sheet {
  width: 100%; max-width: 420px; background: var(--s1); border: 1px solid var(--line2); border-radius: 24px; padding: 20px;
  transform: translateY(24px); transition: transform .25s var(--ease); box-shadow: 0 24px 60px rgba(0,0,0,.5);
}
.scrim.open .sheet { transform: none; }
@media (min-width: 640px) { .scrim { align-items: center; } }
.sheet h3 { margin: 4px 0 6px; font-size: 19px; }
.sheet p { margin: 0 0 18px; color: var(--mid); }
.sheet .row { display: flex; gap: 10px; }
.sheet .row .btn { flex: 1; }
.option {
  width: 100%; display: flex; align-items: center; gap: 14px; padding: 14px; border-radius: 16px; border: 1px solid var(--line);
  background: var(--s2); cursor: pointer; text-align: left; margin-bottom: 10px; transition: background .15s;
}
.option:hover { background: var(--s3); }
.option .ic { width: 40px; height: 40px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: rgba(255,255,255,.06); }
.option b { display: block; font-size: 15px; }
.option span { color: var(--mid); font-size: 13px; }
"""

ICONS_JS = r"""
const ICON = {
  back: '<path d="M15 5l-7 7 7 7"/>',
  next: '<path d="M9 5l7 7-7 7"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  select: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8.5 12.2l2.4 2.4 4.6-4.9"/>',
  power: '<path d="M12 3v8"/><path d="M6.6 6.8a8 8 0 1 0 10.8 0"/>',
  reboot: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M5.2 18.8l1.4-1.4M17.4 6.6l1.4-1.4"/>',
  download: '<path d="M12 4v11"/><path d="M7 10.5l5 5 5-5"/><path d="M5 20h14"/>',
  trash: '<path d="M4 7h16"/><path d="M10 7V4.5h4V7"/><path d="M6.5 7l1 13h9l1-13"/><path d="M10 11v5.5M14 11v5.5"/>',
  camera: '<path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.8l1.5-2h4.4l1.5 2h1.8A2.5 2.5 0 0 1 20 8.5v8A2.5 2.5 0 0 1 17.5 19h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.5" r="3.5"/>',
  live: '<rect x="3" y="6.5" width="13" height="11" rx="2.5"/><path d="M16 11l5-3v8l-5-3"/>',
  photos: '<rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="8.5" cy="10" r="1.6"/><path d="M21 15.5l-5-5-8.5 8.5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.2"/>',
  bw: '<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/>',
  focus: '<path d="M4 9V6a2 2 0 0 1 2-2h3M15 4h3a2 2 0 0 1 2 2v3M20 15v3a2 2 0 0 1-2 2h-3M9 20H6a2 2 0 0 1-2-2v-3"/><circle cx="12" cy="12" r="2"/>',
  zoom: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3M11 8.5v5M8.5 11h5"/>',
  battery: '<rect x="3" y="7.5" width="16" height="9" rx="2.5"/><path d="M21.5 10.5v3"/>',
  wifi: '<path d="M2.5 9a14 14 0 0 1 19 0M5.8 12.4a9 9 0 0 1 12.4 0M9.2 15.8a4 4 0 0 1 5.6 0"/><circle cx="12" cy="19" r=".8" fill="currentColor"/>',
  grid: '<rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M9.2 3.5v17M14.8 3.5v17M3.5 9.2h17M3.5 14.8h17"/>'
};
function icon(name, cls) { return `<svg class="i ${cls || ''}" viewBox="0 0 24 24">${ICON[name]}</svg>`; }
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
document.querySelectorAll('[data-icon]').forEach(el => el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon)));
function toast(text, tone) {
  let t = document.getElementById('toast');
  t.innerHTML = `<span class="pill"><span class="dot ${tone || 'ok'}"></span>${text}</span>`;
  t.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove('show'), 2600);
}
function confirmDialog({title, body, ok, tone}) {
  return new Promise(resolve => {
    const scrim = document.getElementById('confirm');
    scrim.querySelector('h3').textContent = title;
    scrim.querySelector('p').textContent = body;
    const okBtn = scrim.querySelector('[data-ok]');
    okBtn.textContent = ok;
    okBtn.className = 'btn ' + (tone || 'primary');
    const done = v => { scrim.classList.remove('open'); scrim.onclick = okBtn.onclick = null; resolve(v); };
    okBtn.onclick = e => { e.stopPropagation(); done(true); };
    scrim.onclick = e => { if (e.target === scrim || e.target.hasAttribute('data-cancel')) done(false); };
    scrim.classList.add('open');
  });
}
async function command(cmd) {
  try {
    const r = await fetch('/api/command', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({command: cmd})});
    return r.ok;
  } catch (e) { return false; }
}
"""

CONFIRM_HTML = """
<div class="scrim" id="confirm"><div class="sheet" role="dialog">
  <h3></h3><p></p>
  <div class="row"><button class="btn" data-cancel>Annulla</button><button class="btn" data-ok></button></div>
</div></div>
<div class="toast" id="toast"></div>
"""

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='8' fill='%230b0c0f'/%3E%3Ccircle cx='16' cy='16' r='9' "
           "fill='none' stroke='%23ffb020' stroke-width='3'/%3E%3Ccircle cx='16' cy='16' r='3.5' fill='%23ffb020'/%3E%3C/svg%3E")

HEAD = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0c0f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="icon" href="__FAVICON__">
"""

GALLERY_HTML = HEAD.replace("__FAVICON__", FAVICON) + r"""<title>Galleria · Pi Camera</title>
<style>
""" + BASE_CSS + r"""
body { min-height: 100vh; padding-bottom: calc(110px + var(--safe-b)); }
.top {
  position: sticky; top: 0; z-index: 50; padding: calc(14px + var(--safe-t)) 16px 12px;
  background: linear-gradient(var(--bg) 70%, rgba(11,12,15,0)); display: flex; align-items: center; gap: 10px;
}
.top .title { flex: 1; min-width: 0; }
.top h1 { margin: 0; font-size: 26px; letter-spacing: -.01em; }
.top .sub { color: var(--mid); font-size: 13px; margin-top: 1px; }
.top.selecting h1 { font-size: 20px; }
.top, main { max-width: 1432px; margin-left: auto; margin-right: auto; }
.status {
  margin: 4px max(16px, calc((100% - 1400px) / 2)) 6px; padding: 14px 16px; border-radius: var(--radius); background: var(--s1); border: 1px solid var(--line);
  display: flex; align-items: center; gap: 12px;
}
.status .txt { flex: 1; min-width: 0; }
.status b { display: block; font-size: 15px; }
.status span { color: var(--mid); font-size: 13px; }
main { padding: 0 16px; }
.day { margin-top: 18px; }
.day h2 { position: sticky; top: calc(68px + var(--safe-t)); z-index: 5; margin: 0 0 8px; padding: 4px 0; font-size: 13px;
  font-weight: 600; letter-spacing: .04em; color: var(--mid); background: var(--bg); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); gap: 4px; }
@media (min-width: 700px) { .grid { grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 6px; } }
.tile {
  position: relative; aspect-ratio: 1; border-radius: 10px; overflow: hidden; background: var(--s2); cursor: pointer;
  border: 0; padding: 0; display: block; width: 100%;
}
.tile img { width: 100%; height: 100%; object-fit: cover; display: block; opacity: 0; transition: opacity .3s, transform .3s var(--ease); }
.tile img.ready { opacity: 1; }
@media (hover: hover) { .tile:hover img { transform: scale(1.04); } }
.tile .check {
  position: absolute; top: 7px; right: 7px; width: 24px; height: 24px; border-radius: 50%; border: 2px solid rgba(255,255,255,.9);
  background: rgba(0,0,0,.25); display: none; align-items: center; justify-content: center; color: var(--accent-ink);
}
.tile .check svg { width: 15px; height: 15px; stroke-width: 3; opacity: 0; }
body.select .tile .check { display: flex; }
.tile.sel .check { background: var(--accent); border-color: var(--accent); }
.tile.sel .check svg { opacity: 1; }
.tile.sel img { transform: scale(.88); border-radius: 8px; }
.empty { text-align: center; color: var(--mid); padding: 80px 24px; }
.empty .i { width: 56px; height: 56px; stroke-width: 1.3; color: var(--low); }
.empty b { display: block; color: var(--text); font-size: 18px; margin: 12px 0 4px; }
.fab {
  position: fixed; right: 18px; bottom: calc(22px + var(--safe-b)); height: 56px; padding: 0 22px 0 18px; border-radius: 18px;
  background: var(--accent); color: var(--accent-ink); display: flex; align-items: center; gap: 10px; font-weight: 700;
  text-decoration: none; box-shadow: 0 10px 30px rgba(255,176,32,.25), 0 4px 12px rgba(0,0,0,.4); z-index: 60;
  transition: transform .15s;
}
.fab:active { transform: scale(.96); }
.fab .dot { background: #c4120f; }
body.select .fab { display: none; }
.selbar {
  position: fixed; left: 12px; right: 12px; bottom: calc(14px + var(--safe-b)); z-index: 70; display: flex; gap: 10px;
  padding: 10px; border-radius: 20px; background: rgba(21,23,28,.86); border: 1px solid var(--line2);
  -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px); transform: translateY(140%); transition: transform .25s var(--ease);
  max-width: 520px; margin: 0 auto;
}
body.select .selbar { transform: none; }
.selbar .btn { flex: 1; }
/* Viewer */
.viewer { position: fixed; inset: 0; background: #000; z-index: 500; display: none; touch-action: pan-y; }
.viewer.open { display: block; }
.viewer .stage { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; }
.viewer img { max-width: 100%; max-height: 100%; object-fit: contain; user-select: none; -webkit-user-drag: none; transition: opacity .2s; }
.viewer .bar {
  position: absolute; left: 0; right: 0; display: flex; align-items: center; gap: 10px; padding: 12px 14px; z-index: 2;
  transition: opacity .2s;
}
.viewer .bar.t { top: 0; padding-top: calc(12px + var(--safe-t)); background: linear-gradient(rgba(0,0,0,.7), rgba(0,0,0,0)); }
.viewer .bar.b { bottom: 0; padding-bottom: calc(14px + var(--safe-b)); background: linear-gradient(rgba(0,0,0,0), rgba(0,0,0,.75)); justify-content: center; }
.viewer .meta { flex: 1; min-width: 0; }
.viewer .meta b { display: block; font-size: 15px; }
.viewer .meta span { color: rgba(255,255,255,.7); font-size: 12.5px; }
.viewer .icon-btn { background: rgba(255,255,255,.12); border-color: rgba(255,255,255,.12); }
.viewer .nav { position: absolute; top: 50%; transform: translateY(-50%); z-index: 2; }
.viewer .nav.l { left: 14px; } .viewer .nav.r { right: 14px; }
@media (hover: none) { .viewer .nav { display: none; } }
.viewer.chrome-off .bar, .viewer.chrome-off .nav { opacity: 0; pointer-events: none; }
.viewer .bar.b .btn { height: 44px; background: rgba(255,255,255,.12); border-color: rgba(255,255,255,.12); }
</style>
</head>
<body>
<header class="top" id="top">
  <div class="title"><h1 id="title">Galleria</h1><div class="sub num" id="sub">&nbsp;</div></div>
  <button class="icon-btn" id="btn-select" title="Seleziona" data-icon="select"></button>
  <button class="icon-btn" id="btn-power" title="Fotocamera" data-icon="power"></button>
</header>

<section class="status" id="status">
  <span class="dot" id="st-dot"></span>
  <div class="txt"><b id="st-title">Fotocamera</b><span id="st-sub" class="num">Connessione…</span></div>
</section>

<main id="main"></main>

<a class="fab" href="/live"><span class="dot live"></span>Telecomando</a>

<div class="selbar">
  <button class="btn" id="btn-all">Tutte</button>
  <button class="btn" id="btn-zip" data-icon="download" disabled>Scarica</button>
  <button class="btn ghost-danger" id="btn-del" data-icon="trash" disabled>Elimina</button>
</div>
<form id="zipform" method="post" action="/download_zip" style="display:none"></form>

<div class="viewer" id="viewer">
  <div class="stage" id="stage"><img id="v-img" alt=""></div>
  <div class="bar t">
    <button class="icon-btn" id="v-close" data-icon="close" title="Chiudi"></button>
    <div class="meta"><b id="v-title" class="num"></b><span id="v-sub" class="num"></span></div>
    <span class="pill num" id="v-count"></span>
  </div>
  <button class="icon-btn nav l" id="v-prev" data-icon="back" title="Precedente"></button>
  <button class="icon-btn nav r" id="v-next" data-icon="next" title="Successiva"></button>
  <div class="bar b">
    <a class="btn" id="v-dl" data-icon="download">Scarica</a>
    <button class="btn ghost-danger" id="v-del" data-icon="trash">Elimina</button>
  </div>
</div>

<div class="scrim" id="power"><div class="sheet" role="dialog">
  <h3>Fotocamera</h3><p id="pw-sub">Gestisci la fotocamera a distanza.</p>
  <button class="option" id="pw-sleep"><span class="ic" id="pw-sleep-ic"></span><div><b id="pw-sleep-t">Standby</b><span id="pw-sleep-s">Schermo e sensore spenti, rete attiva</span></div></button>
  <button class="option" id="pw-reboot"><span class="ic" style="color:var(--accent)" data-icon="reboot"></span><div><b>Riavvia</b><span>Torna operativa in circa mezzo minuto</span></div></button>
  <button class="option" id="pw-off"><span class="ic" style="color:var(--danger)" data-icon="power"></span><div><b>Spegni</b><span>Per riaccenderla serve ricollegare l'alimentazione</span></div></button>
  <div class="row"><button class="btn" data-cancel style="flex:1">Chiudi</button></div>
</div></div>
""" + CONFIRM_HTML + r"""
<script>
""" + ICONS_JS + r"""
const MONTHS = ['gennaio','febbraio','marzo','aprile','maggio','giugno','luglio','agosto','settembre','ottobre','novembre','dicembre'];
let photos = [], version = '', selecting = false, cam = {};
const selected = new Set();
const $ = id => document.getElementById(id);

function fmtSize(b) {
  if (b == null) return '';
  if (b >= 1e9) return (b / 1e9).toFixed(1).replace('.', ',') + ' GB';
  return (b / 1e6).toFixed(b >= 1e8 ? 0 : 1).replace('.', ',') + ' MB';
}
function photoDate(p) {
  const m = /(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/.exec(p.name);
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : new Date(p.time * 1000);
}
function dayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 864e5);
  if (diff === 0) return 'Oggi';
  if (diff === 1) return 'Ieri';
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== today.getFullYear() ? ' ' + d.getFullYear() : ''}`;
}
const pad = n => String(n).padStart(2, '0');

function render() {
  const main = $('main');
  if (!photos.length) {
    main.innerHTML = `<div class="empty">${icon('photos')}<b>Nessuna foto</b>Scatta con il pulsante della fotocamera o dal telecomando.</div>`;
    return;
  }
  const groups = [];
  let last = null;
  photos.forEach((p, i) => {
    const label = dayLabel(photoDate(p));
    if (label !== last) { groups.push({label, items: []}); last = label; }
    groups[groups.length - 1].items.push(i);
  });
  main.innerHTML = groups.map(g => `<section class="day"><h2>${g.label}</h2><div class="grid">${
    g.items.map(i => `<button class="tile${selected.has(photos[i].name) ? ' sel' : ''}" data-i="${i}" aria-label="${esc(photos[i].name)}">
      <img loading="lazy" decoding="async" src="/thumb/${encodeURIComponent(photos[i].name)}?v=${Math.round(photos[i].time)}" alt="" onload="this.classList.add('ready')">
      <span class="check">${icon('check')}</span></button>`).join('')}</div></section>`).join('');
}

function updateHeader(free, total) {
  const n = photos.length;
  if (selecting) {
    $('title').textContent = selected.size ? `${selected.size} selezionat${selected.size === 1 ? 'a' : 'e'}` : 'Seleziona foto';
  } else {
    $('title').textContent = 'Galleria';
  }
  if (free !== undefined) updateHeader.space = free != null ? ` · ${fmtSize(free)} liberi` : '';
  const size = photos.reduce((s, p) => s + p.size, 0);
  $('sub').textContent = `${n} foto · ${fmtSize(size)}${updateHeader.space || ''}`;
  $('btn-zip').disabled = $('btn-del').disabled = !selected.size;
  $('btn-all').textContent = selected.size === n && n ? 'Nessuna' : 'Tutte';
}

async function load(force) {
  try {
    const r = await fetch('/api/photos' + (force ? '' : '?v=' + version));
    const data = await r.json();
    if (data.unchanged) return;
    version = data.version;
    const names = new Set(data.photos.map(p => p.name));
    [...selected].forEach(n => names.has(n) || selected.delete(n));
    photos = data.photos;
    render();
    updateHeader(data.free, data.total);
    if (viewer.open) viewer.refresh();
  } catch (e) { /* offline: keep what we have */ }
}

// ---- selection
function setSelecting(on) {
  selecting = on;
  if (!on) selected.clear();
  document.body.classList.toggle('select', on);
  $('top').classList.toggle('selecting', on);
  $('btn-select').classList.toggle('on', on);
  document.querySelectorAll('.tile.sel').forEach(t => on || t.classList.remove('sel'));
  updateHeader();
}
$('btn-select').onclick = () => setSelecting(!selecting);
$('main').addEventListener('click', e => {
  const tile = e.target.closest('.tile');
  if (!tile) return;
  const i = +tile.dataset.i, name = photos[i].name;
  if (selecting) {
    selected.has(name) ? selected.delete(name) : selected.add(name);
    tile.classList.toggle('sel', selected.has(name));
    updateHeader();
  } else viewer.show(i);
});
$('btn-all').onclick = () => {
  const all = selected.size !== photos.length;
  selected.clear();
  if (all) photos.forEach(p => selected.add(p.name));
  document.querySelectorAll('.tile').forEach(t => t.classList.toggle('sel', all));
  updateHeader();
};
$('btn-zip').onclick = () => {
  const form = $('zipform');
  form.innerHTML = [...selected].map(n => `<input type="hidden" name="files" value="${esc(n)}">`).join('');
  form.submit();
  toast(`Preparazione di ${selected.size} foto…`, 'ok');
};
async function removePhotos(names) {
  const r = await fetch('/api/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({files: names})});
  const data = await r.json();
  if (data.errors.length) toast(`${data.errors.length} foto non eliminate`, 'warn');
  else toast(data.deleted.length === 1 ? 'Foto eliminata' : `${data.deleted.length} foto eliminate`, 'ok');
  await load(true);
  return data.deleted;
}
$('btn-del').onclick = async () => {
  const n = selected.size;
  if (!await confirmDialog({title: `Eliminare ${n} foto?`, body: 'Verranno cancellate dalla scheda della fotocamera. Non si può annullare.', ok: 'Elimina', tone: 'danger'})) return;
  await removePhotos([...selected]);
  setSelecting(false);
};

// ---- viewer
const viewer = {
  i: 0, open: false, el: $('viewer'), img: $('v-img'),
  show(i) {
    this.i = i; this.open = true;
    this.el.classList.add('open'); this.el.classList.remove('chrome-off');
    document.body.style.overflow = 'hidden';
    history.pushState({viewer: true}, '');
    this.refresh();
  },
  refresh() {
    if (!photos.length) return this.close(true);
    this.i = Math.min(this.i, photos.length - 1);
    const p = photos[this.i], d = photoDate(p);
    const url = '/' + encodeURIComponent(p.name);
    // the thumbnail is already cached: show it at once, then the full photo
    this.img.src = `/thumb/${encodeURIComponent(p.name)}?v=${Math.round(p.time)}`;
    const full = new Image();
    full.onload = () => { if (photos[this.i] === p) this.img.src = full.src; };
    full.src = url;
    $('v-title').textContent = `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    $('v-sub').textContent = fmtSize(p.size);
    $('v-count').textContent = `${this.i + 1} / ${photos.length}`;
    $('v-dl').href = url + '?download=1';
    fetch('/api/info/' + encodeURIComponent(p.name)).then(r => r.json()).then(info => {
      if (photos[this.i] !== p) return;
      const parts = [info.exposure, info.iso && 'ISO ' + info.iso, info.width && `${info.width}×${info.height}`, fmtSize(info.size)].filter(Boolean);
      $('v-sub').textContent = parts.join(' · ');
    }).catch(() => {});
    [this.i - 1, this.i + 1].forEach(j => { if (photos[j]) new Image().src = '/' + encodeURIComponent(photos[j].name); });
  },
  step(d) { if (photos.length) { this.i = (this.i + d + photos.length) % photos.length; this.refresh(); } },
  close(fromHistory) {
    if (!this.open) return;
    this.open = false; this.el.classList.remove('open'); document.body.style.overflow = '';
    if (!fromHistory && history.state && history.state.viewer) history.back();
  }
};
$('v-close').onclick = () => viewer.close();
$('v-prev').onclick = e => { e.stopPropagation(); viewer.step(-1); };
$('v-next').onclick = e => { e.stopPropagation(); viewer.step(1); };
$('v-del').onclick = async () => {
  const p = photos[viewer.i];
  if (!await confirmDialog({title: 'Eliminare la foto?', body: `${p.name} verrà cancellata dalla scheda della fotocamera.`, ok: 'Elimina', tone: 'danger'})) return;
  await removePhotos([p.name]);
};
window.addEventListener('popstate', () => viewer.close(true));
document.addEventListener('keydown', e => {
  if (!viewer.open) return;
  if (e.key === 'ArrowLeft') viewer.step(-1);
  else if (e.key === 'ArrowRight') viewer.step(1);
  else if (e.key === 'Escape') viewer.close();
});
(() => {  // swipe between photos, tap toggles the bars
  let x0 = null, y0 = 0, t0 = 0;
  const stage = $('stage');
  stage.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; t0 = Date.now(); }, {passive: true});
  stage.addEventListener('touchend', e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    x0 = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) viewer.step(dx < 0 ? 1 : -1);
    else if (Math.abs(dx) < 10 && Math.abs(dy) < 10 && Date.now() - t0 < 300) viewer.el.classList.toggle('chrome-off');
  });
  stage.addEventListener('click', e => { if (matchMedia('(hover: hover)').matches) viewer.el.classList.toggle('chrome-off'); });
})();

// ---- camera status and power
function describe(s) {
  if (!s.online) return ['off', 'Fotocamera non attiva', "L'app sul display non risponde"];
  const settings = `${s.shutter === 'Auto' ? (s.live_shutter || 'Auto') : s.shutter} · ISO ${s.iso === 'Auto' ? (s.live_iso || 'Auto') : s.iso}`;
  const batt = s.battery != null ? ` · ${s.battery}%` : '';
  if (s.status === 'standby') return ['warn', 'In standby', 'Schermo e sensore spenti' + batt];
  if (s.status === 'stalled') return ['warn', 'Il sensore non risponde', 'Controlla il cavo flat' + batt];
  if (s.status === 'nocamera') return ['warn', 'Fotocamera non rilevata', 'Spegni, ricollega il cavo flat e riaccendi' + batt];
  return ['ok', s.mode === 'remote' ? 'Pronta · telecomando aperto' : 'Pronta', settings + batt];
}
async function poll() {
  try { cam = await (await fetch('/api/status')).json(); } catch (e) { cam = {online: false}; }
  const [tone, title, sub] = describe(cam);
  $('st-dot').className = 'dot ' + tone;
  $('st-title').textContent = title;
  $('st-sub').textContent = sub;
  const asleep = cam.status === 'standby';
  $('pw-sleep-ic').innerHTML = icon(asleep ? 'sun' : 'moon');
  $('pw-sleep-ic').style.color = asleep ? 'var(--accent)' : 'var(--info)';
  $('pw-sleep-t').textContent = asleep ? 'Risveglia' : 'Standby';
  $('pw-sleep-s').textContent = asleep ? 'Riaccende schermo e sensore' : 'Schermo e sensore spenti, rete attiva';
  if (cam.last_capture && cam.last_capture !== poll.lastCapture) {
    if (poll.lastCapture !== undefined) load();
    poll.lastCapture = cam.last_capture;
  }
}
const power = $('power');
$('btn-power').onclick = () => power.classList.add('open');
power.onclick = e => { if (e.target === power || e.target.hasAttribute('data-cancel')) power.classList.remove('open'); };
$('pw-sleep').onclick = async () => {
  power.classList.remove('open');
  const wake = cam.status === 'standby';
  if (await command(wake ? 'WAKE' : 'STANDBY')) toast(wake ? 'Fotocamera riattivata' : 'Fotocamera in standby', 'ok');
  else toast('Fotocamera non raggiungibile', 'warn');
  setTimeout(poll, 600);
};
$('pw-reboot').onclick = async () => {
  power.classList.remove('open');
  if (!await confirmDialog({title: 'Riavviare la fotocamera?', body: 'La pagina tornerà disponibile tra circa mezzo minuto.', ok: 'Riavvia'})) return;
  await fetch('/system/reboot', {method: 'POST'}).catch(() => {});
  toast('Riavvio in corso…', 'warn');
};
$('pw-off').onclick = async () => {
  power.classList.remove('open');
  if (!await confirmDialog({title: 'Spegnere la fotocamera?', body: "Per riaccenderla dovrai scollegare e ricollegare l'alimentazione.", ok: 'Spegni', tone: 'danger'})) return;
  await fetch('/system/shutdown', {method: 'POST'}).catch(() => {});
  toast('Spegnimento in corso…', 'warn');
};

load(true);
poll();
setInterval(poll, 3000);
setInterval(() => document.hidden || load(), 5000);
</script>
</body>
</html>
"""

LIVE_HTML = HEAD.replace("__FAVICON__", FAVICON) + r"""<title>Telecomando · Pi Camera</title>
<style>
""" + BASE_CSS + r"""
html, body { height: 100%; overflow: hidden; overscroll-behavior: none; }
body { display: flex; flex-direction: column; }
.toast { top: calc(66px + var(--safe-t)); bottom: auto; transform: translate(-50%, -16px); }
.vf { position: relative; flex: 1; min-height: 0; background: #000; display: flex; align-items: center; justify-content: center;
  overflow: hidden; container-type: size; }
/* The 4:3 picture, as large as the viewfinder allows (so the grid lines up with it) */
.frame { position: relative; width: 100%; height: 100%; }
@supports (width: 1cqw) { .frame { width: min(100cqw, 133.333cqh); height: auto; aspect-ratio: 4 / 3; } }
.frame img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; background: #050506; }
.frame .grid3 { position: absolute; inset: 0; pointer-events: none; opacity: .0; transition: opacity .2s;
  background-image: linear-gradient(to right, rgba(255,255,255,.22) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,.22) 1px, transparent 1px);
  background-size: 33.333% 33.333%; background-position: -1px -1px; }
.vf.grid-on .grid3 { opacity: 1; }
.flash { position: absolute; inset: 0; background: #fff; opacity: 0; pointer-events: none; }
.flash.go { animation: flash .45s ease-out; }
@keyframes flash { 0% { opacity: .9; } 100% { opacity: 0; } }
.hud { position: absolute; left: 0; right: 0; display: flex; align-items: center; gap: 8px; padding: 12px; z-index: 3; pointer-events: none; }
.hud > * { pointer-events: auto; }
.hud.t { top: 0; padding-top: calc(12px + var(--safe-t)); background: linear-gradient(rgba(0,0,0,.55), rgba(0,0,0,0)); }
.hud .grow { flex: 1; }
.hud .icon-btn { background: rgba(20,22,27,.6); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
.readout { position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); z-index: 3; }
.readout .pill { gap: 12px; }
.readout .sep { width: 1px; height: 14px; background: var(--line2); }
.readout .tag { color: var(--accent); }
.state { position: absolute; inset: 0; z-index: 4; display: none; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px; text-align: center; padding: 24px; background: rgba(6,7,9,.78); -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px); }
.state.show { display: flex; }
.state .i { width: 44px; height: 44px; stroke-width: 1.5; color: var(--mid); }
.state b { font-size: 18px; }
.state span { color: var(--mid); max-width: 300px; }
.state .btn { margin-top: 8px; }
.zoombadge { position: absolute; top: calc(66px + var(--safe-t)); left: 12px; z-index: 3; display: none; }
.vf.focus .zoombadge { display: block; }
.vf.focus .frame { cursor: zoom-in; }
/* Controls */
.panel { flex: none; background: var(--s1); border-top: 1px solid var(--line); padding: 14px 0 calc(16px + var(--safe-b)); }
.picker { padding: 0 0 12px; }
.picker .head { display: flex; align-items: baseline; justify-content: space-between; padding: 0 18px 7px; }
.picker .val { font-weight: 700; font-size: 15px; }
.picker .val.auto { color: var(--accent); }
.track { display: flex; gap: 6px; overflow-x: auto; scroll-snap-type: x proximity; padding: 2px 18px 4px; scrollbar-width: none; }
.track::-webkit-scrollbar { display: none; }
.chip { flex: none; height: 38px; min-width: 52px; padding: 0 13px; border-radius: 12px; border: 1px solid var(--line); background: var(--s2);
  color: var(--text); font-weight: 600; font-size: 14px; cursor: pointer; scroll-snap-align: center; transition: background .15s, color .15s, transform .1s; }
.chip:active { transform: scale(.95); }
.chip.on { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
.focusbar { display: none; align-items: center; gap: 12px; padding: 0 18px 12px; }
.vf-focus-on .focusbar { display: flex; }
.seg { display: flex; background: var(--s2); border: 1px solid var(--line); border-radius: 12px; padding: 3px; gap: 3px; }
.seg button { height: 32px; min-width: 44px; border: 0; border-radius: 9px; background: transparent; color: var(--mid); font-weight: 700; cursor: pointer; }
.seg button.on { background: var(--s3); color: var(--accent); }
.meter { flex: 1; height: 8px; border-radius: 4px; background: var(--s3); overflow: hidden; }
.meter i { display: block; height: 100%; width: 0; background: var(--mid); border-radius: 4px; transition: width .25s, background .25s; }
.meter i.peak { background: var(--accent); }
.deck { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; padding: 4px 22px 0; }
.deck .side { display: flex; gap: 10px; align-items: center; }
.deck .side.r { justify-content: flex-end; }
.thumb { width: 52px; height: 52px; border-radius: 14px; overflow: hidden; border: 2px solid var(--line2); background: var(--s2); display: block; flex: none; }
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.toggle { display: flex; flex-direction: column; align-items: center; gap: 4px; background: none; border: 0; cursor: pointer; color: var(--mid); padding: 0; font-size: 10.5px; font-weight: 700; letter-spacing: .08em; }
.toggle .icon-btn { pointer-events: none; }
.toggle.on { color: var(--accent); }
.toggle.on .icon-btn { background: var(--accent); color: var(--accent-ink); border-color: transparent; }
.shutter { width: 78px; height: 78px; border-radius: 50%; border: 0; padding: 0; cursor: pointer; background: transparent; position: relative; }
.shutter::before { content: ""; position: absolute; inset: 0; border-radius: 50%; border: 4px solid #fff; }
.shutter::after { content: ""; position: absolute; inset: 9px; border-radius: 50%; background: #fff; transition: transform .12s, background .2s; }
.shutter:active::after { transform: scale(.88); }
.shutter.busy::after { background: rgba(255,255,255,.35); transform: scale(.8); }
.shutter.busy::before { border-color: rgba(255,255,255,.25); border-top-color: var(--accent); animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.shutter:disabled { opacity: .35; }
/* Landscape phones and desktops: viewfinder left, controls right */
@media (orientation: landscape) and (min-width: 600px) {
  body { flex-direction: row; }
  .panel { width: 340px; border-top: 0; border-left: 1px solid var(--line); overflow-y: auto; padding: calc(14px + var(--safe-t)) 0 14px;
    display: flex; flex-direction: column; justify-content: center; }
  .deck { padding-top: 10px; }
}
@media (orientation: portrait) and (min-width: 600px) {
  .panel > * { max-width: 620px; margin-left: auto; margin-right: auto; }
}
@media (orientation: landscape) and (max-height: 430px) {
  .picker { padding-bottom: 8px; } .chip { height: 34px; } .shutter { width: 66px; height: 66px; }
}
</style>
</head>
<body>
<div class="vf" id="vf">
  <div class="frame" id="frame">
    <img id="stream" alt="Anteprima dal vivo">
    <div class="grid3"></div>
    <div class="flash" id="flash"></div>
  </div>
  <div class="hud t">
    <a class="icon-btn" href="/" title="Galleria" data-icon="back"></a>
    <span class="pill" id="live-pill"><span class="dot" id="live-dot"></span><span id="live-txt">Connessione…</span></span>
    <span class="grow"></span>
    <span class="pill num" id="batt" style="display:none"></span>
    <button class="icon-btn" id="btn-grid" title="Griglia" data-icon="grid"></button>
  </div>
  <span class="pill num zoombadge" id="zoombadge">Tocca l'immagine per ingrandire</span>
  <div class="readout"><span class="pill num" id="readout">–</span></div>
  <div class="state" id="state"><span id="state-ic"></span><b id="state-title"></b><span id="state-sub"></span>
    <button class="btn primary" id="state-btn" style="display:none"></button></div>
</div>

<div class="panel" id="panel">
  <div class="picker"><div class="head"><span class="label">Otturatore</span><span class="val num" id="sh-val">–</span></div>
    <div class="track" id="sh-track"></div></div>
  <div class="picker"><div class="head"><span class="label">ISO</span><span class="val num" id="iso-val">–</span></div>
    <div class="track" id="iso-track"></div></div>
  <div class="focusbar">
    <div class="seg" id="zoomseg"><button data-z="1">×1</button><button data-z="3">×3</button><button data-z="6">×6</button></div>
    <span class="label">Focus</span><div class="meter"><i id="meter"></i></div>
  </div>
  <div class="deck">
    <div class="side"><a class="thumb" href="/" id="thumb" title="Ultima foto"></a>
      <button class="toggle" id="t-bw"><span class="icon-btn" data-icon="bw"></span>B/N</button></div>
    <button class="shutter" id="shutter" title="Scatta" aria-label="Scatta"></button>
    <div class="side r"><button class="toggle" id="t-peak"><span class="icon-btn" data-icon="focus"></span>FOCUS</button></div>
  </div>
</div>
""" + CONFIRM_HTML + r"""
<script>
""" + ICONS_JS + r"""
const $ = id => document.getElementById(id);
let st = {}, built = false, pending = false, shutterWait = false;

function chips(track, values, current, cmd) {
  if (track.dataset.n != values.length) {
    track.innerHTML = values.map((v, i) => `<button class="chip num" data-i="${i}">${v}</button>`).join('');
    track.dataset.n = values.length;
    track.onclick = e => {
      const b = e.target.closest('.chip');
      if (!b) return;
      track.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c === b));
      command(`${cmd} ${b.dataset.i}`);
      pending = Date.now();
    };
  }
  const prev = track.querySelector('.chip.on');
  const now = track.children[current];
  if (prev !== now) {
    if (prev) prev.classList.remove('on');
    if (now) {
      now.classList.add('on');
      now.scrollIntoView({block: 'nearest', inline: 'center', behavior: built ? 'smooth' : 'auto'});
    }
  }
}

function setState(kind) {
  const el = $('state');
  const btn = $('state-btn');
  if (!kind) { el.classList.remove('show'); return; }
  const map = {
    offline: ['camera', 'Fotocamera non raggiungibile', "L'app sul display non risponde. Riprovo…", null],
    standby: ['moon', 'Fotocamera in standby', 'Schermo e sensore sono spenti.', 'Risveglia'],
    stalled: ['camera', 'Il sensore non invia immagini', 'Controlla il cavo flat. La fotocamera riprova da sola.', null],
    nocamera: ['camera', 'Fotocamera non rilevata', 'Spegni, ricollega il cavo flat della fotocamera e riaccendi. Galleria e menu funzionano.', null],
  }[kind];
  $('state-ic').innerHTML = icon(map[0]);
  $('state-title').textContent = map[1];
  $('state-sub').textContent = map[2];
  btn.style.display = map[3] ? '' : 'none';
  btn.textContent = map[3] || '';
  el.classList.add('show');
}
$('state-btn').onclick = async () => { $('state-btn').disabled = true; await command('WAKE'); setTimeout(() => $('state-btn').disabled = false, 1500); };

function update(s) {
  st = s;
  const online = s.online, sleeping = s.status === 'standby';
  $('live-dot').className = 'dot ' + (online && !sleeping ? 'live' : online ? 'warn' : 'off');
  $('live-txt').textContent = !online ? 'Offline' : sleeping ? 'Standby' : 'Live';
  setState(!online ? 'offline' : sleeping ? 'standby' : s.status === 'stalled' ? 'stalled'
           : s.status === 'nocamera' ? 'nocamera' : null);
  if (!online) return;
  // Pickers (skip while a tap is in flight, so they do not jump back)
  if (!pending || Date.now() - pending > 1200) {
    chips($('sh-track'), s.shutter_values, s.shutter_index, 'SHUTTER');
    chips($('iso-track'), s.iso_values, s.iso_index, 'ISO');
    pending = false;
  }
  const shAuto = s.shutter === 'Auto', isoAuto = s.iso === 'Auto';
  $('sh-val').textContent = shAuto ? `Auto · ${s.live_shutter || '–'}` : s.shutter;
  $('sh-val').classList.toggle('auto', shAuto);
  $('iso-val').textContent = isoAuto ? `Auto · ${s.live_iso || '–'}` : s.iso;
  $('iso-val').classList.toggle('auto', isoAuto);
  const tags = [];
  if (s.bw) tags.push('<span class="tag">B/N</span>');
  if (s.peaking) tags.push(`<span class="tag">FOCUS ×${s.zoom}</span>`);
  $('readout').innerHTML = [`${shAuto ? (s.live_shutter || 'Auto') : s.shutter}`, `ISO ${isoAuto ? (s.live_iso || 'Auto') : s.iso}`, ...tags]
    .join('<span class="sep"></span>');
  $('t-bw').classList.toggle('on', !!s.bw);
  $('t-peak').classList.toggle('on', !!s.peaking);
  $('vf').classList.toggle('focus', !!s.peaking);
  document.body.classList.toggle('vf-focus-on', !!s.peaking);
  $('zoombadge').textContent = s.zoom > 1 ? `×${s.zoom} · tocca per spostare` : "Tocca l'immagine per ingrandire";
  document.querySelectorAll('#zoomseg button').forEach(b => b.classList.toggle('on', +b.dataset.z === s.zoom));
  const meter = $('meter');
  meter.style.width = s.focus != null ? Math.round(s.focus * 100) + '%' : '0';
  meter.classList.toggle('peak', s.focus != null && s.focus > .93);
  if (s.battery != null) { $('batt').style.display = ''; $('batt').innerHTML = icon('battery') + ` ${s.battery}%`; }
  if (s.last_capture && s.last_capture !== update.last) {
    if (update.last !== undefined && shutterWait) toast('Foto salvata', 'ok');
    shutterWait = false;
    update.last = s.last_capture;
    $('thumb').innerHTML = `<img src="/thumb/${encodeURIComponent(s.last_capture)}" alt="">`;
  }
  const sh = $('shutter');
  sh.classList.toggle('busy', !!s.capturing || shutterWait);
  sh.disabled = sleeping || s.status === 'nocamera';
  built = true;
}

$('shutter').onclick = async () => {
  if (shutterWait) return;
  if (navigator.vibrate) navigator.vibrate(12);
  const flash = $('flash');
  flash.classList.remove('go'); void flash.offsetWidth; flash.classList.add('go');
  shutterWait = true;
  $('shutter').classList.add('busy');
  if (!await command('CAPTURE')) { shutterWait = false; toast('Fotocamera non raggiungibile', 'warn'); }
  setTimeout(() => { if (shutterWait) { shutterWait = false; $('shutter').classList.remove('busy'); } }, 12000);
};
$('t-bw').onclick = () => { command('BW'); $('t-bw').classList.toggle('on'); };
$('t-peak').onclick = () => { command('PEAK'); $('t-peak').classList.toggle('on'); };
$('btn-grid').onclick = () => { $('vf').classList.toggle('grid-on'); $('btn-grid').classList.toggle('on'); };
$('zoomseg').onclick = e => { const b = e.target.closest('button'); if (b) command('ZOOM ' + b.dataset.z); };
$('frame').addEventListener('click', e => {
  if (!st.peaking) return;
  // object-fit: contain - map the tap into the 4:3 picture
  const r = e.currentTarget.getBoundingClientRect();
  const scale = Math.min(r.width / 4, r.height / 3);
  const w = 4 * scale, h = 3 * scale;
  const u = (e.clientX - r.left - (r.width - w) / 2) / w, v = (e.clientY - r.top - (r.height - h) / 2) / h;
  if (u >= 0 && u <= 1 && v >= 0 && v <= 1) command(`ZOOM_AT ${u.toFixed(3)} ${v.toFixed(3)}`);
});

async function poll() {
  try { update(await (await fetch('/api/status')).json()); }
  catch (e) { update({online: false}); }
}
// The stream: reconnect when it drops (camera restart, Wi-Fi hiccup)
const img = $('stream');
function connect() { img.src = '/stream.mjpg?t=' + Date.now(); }
img.onerror = () => setTimeout(connect, 1500);
connect();
command('START_REMOTE');
setInterval(() => command('HEARTBEAT'), 3000);
fetch('/api/photos').then(r => r.json()).then(d => {
  if (d.photos && d.photos.length && !$('thumb').firstChild)
    $('thumb').innerHTML = `<img src="/thumb/${encodeURIComponent(d.photos[0].name)}" alt="">`;
}).catch(() => {});
poll();
setInterval(poll, 700);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { command('START_REMOTE'); connect(); poll(); } });
window.addEventListener('pagehide', () => navigator.sendBeacon && navigator.sendBeacon('/api/command', new Blob([JSON.stringify({command: 'STOP_REMOTE'})], {type: 'application/json'})));
</script>
</body>
</html>
"""


def warm_thumbnails():
    """Generate missing thumbnails in the background, newest first.

    Waits for the boot to settle, then works one photo at a time with pauses
    (the service already runs at low CPU and I/O priority), so the gallery
    opens with every thumbnail ready instead of decoding them on demand.
    """
    time.sleep(30)
    while True:
        for photo in list_photos():
            path = PHOTOS_DIR / photo["name"]
            try:
                if (THUMB_DIR / path.name).stat().st_mtime >= path.stat().st_mtime:
                    continue
            except OSError:
                pass
            try:
                thumbnail(path)
            except Exception as e:
                print(f"Thumbnail error for {path.name}: {e}")
            time.sleep(0.3)
        time.sleep(30)


def run_server():
    """Start the server"""
    server_address = ('', PORT)
    if Image is not None:
        threading.Thread(target=warm_thumbnails, daemon=True).start()
    try:
        # Use custom ThreadedHTTPServer
        httpd = ThreadedHTTPServer(server_address, PhotoHandler)
        print(f"Starting photo server on port {PORT}")
        httpd.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except:
            pass

if __name__ == '__main__':
    run_server()
