"""mitmproxy addon: intercept the target site's document request, serve the
Turnstile relay page, and capture the resulting token.

Flow:
  1. api_server.py writes /config/relay/task.json describing the current task.
  2. Firefox navigates to the task URL.  mitmproxy intercepts the document
     request (host must match the task hostname) and returns an injected page
     that renders Turnstile with the given sitekey.
  3. Turnstile runs in a clean browser; its traffic to challenges.cloudflare.com
     never passes through this proxy (Firefox proxy bypass list).
  4. The widget's success callback fetches /.relay-token/?t=<token>, which this
     addon captures into /config/relay/result.json.
"""

import json
import os
import time

from mitmproxy import http

TASK_FILE = "/config/relay/task.json"
RESULT_FILE = "/config/relay/result.json"

# A Turnstile token is valid for 300s.  Tasks older than this are stale.
TASK_TTL = 360


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Turnstile Relay</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; color: #333;
        display: flex; flex-direction: column; align-items: center;
        justify-content: center; min-height: 100vh; margin: 0; gap: 16px; }}
  h1 {{ font-size: 20px; font-weight: 600; }}
  #status {{ font-size: 14px; color: #666; min-height: 20px; }}
  #c {{ min-height: 65px; }}
  .ok {{ color: #1a7f37 !important; font-weight: 600; }}
  .err {{ color: #d1242f !important; font-weight: 600; }}
</style>
</head>
<body>
<h1>Turnstile Relay</h1>
<div id="c"></div>
<div id="status">Waiting for widget&hellip;</div>
<script>
  function setStatus(msg, cls) {{
    var el = document.getElementById('status');
    el.textContent = msg;
    el.className = cls || '';
  }}
  window.turnstileReady = new Promise(function (resolve, reject) {{
    var t = setInterval(function () {{
      if (window.turnstile) {{ clearInterval(t); resolve(window.turnstile); }}
    }}, 100);
    setTimeout(function () {{ clearInterval(t); reject('timeout'); }}, 20000);
  }});
  turnstileReady.then(function (ts) {{
    ts.render('#c', {{
      sitekey: '{sitekey}',
      callback: function (token) {{
        setStatus('Token obtained, delivering\u2026');
        fetch('/.relay-token/?t=' + encodeURIComponent(token))
          .then(function (r) {{
            if (r.ok) {{ setStatus('\\u2713 Token captured - you can submit it now', 'ok'); }}
            else {{ setStatus('Delivery failed (HTTP ' + r.status + ')', 'err'); }}
          }})
          .catch(function (e) {{ setStatus('Delivery error: ' + e, 'err'); }});
      }},
      'error-callback': function (code) {{
        setStatus('Turnstile error: ' + code, 'err'); return true;
      }},
      'expired-callback': function () {{
        setStatus('Token expired', 'err'); return true;
      }},
      'timeout-callback': function () {{
        setStatus('Widget timed out', 'err'); return true;
      }}
    }});
  }}).catch(function () {{
    setStatus('Failed to load challenges.cloudflare.com', 'err');
  }});
</script>
</body>
</html>
"""

_token_cache = None  # reserved for future use


def _load_task():
    """Load the current task, or None if missing/expired."""
    global _token_cache
    try:
        mtime = os.stat(TASK_FILE).st_mtime
    except OSError:
        return None
    if time.time() - mtime > TASK_TTL:
        return None
    try:
        with open(TASK_FILE, "r", encoding="utf-8") as f:
            task = json.load(f)
        if not task.get("hostname") or not task.get("sitekey"):
            return None
        return task
    except (OSError, ValueError):
        return None


def _write_result(task, token):
    result = {
        "task_id": task.get("task_id"),
        "hostname": task.get("hostname"),
        "token": token,
        "ts": int(time.time()),
    }
    tmp = RESULT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, RESULT_FILE)
    # Single-use: drop the task so a page reload cannot deliver twice.
    try:
        os.remove(TASK_FILE)
    except OSError:
        pass


def request(flow: http.HTTPFlow) -> None:
    task = _load_task()
    if task is None:
        return

    host = flow.request.host
    req_path = flow.request.path.split("?", 1)[0]

    # Token delivery endpoint (same-origin fetch from the injected page).
    if host == task["hostname"] and req_path == "/.relay-token/":
        token = flow.request.query.get("t")
        if token:
            _write_result(task, token)
            flow.response = http.Response.make(
                200, b"ok", {"Content-Type": "text/plain; charset=utf-8"}
            )
        else:
            flow.response = http.Response.make(400, b"missing token", {})
        return

    # Document request: exact path match (query ignored), task host only.
    if host == task["hostname"] and req_path == task.get("path", "/"):
        flow.response = http.Response.make(
            200,
            PAGE.format(sitekey=task["sitekey"]).encode("utf-8"),
            {"Content-Type": "text/html; charset=utf-8"},
        )
