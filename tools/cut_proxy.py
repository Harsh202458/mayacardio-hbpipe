"""TCP proxy that can black-hole traffic, to simulate a network cut without root.

    python tools/cut_proxy.py --listen 18081 --target 127.0.0.1:18080 --flag /tmp/netcut

While the flag file exists, bytes in both directions are silently dropped (like an iptables
DROP rule): connections hang instead of failing fast. On Linux test boxes prefer the real
thing: tests/chaos/net_cut.sh.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading


def pump(src, dst, flag, done):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            if os.path.exists(flag):
                continue  # black hole
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)  # pass the end-of-stream on, keep the other direction open
    except OSError:
        pass
    finally:
        with done["lock"]:
            done["n"] += 1
            last = done["n"] == 2
        if last:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--listen", type=int, required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--flag", required=True)
    a = p.parse_args(argv)
    host, port = a.target.rsplit(":", 1)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.listen))
    srv.listen(64)
    while True:
        c, _ = srv.accept()
        try:
            u = socket.create_connection((host, int(port)), timeout=5)
            u.settimeout(None)
        except OSError:
            c.close()
            continue
        done = {"n": 0, "lock": threading.Lock()}
        threading.Thread(target=pump, args=(c, u, a.flag, done), daemon=True).start()
        threading.Thread(target=pump, args=(u, c, a.flag, done), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
