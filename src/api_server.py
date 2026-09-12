"""HTTP API service for the Turnstile relay.

Endpoints:
  POST /solve   {url, sitekey, timeout?}  -> long-poll, returns the token
  GET  /status  -> current/last task state
  GET  /healthz -> liveness probe

Single-task model: the container drives one Firefox window, so only one
solve may be in flight at a time; concurrent requests get 409.  The task is
communicated to mitmproxy via /config/relay/task.json, and the token comes
back via /config/relay/result.json (written by the mitmproxy addon).
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
NAV_SCRIPT = "/opt/turnstile-relay/nav.sh"

HOST, PORT = "0.0.0.0", 8081
DEFAULT_TIMEOUT = 180
MAX_TIMEOUT = 300
POLL_INTERVAL = 0.2
NAV_MAX_WAIT = 60  # seconds to wait for a usable Firefox window

_lock = threading.Lock()
_current = None  # dict of the in-flight task, or None


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
            while time.time() < deadline:
                result = _read_json(RESULT_FILE)
                if result and result.get("task_id") == task["task_id"]:
                    elapsed = round(time.time() - started, 2)
                    return 200, {"ok": True, "task_id": task["task_id"],
                                 "hostname": task["hostname"],
                                 "token": result["token"], "elapsed": elapsed}
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
            self._send(200, {"ok": True, "current": cur, "last_result": last})
        else:
            self._send(404, {"ok": False, "error": "not found"})


def main():
    os.makedirs(TASK_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[api] listening on %s:%d" % (HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
