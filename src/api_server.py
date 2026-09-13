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
import random
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
PAGE_FILE = os.path.join(TASK_DIR, "page.json")
READY_FILE = os.path.join(TASK_DIR, "widget-ready.json")
COORDS_FILE = os.path.join(TASK_DIR, "coords.json")
CLICK_FILE = os.path.join(TASK_DIR, "last-click.json")
NAV_SCRIPT = "/opt/turnstile-relay/nav.sh"

HOST, PORT = "0.0.0.0", 8081
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 300
POLL_INTERVAL = 0.2
NAV_MAX_WAIT = 60  # seconds to wait for a usable Firefox window
AUTO_CLICK = os.environ.get("AUTO_CLICK", "1") not in ("0", "false", "no")

_lock = threading.Lock()
_current = None  # dict of the in-flight task, or None


# --------------------------------------------------------------------------
# Click listener.  Normal button events are delivered to the window under
# the pointer (Firefox) and are invisible to xinput listeners; only RAW
# events, broadcast through the root window, are globally observable --
# and only from slave devices (masters emit none).  So we listen to every
# slave pointer device (e.g. "TigerVNC pointer" for a human clicking
# through the noVNC web UI, "Virtual core XTEST pointer" for injected
# xdotool clicks).  Raw events carry no usable screen position (XTEST
# valuators are relative), so on every RawButtonPress we query the
# pointer position via XQueryPointer (xdotool getmouselocation), which
# is authoritative for both human and XTEST clicks.
# --------------------------------------------------------------------------

_click_lock = threading.Lock()
_last_click = None  # {"x": int, "y": int, "ts": float}
_click_during_task = None  # click captured while a task is running
_self_click_until = 0.0  # clicks until this time are ours: not calibration


def _slave_pointer_devices():
    """Names of slave pointer devices -- the only ones with raw events."""
    try:
        r = subprocess.run(["xinput", "list"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        # Lines look like "⎜   ↳ TigerVNC pointer\tid=6\t[slave  pointer  (2)]"
        # -- strip any tree-drawing glyphs before the device name.
        m = re.match(r"^[⎜⎟↳│├└\s]*(.+?)\s+id=\d+\s+\[slave\s+pointer", line)
        if m:
            names.append(m.group(1).strip())
    return names


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
    ours = time.time() < _self_click_until
    print("[api] click recorded: x=%d y=%d%s"
          % (click["x"], click["y"],
             " (self, not calibration)" if ours else
             (" (during task)" if _current is not None else "")),
          flush=True)
    with _click_lock:
        _last_click = click
        if _current is not None and not ours:
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
    devices = _slave_pointer_devices()
    if not devices:
        print("[api] no slave pointer devices found, click calibration "
              "disabled", flush=True)
        return
    for device in devices:
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


# --------------------------------------------------------------------------
# Auto-click: replay the calibrated checkbox position, mimicking a human
# mouse -- curved approach in small steps, slight overshoot, tiny target
# jitter, short press.  Repeated until the token arrives (the checkbox
# takes ~5-7s to appear after page load), never at calibration time.
# --------------------------------------------------------------------------

def _human_move_to(x, y, start):
    """Move the pointer to (x,y) along a slightly curved, uneven path."""
    # Control point off the straight line gives a shallow arc, like a
    # hand moving around rather than a teleporting cursor.
    mx, my = (start[0] + x) / 2, (start[1] + y) / 2
    dx, dy = x - start[0], y - start[1]
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
    mx -= dy / dist * random.uniform(15, 60) * random.choice((-1, 1))
    my += dx / dist * random.uniform(15, 60)
    steps = random.randint(14, 24)
    for i in range(1, steps + 1):
        t = i / steps
        # Quadratic bezier through the control point.
        bx = (1 - t) ** 2 * start[0] + 2 * (1 - t) * t * mx + t * t * x
        by = (1 - t) ** 2 * start[1] + 2 * (1 - t) * t * my + t * t * y
        subprocess.run(["xdotool", "mousemove", str(int(round(bx))),
                        str(int(round(by)))],
                       capture_output=True, timeout=5)
        time.sleep(random.uniform(0.008, 0.028))


def _auto_click_once(x, y):
    """One human-like click at (x,y); returns True if the input was sent."""
    global _self_click_until
    pos = _pointer_position()
    start = pos if pos else (random.randint(0, 800), random.randint(0, 600))
    # Small overshoot then settle, like a real hand.
    if random.random() < 0.5:
        _human_move_to(x + random.randint(-25, 25), y + random.randint(-18, 18),
                       start)
        time.sleep(random.uniform(0.05, 0.15))
        _human_move_to(x, y, _pointer_position() or (x, y))
    else:
        _human_move_to(x, y, start)
    time.sleep(random.uniform(0.1, 0.35))  # aim pause before pressing
    _self_click_until = time.time() + 1.5  # our own XTEST press must not
    # contaminate the calibration sample (flag read by _record_click).
    subprocess.run(["xdotool", "click", "1"], capture_output=True, timeout=5)
    print("[api] auto-click fired at x=%d y=%d" % (x, y), flush=True)


def _coord_matches_display(coord):
    """True if the calibration was recorded at the current resolution.

    Entries from before display_h was recorded only carry the width --
    accept them on a width match; anything else must match exactly.
    """
    w = os.environ.get("DISPLAY_WIDTH", "")
    h = os.environ.get("DISPLAY_HEIGHT", "")
    if not (w and h):
        return True  # resolution unknown: trust the calibration
    dw = str(coord.get("display_w", coord.get("display", "")))
    dh = str(coord.get("display_h", ""))
    if not dw:
        return True  # pre-resolution-era entry: trust it
    if not dh:
        return dw == w  # width-only legacy entry
    return dw == w and dh == h


def _auto_click_loop(task, coord, deadline, is_done):
    """Re-click the calibrated position until the token arrives.

    The checkbox only appears ~5-7s after page load and may need a moment
    more to become clickable, so keep firing (with pauses) rather than
    betting on one shot.
    """
    x, y = coord["x"], coord["y"]
    print("[api] auto-click enabled for %s at (%d,%d)"
          % (task["hostname"], x, y), flush=True)
    next_click = time.time() + 1.0  # first attempt ~1s after loop start
    while time.time() < deadline and not is_done():
        if time.time() >= next_click:
            try:
                _auto_click_once(x + random.randint(-2, 2),
                                 y + random.randint(-2, 2))
            except (OSError, subprocess.TimeoutExpired) as e:
                print("[api] auto-click failed: %s" % e, flush=True)
            # Widget load takes 5-7s; retry soon, back off slowly.
            next_click = time.time() + random.uniform(1.5, 2.5)
        time.sleep(0.2)


def _load_coords():
    return _read_json(COORDS_FILE) or {}


def _save_coord(hostname, click, sitekey):
    """Persist the calibrated checkbox position for a hostname."""
    coords = _load_coords()
    coords[hostname] = {
        "x": click["x"], "y": click["y"],
        "sitekey": sitekey,
        # Resolution context: a coordinate is only valid at the
        # resolution it was recorded at.
        "display_w": os.environ.get("DISPLAY_WIDTH", ""),
        "display_h": os.environ.get("DISPLAY_HEIGHT", ""),
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
        auto = (AUTO_CLICK and coord
                and isinstance(coord.get("x"), int)
                and isinstance(coord.get("y"), int)
                and _coord_matches_display(coord))
        if coord and not auto and AUTO_CLICK:
            print("[api] calibration for %s unusable (resolution changed?) "
                  "-- falling back to manual click" % task["hostname"],
                  flush=True)
        started = time.time()
        try:
            # Clear any stale result, then publish the task for mitmproxy.
            for f in (RESULT_FILE, TASK_FILE, PAGE_FILE, READY_FILE):
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
            clicked_at = None
            auto_thread = None
            while time.time() < deadline:
                result = _read_json(RESULT_FILE)
                if result and result.get("task_id") == task["task_id"]:
                    elapsed = round(time.time() - started, 2)
                    # The click that completed the challenge is the calibration
                    # sample: persist it for this hostname.
                    click = _get_click_during_task()
                    if click:
                        _save_coord(task["hostname"], click, task["sitekey"])
                    if auto_thread is not None:
                        print("[api] token arrived after auto-click pass"
                              if clicked_at else "[api] token arrived",
                              flush=True)
                    return 200, {"ok": True, "task_id": task["task_id"],
                                 "hostname": task["hostname"],
                                 "token": result["token"], "elapsed": elapsed,
                                 "click": click,
                                 "calibrated": bool(click),
                                 "auto_clicked": auto_thread is not None}
                # Once the challenge document is served and the widget is
                # ready to receive input, start replaying the calibrated
                # position (checkbox itself still takes ~5-7s to show).
                if (auto and auto_thread is None
                        and _read_json(READY_FILE)):
                    auto_thread = threading.Thread(
                        target=_auto_click_loop,
                        args=(task, coord, deadline,
                              lambda: (_read_json(RESULT_FILE) or {}).get(
                                  "task_id") == task["task_id"]),
                        daemon=True)
                    auto_thread.start()
                    clicked_at = time.time()
                # Manual clicks still work while auto-click runs: the
                # listener keeps updating the calibration sample.
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
            # Read state without acquiring the solve lock: a running task
            # holds that lock for minutes, and /status is the progress
            # check for the very task that holds it.
            with _click_lock:
                cur = dict(_current) if _current else None
                lc = dict(_last_click) if _last_click else None
            last = _read_json(RESULT_FILE)
            self._send(200, {"ok": True, "current": cur, "last_result": last,
                             "page_served": _read_json(PAGE_FILE),
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
