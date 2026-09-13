"""Egress engine for the Turnstile relay.

All outbound traffic of a task flows through here so that "use a proxy"
is one switchable property of the task, not of each component:

    Firefox (locked proxy 127.0.0.1:4114 router)
      |-- challenges.cloudflare.com ----> engine 127.0.0.1:8082 (CONNECT)
      |                                      |-- task has proxy? dial upstream
      |                                      |-- no task / no proxy? direct
      +-- everything else -------------> mitmproxy 127.0.0.1:8080 (regular)
                                            mitmproxy in *upstream mode*,
                                            also pointing at the engine.

The Cloudflare branch stays end-to-end TLS: the engine only relays the
tunnel bytes (Firefox performs the TLS handshake with Cloudflare itself),
so the client TLS fingerprint is untouched -- only the exit IP changes.

Supported upstream schemes:
    (no scheme) host:port            -> HTTP proxy
    http://[user:pass@]host:port     -> HTTP proxy (CONNECT)
    https://[user:pass@]host:port    -> HTTP proxy over TLS
    socks4://host:port               -> SOCKS4
    socks5://[user:pass@]host:port    -> SOCKS5 (RFC 1929 auth)

The upstream is chosen per-connection from the CURRENT task.json (written
by the API service): with a task in flight that carries a "proxy", all
egress goes through it; without a task (or a task without a proxy),
everything goes direct -- identical to the no-proxy behaviour.
"""

import json
import os
import re
import socket
import socketserver
import ssl
import struct
import threading
import time
import urllib.parse

TASK_FILE = "/config/relay/task.json"
TASK_TTL = 360  # a task file older than this is stale, ignore it

ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4114   # Firefox-facing (locked policy)
ENGINE_HOST, ENGINE_PORT = "127.0.0.1", 8082   # mitmproxy upstream + CF exit
MITM_HOST, MITM_PORT = "127.0.0.1", 8080

# Hostnames whose traffic must skip mitmproxy (clean TLS to Cloudflare).
PASSTHROUGH = ("challenges.cloudflare.com",)

CONNECT_RE = re.compile(rb"^CONNECT (\S+) HTTP/1\.[01]\r?\n")


def _load_task_proxy():
    """Proxy URL of the current task, or None (no task / no proxy / stale)."""
    try:
        if time.time() - os.stat(TASK_FILE).st_mtime > TASK_TTL:
            return None
        with open(TASK_FILE, "r", encoding="utf-8") as f:
            task = json.load(f)
        proxy = task.get("proxy")
        if isinstance(proxy, str) and proxy:
            return proxy
    except (OSError, ValueError):
        pass
    return None


def _parse_upstream(url):
    """Split a proxy URL into (scheme, host, port, user, password)."""
    if "://" not in url:
        url = "http://" + url
    p = urllib.parse.urlsplit(url)
    scheme = (p.scheme or "http").lower()
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise ValueError("unsupported proxy scheme: %r" % p.scheme)
    host = p.hostname
    if not host:
        raise ValueError("proxy URL has no host")
    port = p.port or (443 if scheme == "https" else 1080
                      if scheme.startswith("socks") else 8080)
    return scheme, host, port, p.username, p.password


# --------------------------------------------------------------------------
# Upstream dialing: return a connected socket to the target through the
# configured proxy (or a direct socket when there is none).
# --------------------------------------------------------------------------

def _dial_direct(host, port, timeout):
    # socket.getaddrinfo can hang on a dead resolver; bound it.
    addrinfos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    af, st, proto, _, sa = addrinfos[0]
    sock = socket.socket(af, st, proto)
    sock.settimeout(timeout)
    sock.connect(sa)
    sock.settimeout(None)
    return sock


def _dial_http_upstream(up_sock, host, port, user, password, timeout):
    """Ask an HTTP proxy for a CONNECT tunnel; returns the same socket."""
    auth = ""
    if user is not None:
        import base64
        cred = ("%s:%s" % (user, password or "")).encode("utf-8")
        auth = "Proxy-Authorization: Basic %s\r\n" % (
            base64.b64encode(cred).decode("ascii"))
    req = ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n%s\r\n"
           % (host, port, host, port, auth)).encode("ascii")
    up_sock.sendall(req)
    up_sock.settimeout(timeout)
    reply = b""
    while b"\r\n\r\n" not in reply and b"\n\n" not in reply:
        chunk = up_sock.recv(4096)
        if not chunk:
            raise OSError("proxy closed during CONNECT")
        reply += chunk
        if len(reply) > 65536:
            raise OSError("oversized CONNECT reply")
    up_sock.settimeout(None)
    status = reply.split(b"\r\n")[0].split()
    if len(status) < 2 or not status[1].startswith(b"2"):
        raise OSError("upstream refused CONNECT: %s"
                      % reply.split(b"\r\n")[0].decode("latin-1", "replace"))
    return up_sock


def _dial_socks4(up_sock, host, port, timeout):
    if not re.fullmatch(r"[0-9.]+", host):
        raise OSError("socks4 cannot proxy hostname %r (use socks5)" % host)
    req = struct.pack("!BBH", 4, 1, port) + socket.inet_aton(host) + b"\x00"
    up_sock.sendall(req)
    up_sock.settimeout(timeout)
    head = b""
    while len(head) < 8:
        chunk = up_sock.recv(8 - len(head))
        if not chunk:
            raise OSError("proxy closed during socks4 handshake")
        head += chunk
    up_sock.settimeout(None)
    code = head[1]
    if code != 90:
        raise OSError("socks4 refused (code %d)" % code)
    return up_sock


def _dial_socks5(up_sock, host, port, user, password, timeout):
    host_b = host.encode("idna") if not host.isascii() else host.encode("ascii")
    # RFC 1928 greeting: [no-auth, user/pass auth]
    up_sock.sendall(b"\x05\x01\x00" if user is None else b"\x05\x02\x00\x02")
    up_sock.settimeout(timeout)
    head = up_sock.recv(2)
    if len(head) < 2 or head[0] != 5:
        raise OSError("bad socks5 greeting reply")
    if head[1] == 2:  # server chose user/pass -> RFC 1929
        if user is None:
            raise OSError("socks5 server demands auth, none configured")
        u = (user or "").encode("utf-8")
        p = (password or "").encode("utf-8")
        if len(u) > 255 or len(p) > 255:
            raise OSError("socks5 credentials too long")
        up_sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        ar = up_sock.recv(2)
        if len(ar) < 2 or ar[1] != 0:
            raise OSError("socks5 auth rejected")
    elif head[1] != 0:
        raise OSError("socks5 no acceptable auth method (code %d)" % head[1])
    # CONNECT request
    req = (b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
           + struct.pack("!H", port))
    up_sock.sendall(req)
    rep = b""
    while len(rep) < 5:  # header: ver rep rsv atyp len(d1)
        chunk = up_sock.recv(5 - len(rep))
        if not chunk:
            raise OSError("proxy closed during socks5 CONNECT")
        rep += chunk
    if rep[1] != 0:
        raise OSError("socks5 CONNECT refused (code %d)" % rep[1])
    atyp = rep[3]
    if atyp == 1:
        need = 4 + 2
    elif atyp == 4:
        need = 16 + 2
    elif atyp == 3:
        # need the domain length byte first
        if len(rep) < 5:
            rep += up_sock.recv(1)
        need = rep[4] + 2
    else:
        raise OSError("socks5 bad address type %d" % atyp)
    while len(rep) < 5 + need:
        chunk = up_sock.recv(5 + need - len(rep))
        if not chunk:
            raise OSError("proxy closed during socks5 CONNECT")
        rep += chunk
    up_sock.settimeout(None)
    return up_sock


def dial(host, port, timeout=20):
    """Connect to host:port via the current task's proxy (or direct)."""
    proxy = _load_task_proxy()
    if not proxy:
        return _dial_direct(host, port, timeout)
    scheme, phost, pport, user, password = _parse_upstream(proxy)
    if scheme == "https":
        ctx = ssl.create_default_context()
        up_sock = ctx.wrap_socket(
            socket.create_connection((phost, pport), timeout=timeout),
            server_hostname=phost)
    else:
        up_sock = socket.create_connection((phost, pport), timeout=timeout)
    up_sock.settimeout(timeout)
    try:
        if scheme in ("http", "https"):
            return _dial_http_upstream(up_sock, host, port,
                                       user, password, timeout)
        if scheme == "socks4":
            return _dial_socks4(up_sock, host, port, timeout)
        return _dial_socks5(up_sock, host, port, user, password, timeout)
    except Exception:
        try:
            up_sock.close()
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------
# Port 8082 "engine": a minimal HTTP proxy that only honours CONNECT and
# relays the tunnel bytes through dial() (i.e. through the task's proxy).
# mitmproxy's upstream mode sends everything here; when the API task has
# no proxy set, dial() is direct and behaviour is unchanged.
# --------------------------------------------------------------------------

def _pump(a, b):
    """Copy a->b until EOF, then close both halves."""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _parse_target(buf):
    """(host, port) from a CONNECT request head, or None."""
    m = CONNECT_RE.match(buf)
    if not m:
        return None
    target = m.group(1).decode("ascii", "replace")
    if ":" in target:
        host, port = target.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return target, 443
    return target, 443


def _split_head(buf):
    """(head, bytes after the head) -- clients may pipeline tunnel bytes."""
    i = buf.find(b"\r\n\r\n")
    if i < 0:
        return buf, b""
    return buf[:i + 4], buf[i + 4:]


def _read_request_head(request):
    """Buffer the client's complete request head (up to the blank line).

    CONNECT carries no body: the head is the request line plus headers,
    terminated by an empty line -- and both Firefox and mitmproxy (upstream
    mode) send the whole head before waiting for the 200.  Consuming it
    fully matters twice: the router forwards the head verbatim (a bare
    request line is an incomplete request that a real HTTP proxy such as
    mitmproxy waits on forever), and the engine must swallow the headers
    so the tunnel payload starts with clean TLS bytes rather than the
    tail of the head.
    """
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = request.recv(8192)
        if not chunk:
            return buf if buf else None
        buf += chunk
        if len(buf) > 16384:
            return None
    return buf


def _serve_tunnel(request, upstream_sock):
    """Pump both directions until the tunnel drains.

    Joins both pump threads: BaseRequestHandler closes self.request when
    handle() returns, so returning early would truncate the tunnel.
    """
    t1 = threading.Thread(target=_pump, args=(request, upstream_sock), daemon=True)
    t2 = threading.Thread(target=_pump, args=(upstream_sock, request), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


class _EngineHandler(socketserver.BaseRequestHandler):
    def handle(self):
        buf = _read_request_head(self.request)
        if buf is None:
            return
        parsed = _parse_target(buf)
        if not parsed:
            self.request.sendall(b"HTTP/1.1 405 CONNECT only\r\n\r\n")
            return
        host, port = parsed
        try:
            up = dial(host, port)
        except OSError as e:
            self.request.sendall(
                (b"HTTP/1.1 502 %s\r\n\r\n"
                 % str(e).encode("latin-1", "replace")[:200]))
            return
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _, extra = _split_head(buf)
        if extra:
            up.sendall(extra)  # pipelined tunnel bytes arrived with the head
        _serve_tunnel(self.request, up)


class EngineServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------
# Port 4114 "router": Firefox's locked proxy.  Routes by destination:
#   challenges.cloudflare.com -> engine (end-to-end TLS, exit via proxy)
#   everything else           -> mitmproxy (regular interception)
# Firefox only ever issues CONNECTs for https, so this is tunnel routing.
# --------------------------------------------------------------------------

def _route(host, port):
    for name in PASSTHROUGH:
        if host == name or host.endswith("." + name):
            return ENGINE_HOST, ENGINE_PORT
    return MITM_HOST, MITM_PORT


class _RouterHandler(socketserver.BaseRequestHandler):
    def handle(self):
        buf = _read_request_head(self.request)
        if buf is None:
            return
        parsed = _parse_target(buf)
        if not parsed:
            # Plain (http) requests are not expected: Firefox is locked to
            # a manual proxy with all-protocols, and our pages are https.
            self.request.sendall(b"HTTP/1.1 405 CONNECT only\r\n\r\n")
            return
        host, port = parsed
        dst_host, dst_port = _route(host, port)
        try:
            up = socket.create_connection((dst_host, dst_port), timeout=10)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 upstream down\r\n\r\n")
            return
        # Forward the COMPLETE head verbatim: a bare request line is an
        # unterminated request that a real HTTP server (mitmproxy) waits
        # on forever -- this is what deadlocked the chain before.
        up.sendall(buf)
        up.settimeout(20)
        # The upstream (engine or mitmproxy) answers the handshake; relay
        # its status line and any piggybacked bytes, then switch to pumping.
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = up.recv(8192)
            if not chunk:
                return
            reply += chunk
            if len(reply) > 16384:
                return
        up.settimeout(None)
        self.request.sendall(reply)
        _serve_tunnel(self.request, up)


class RouterServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    engine = EngineServer((ENGINE_HOST, ENGINE_PORT), _EngineHandler)
    threading.Thread(target=engine.serve_forever, daemon=True).start()
    print("[upproxy] engine on %s:%d (egress: task proxy or direct)"
          % (ENGINE_HOST, ENGINE_PORT), flush=True)
    router = RouterServer((ROUTER_HOST, ROUTER_PORT), _RouterHandler)
    print("[upproxy] router on %s:%d (challenges.cloudflare.com -> engine, "
          "rest -> mitmproxy)" % (ROUTER_HOST, ROUTER_PORT), flush=True)
    router.serve_forever()


if __name__ == "__main__":
    main()
