#!/usr/bin/env python3
"""Minimal HTTP CONNECT proxy used for fnack's split-mode VPN.

The fnack container routes ALL traffic through the WireGuard tunnel by
default, which makes the web dashboard unreachable and blackholes every
connection when the peer handshake is incomplete. Split mode instead keeps
the container's normal routing and sends only download/metadata traffic
through the tunnel: this proxy listens on 127.0.0.1, and an `ip rule
uidrange` entry routes the proxy's own outbound sockets via wg0.

Works with requests, httpx and yt-dlp out of the box (plain HTTP proxy, no
PySocks/socksio needed). HTTPS traffic arrives as CONNECT and is tunneled
raw; plain-HTTP absolute-form requests are rewritten and forwarded.

Usage:  python3 scripts/http_proxy.py [--port 1080] [--uid 2001]
"""

import argparse
import logging
import os
import select
import socket
import threading

logging.basicConfig(level=logging.INFO, format="[PROXY] %(message)s")
log = logging.getLogger("proxy")

_BUFSIZE = 64 * 1024


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """Bidirectionally pump bytes between two sockets until one closes."""
    sockets = [a, b]
    try:
        while True:
            r, _, _ = select.select(sockets, [], [], 30)
            if not r:
                continue
            for s in r:
                try:
                    data = s.recv(_BUFSIZE)
                except OSError:
                    data = b""
                if not data:
                    return
                peer = b if s is a else a
                try:
                    peer.sendall(data)
                except OSError:
                    return
    finally:
        for s in sockets:
            try:
                s.close()
            except OSError:
                pass


def _read_headers(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(_BUFSIZE)
        if not chunk:
            break
        data += chunk
        if len(data) > 1024 * 1024:
            break
    return data


def handle_client(conn: socket.socket) -> None:
    try:
        conn.settimeout(60)
        head = _read_headers(conn)
        if not head:
            conn.close()
            return
        first_line = head.split(b"\r\n", 1)[0]
        parts = first_line.split(b" ")
        if len(parts) < 3:
            conn.close()
            return
        method, target = parts[0], parts[1]

        if method == b"CONNECT":
            # CONNECT host:port  ->  tunnel raw bytes
            hostport = target.decode("latin1")
            host, _, port = hostport.rpartition(":")
            port = int(port or 443)
            upstream = socket.create_connection((host, port), timeout=30)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            # absolute-form HTTP request: GET http://host:port/path ...
            # connect to the host and rewrite the request line to origin-form
            rest = head.split(b"\r\n\r\n", 1)
            headers_part = rest[0] if rest else head
            body = rest[1] if len(rest) > 1 else b""
            target_str = target.decode("latin1")
            scheme, _, rest_t = target_str.partition("://")
            hostport, _, path = rest_t.partition("/")
            default_port = 443 if scheme == "https" else 80
            if ":" in hostport:
                host, _, port_str = hostport.rpartition(":")
                try:
                    port = int(port_str)
                except ValueError:
                    host, port = hostport, default_port
            else:
                host, port = hostport, default_port
            upstream = socket.create_connection((host, port), timeout=30)
            new_line = b"%s /%s HTTP/1.1" % (method, path.encode("latin1"))
            headers = head.split(b"\r\n", 1)[1].split(b"\r\n\r\n")[0]
            rewritten = new_line + b"\r\n" + headers
            # strip proxy-hop headers
            lines = [ln for ln in rewritten.split(b"\r\n")
                     if not ln.lower().startswith((b"proxy-connection:", b"proxy-authorization:"))]
            payload = b"\r\n".join(lines) + b"\r\n\r\n" + body
            upstream.sendall(payload)

        _pipe(conn, upstream)
    except Exception as e:
        log.debug("client error: %s", e)
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=1080)
    ap.add_argument("--uid", type=int, default=None)
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(128)

    # Drop privileges AFTER binding, so the ip-rule uidrange match applies to
    # this process's outbound sockets (routed via wg0) — and only to those.
    if args.uid and os.geteuid() == 0:
        os.setgid(args.uid)
        os.setuid(args.uid)

    log.info("HTTP CONNECT proxy listening on 127.0.0.1:%d (uid=%s)", args.port, args.uid or "root")
    while True:
        conn, _ = srv.accept()
        t = threading.Thread(target=handle_client, args=(conn,), daemon=True)
        t.start()


if __name__ == "__main__":
    raise SystemExit(main())
