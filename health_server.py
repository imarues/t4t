import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = int(os.getenv("HEALTH_PORT", "3000"))
STARTED_AT = time.time()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/ping"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"not_found"}')
            return

        payload = json.dumps({
            "ok": True,
            "service": "t4t-bot",
            "uptime_seconds": max(0, int(time.time() - STARTED_AT)),
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Health server listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()
