"""Cloud connection reuse must survive the server closing a kept-alive connection."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hbpipe.device.uplink import CloudClient


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        Handler.calls += 1
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        if Handler.calls % 2 == 1:
            self.send_header("Connection", "close")  # like a proxy closing the keep-alive
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)


class UplinkTests(unittest.TestCase):
    def setUp(self):
        Handler.calls = 0
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.client = CloudClient(f"http://127.0.0.1:{self.srv.server_address[1]}", "t")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_reconnects_after_server_closes_connection(self):
        results = [self.client.ping() for _ in range(6)]
        self.assertEqual(results, [True] * 6)
        self.assertEqual(Handler.calls, 6)

    def test_reset_drops_connections(self):
        self.assertTrue(self.client.ping())
        self.client.reset()
        self.assertFalse(self.client.connected("ping"))
        self.assertTrue(self.client.ping())


if __name__ == "__main__":
    unittest.main()
