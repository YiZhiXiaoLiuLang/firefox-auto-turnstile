"""Live test for the CONNECT-head fix in upproxy.py.

Simulates the real chain on loopback:
  - T1 regression (the CI failure): router -> strict upstream that only
    answers after a COMPLETE blank-line-terminated head (mitmproxy's
    behaviour).  The old code forwarded the bare request line and the
    strict upstream waited forever.
  - T2: router -> engine for challenges.cloudflare.com, full head, echo
    through the direct dial path.
  - T3: engine consumes the whole head and forwards piggybacked tunnel
    bytes that arrived in the same packet (not dropped as junk).
"""
import socket
import socketserver
import threading
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import upproxy  # noqa: E402

RESULT = []


def ok(name):
    RESULT.append((name, True))
    print("PASS", name)


def fail(name, why):
    RESULT.append((name, False))
    print("FAIL", name, "->", why)


def recv_exact(sock, n, timeout=10):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("closed at %d/%d" % (len(buf), n))
        buf += chunk
    return buf


class StrictMitmHandler(socketserver.BaseRequestHandler):
    """Behaves like mitmproxy upstream listener: needs the FULL head."""

    def handle(self):
        try:
            self.request.settimeout(10)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.request.recv(8192)
                if not chunk:
                    return
                buf += chunk
            head, extra = buf.split(b"\r\n\r\n", 1)
            # The head must contain more than the request line: the router
            # must forward the complete head, not the bare CONNECT line.
            if b"Host:" not in head:
                self.request.sendall(b"HTTP/1.1 400 bare line\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if extra:
                self.request.sendall(extra)  # echo piggybacked bytes
            # echo pump
            while True:
                data = self.request.recv(65536)
                if not data:
                    return
                self.request.sendall(data)
        except OSError:
            pass


class EchoOriginHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(10)
            while True:
                data = self.request.recv(65536)
                if not data:
                    return
                self.request.sendall(data)
        except OSError:
            pass


def start_server(handler):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main():
    mitm_srv, mitm_port = start_server(StrictMitmHandler)
    echo_srv, echo_port = start_server(EchoOriginHandler)

    # point the router's "mitm" and the engine's egress at our fakes
    upproxy.MITM_HOST, upproxy.MITM_PORT = "127.0.0.1", mitm_port
    real_dial_direct = upproxy._dial_direct

    def fake_dial_direct(host, port, timeout):
        return real_dial_direct("127.0.0.1", echo_port, timeout)

    upproxy._dial_direct = fake_dial_direct

    engine = upproxy.EngineServer(("127.0.0.1", 0), upproxy._EngineHandler)
    engine.daemon_threads = True
    threading.Thread(target=engine.serve_forever, daemon=True).start()
    engine_port = engine.server_address[1]
    upproxy.ENGINE_HOST, upproxy.ENGINE_PORT = "127.0.0.1", engine_port

    router = upproxy.RouterServer(("127.0.0.1", 0), upproxy._RouterHandler)
    router.daemon_threads = True
    threading.Thread(target=router.serve_forever, daemon=True).start()
    router_port = router.server_address[1]

    # ---- T1: full head through the router to the STRICT upstream ----
    try:
        c = socket.create_connection(("127.0.0.1", router_port), timeout=10)
        head = (b"CONNECT example.com:443 HTTP/1.1\r\n"
                b"Host: example.com:443\r\n"
                b"User-Agent: Mozilla/5.0\r\n\r\n")
        c.sendall(head)
        line = recv_exact(c, len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        assert line.startswith(b"HTTP/1.1 200"), line
        c.sendall(b"ping-through-mitm")
        assert recv_exact(c, 17) == b"ping-through-mitm"
        c.close()
        ok("T1 router forwards COMPLETE head to strict upstream (mitm)")
    except Exception as e:
        fail("T1 router forwards COMPLETE head to strict upstream (mitm)", e)

    # ---- T2: CF host routed to engine, echo via direct dial ----
    try:
        c = socket.create_connection(("127.0.0.1", router_port), timeout=10)
        head = (b"CONNECT challenges.cloudflare.com:443 HTTP/1.1\r\n"
                b"Host: challenges.cloudflare.com:443\r\n"
                b"Proxy-Connection: keep-alive\r\n\r\n")
        c.sendall(head)
        line = recv_exact(c, len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        assert line.startswith(b"HTTP/1.1 200"), line
        c.sendall(b"tls-clienthello")
        assert recv_exact(c, 15) == b"tls-clienthello"
        c.close()
        ok("T2 router->engine (challenges.cloudflare.com) echo")
    except Exception as e:
        fail("T2 router->engine (challenges.cloudflare.com) echo", e)

    # ---- T3: piggybacked tunnel bytes sent WITH the head ----
    try:
        c = socket.create_connection(("127.0.0.1", engine_port), timeout=10)
        blob = (b"CONNECT challenges.cloudflare.com:443 HTTP/1.1\r\n"
                b"Host: challenges.cloudflare.com:443\r\n\r\n"
                b"\x16\x03\x01\x00\x05junk")
        c.sendall(blob)  # one single send: head + tunnel bytes
        line = recv_exact(c, len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        assert line.startswith(b"HTTP/1.1 200"), line
        assert recv_exact(c, 9) == b"\x16\x03\x01\x00\x05junk", "piggyback lost"
        c.sendall(b"after-piggyback")
        assert recv_exact(c, 15) == b"after-piggyback"
        c.close()
        ok("T3 engine keeps piggybacked tunnel bytes, echoes rest")
    except Exception as e:
        fail("T3 engine keeps piggybacked tunnel bytes, echoes rest", e)

    # ---- T4: engine gets a bare line + headers split across packets ----
    try:
        c = socket.create_connection(("127.0.0.1", engine_port), timeout=10)
        c.sendall(b"CONNECT challenges.cloudflare.com:443 HTTP/1.1\r\n")
        time.sleep(0.1)
        c.sendall(b"Host: challenges.cloudflare.com:443\r\n\r\n")
        line = recv_exact(c, len(b"HTTP/1.1 200 Connection established\r\n\r\n"))
        assert line.startswith(b"HTTP/1.1 200"), line
        c.sendall(b"split-packet-echo")
        assert recv_exact(c, 17) == b"split-packet-echo"
        c.close()
        ok("T4 engine handles head split across packets")
    except Exception as e:
        fail("T4 engine handles head split across packets", e)

    print()
    bad = [n for n, v in RESULT if not v]
    print("ALL LIVE TESTS PASSED" if not bad else "FAILED: %s" % bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
