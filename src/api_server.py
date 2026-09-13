"""HTTP API service for the Turnstile relay.

Endpoints:
  POST /solve   {url, sitekey, timeout?}  -> long-poll, returns the token
  GET  /status  -> current/last task state
  GET  /healthz -> liveness probe

Single-task model: the container drives one Firefox window, so only one
solve may be in flight at a time; concurrent requests get 409.  The task is
communicated to mitmproxy via /config/relay/task.json, and the token comes
back via /config/relay/result.json (written by the mitmproxy addon).

Click-position calibration: xinput listeners watch the slave pointer
devices for raw button presses -- the only globally visible form of a
click on the X display -- and immediately read the pointer position via
XQueryPointer.  During a task, the first click's coordinates are
captured as the Turnstile checkbox position (the user teaches it by
clicking once), saved per-hostname in coords.json, and returned with
the token so future runs can automate the click.
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TASK_DIR = "/config/relay"
TASK_FILE = os.path.join(TASK_DIR, "task.json")
RESULT_FILE = os.path.join(TASK_DIR, "result.json")
COORDS_FILE = os.path.join(TASK_DIR, "coords.json")
CLICK_FILE = os.path.join(TASK_DIR, "last-click.json")
NAV_SCRIPT = "/opt/turnstile-relay/nav.sh"

HOST, PORT = "0.0.0.0", 8081
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 300
POLL_INTERVAL = 0.2
NAV_MAX_WAIT = 60  # seconds to wait for a usable Firefox window

_lock = threading.Lock()
_current = None  # dict of the in-flight task, or None


# --------------------------------------------------------------------------
# Click listener.  Normal button events are delivered to the window under
# the pointer (Firefox) and are invisible to xinput listeners; only RAW
# events, broadcast through the root window, are globally observable --
# and only from slave devices (masters emit none).  So we listen to the
# slave pointers that can produce clicks:
#   - "TigerVNC pointer":  a human clicking through the noVNC web UI
#   - "Virtual core XTEST pointer": injected clicks (xdotool, tests)
# Raw events carry no usable screen position (XTEST valuators are
# relative), so on every RawButtonPress we query the pointer position
# via XQueryPointer (xdotool getmouselocation), which is authoritative
# for both human and XTEST clicks.
# --------------------------------------------------------------------------

_click_lock = threading.Lock()
_last_click = None  # {"x": int, "y": int, "ts": float}
_click_during_task = None  # click captured while a task is running

POINTER_DEVICES = ("TigerVNC pointer", "Virtual core XTEST pointer")


def _pointer_position():
    """Current pointer position via XQueryPointer, or None."""
    try:
        r = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                           capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    x = y = None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("X="):
            x = int(line[2:])
        elif line.startswith("Y="):
            y = int(line[2:])
    if x is None or y is None:
        return None
    return x, y


def _record_click():
    """Register a click event: snapshot position and update state."""
    global _last_click, _click_during_task
    pos = _pointer_position()
    if pos is None:
        return
    click = {"x": pos[0], "y": pos[1], "ts": time.time()}
    with _click_lock:
        _last_click = click
        if _current is not None:
            _click_during_task = dict(click)
    # Also mirror to a file so mitmproxy (separate process) can report the
    # live click position on the injected page.
    try:
        _atomic_write_json(CLICK_FILE, click)
    except OSError:
        pass


def _device_listener(device):
    """Watch one slave pointer device for raw button presses."""
    while True:
        try:
            proc = subprocess.Popen(
                ["xinput", "test-xi2", device],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except OSError as e:
            print("[api] xinput not available (%s), click calibration disabled" % e,
                  flush=True)
            time.sleep(30)
            continue
        for line in proc.stdout:
            if line.startswith("EVENT type") and "(RawButtonPress)" in line:
                _record_click()
        rc = proc.wait()
        print("[api] xinput listener for %r exited rc=%s, restarting"
              % (device, rc), flush=True)
        time.sleep(2)  # respawn on unexpected exit


def _click_monitor():
    """Start one listener per slave pointer device."""
    for device in POINTER_DEVICES:
        print("[api] click listener starting for %r" % device, flush=True)
        threading.Thread(target=_device_listener, args=(device,),
                         daemon=True).start()


def _get_click_during_task():
    with _click_lock:
        return dict(_click_during_task) if _click_during_task else None


def _clear_click_during_task():
    global _click_during_task
    with _click_lock:
        _click_during_task = None


def _load_coords():
    return _read_json(COORDS_FILE) or {}


def _save_coord(hostname, click, sitekey):
    """Persist the calibrated checkbox position for a hostname."""
    coords = _load_coords()
    coords[hostname] = {
        "x": click["x"], "y": click["y"],
        "sitekey": sitekey,
        "display": os.environ.get("DISPLAY_WIDTH", ""),  # resolution context
        "ts": int(time.time()),
    }
    _atomic_write_json(COORDS_FILE, coords)


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _wait_for_window(deadline):
    """Return True once a visible Firefox window exists on the X display."""
    while time.time() < deadline:
        r = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", "firefox"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True
        time.sleep(1)
    return False


def _navigate(url):
    """Focus Firefox, focus the URL bar, type the URL, press Enter."""
    subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--class", "firefox",
         "windowactivate", "--sync"],
        capture_output=True, timeout=15,
    )
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+l"],
                   capture_output=True, timeout=10)
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--delay", "40", url],
        capture_output=True, timeout=30,
    )
    # Small pause so the URL bar finished processing the typed text.
    time.sleep(0.2)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"],
                   capture_output=True, timeout=10)


class Handler(BaseHTTPRequestHandler):
    server_version = "turnstile-relay/1.0"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("[api] %s %s" % (self.address_string(), fmt % args), flush=True)

    # ----- helpers -----------------------------------------------------

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _validate(self, payload):
        """Return (task, error_response)."""
        if not isinstance(payload, dict):
            return None, (400, {"ok": False, "error": "body must be a JSON object"})
        url = payload.get("url")
        sitekey = payload.get("sitekey")
        timeout = payload.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(url, str) or not isinstance(sitekey, str):
            return None, (400, {"ok": False, "error": "'url' and 'sitekey' are required strings"})
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return None, (400, {"ok": False, "error": "'timeout' must be an integer"})
        if not (5 <= timeout <= MAX_TIMEOUT):
            return None, (400, {"ok": False,
                                "error": "'timeout' must be between 5 and %d seconds" % MAX_TIMEOUT})
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            return None, (400, {"ok": False, "error": "'url' must be an absolute https:// URL"})
        if parts.hostname is None:
            return None, (400, {"ok": False, "error": "'url' has no hostname"})
        if not re.fullmatch(r"[0-9a-zA-Z.\-]+", sitekey):
            return None, (400, {"ok": False, "error": "invalid sitekey format"})
        task = {
            "task_id": uuid.uuid4().hex[:12],
            "url": url,
            "hostname": parts.hostname.lower(),
            "path": parts.path or "/",
            "sitekey": sitekey,
            "timeout": timeout,
            "created": time.time(),
        }
        return task, None

    def _solve(self, task):
        """Run one task to completion.  Caller holds _lock."""
        global _current
        _current = task
        _clear_click_during_task()
        coord = _load_coords().get(task["hostname"])
        started = time.time()
        try:
            # Clear any stale result, then publish the task for mitmproxy.
            for f in (RESULT_FILE, TASK_FILE):
                try:
                    os.remove(f)
                except OSError:
                    pass
            os.makedirs(TASK_DIR, exist_ok=True)
            _atomic_write_json(TASK_FILE, task)

            if not _wait_for_window(time.time() + NAV_MAX_WAIT):
                return 503, {"ok": False, "error": "no visible Firefox window"}
            try:
                _navigate(task["url"])
            except subprocess.TimeoutExpired:
                return 503, {"ok": False, "error": "xdotool navigation failed"}

            deadline = time.time() + task["timeout"]
            click = None
            while time.time() < deadline:
                result = _read_json(RESULT_FILE)
                if result and result.get("task_id") == task["task_id"]:
                    elapsed = round(time.time() - started, 2)
                    # The click that completed the challenge is the calibration
                    # sample: persist it for this hostname.
                    click = _get_click_during_task()
                    if click:
                        _save_coord(task["hostname"], click, task["sitekey"])
                    return 200, {"ok": True, "task_id": task["task_id"],
                                 "hostname": task["hostname"],
                                 "token": result["token"], "elapsed": elapsed,
                                 "click": click,
                                 "calibrated": bool(click)}
                time.sleep(POLL_INTERVAL)
            return 504, {"ok": False, "task_id": task["task_id"],
                         "error": "timed out waiting for token"}
        finally:
            try:
                os.remove(TASK_FILE)
            except OSError:
                pass
            _current = None

    # ----- routes ------------------------------------------------------

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/solve":
            self._send(404, {"ok": False, "error": "not found"})
            return
        payload = self._read_body()
        task, err = self._validate(payload)
        if err:
            self._send(err[0], err[1])
            return
        if not _lock.acquire(blocking=False):
            self._send(409, {"ok": False,
                             "error": "another solve task is in progress"})
            return
        try:
            code, obj = self._solve(task)
            self._send(code, obj)
        finally:
            _lock.release()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, {"ok": True})
        elif path == "/status":
            with _lock:
                cur = dict(_current) if _current else None
            last = _read_json(RESULT_FILE)
            with _click_lock:
                lc = dict(_last_click) if _last_click else None
            self._send(200, {"ok": True, "current": cur, "last_result": last,
                             "calibrated_click": lc,
                             "coords": _load_coords()})
        elif path == "/coords":
            self._send(200, {"ok": True, "coords": _load_coords()})
        else:
            self._send(404, {"ok": False, "error": "not found"})


def main():
    os.makedirs(TASK_DIR, exist_ok=True)
    threading.Thread(target=_click_monitor, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[api] listening on %s:%d" % (HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
