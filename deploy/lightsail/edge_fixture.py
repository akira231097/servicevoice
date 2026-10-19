"""Synthetic local-only SSE fixture for verify_local.py; never a production entrypoint."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

lock = threading.Lock()
state = {"requests": 0, "completed": 0, "disconnected": 0}


def mark(key):
    with lock:
        state[key] += 1
        Path("/tmp/edge-fixture.json").write_text(json.dumps(state))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1024:
            self.send_error(413)
            return
        self.rfile.read(size)
        mark("requests")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for number in range(24):
                data = f'data: {{"synthetic_sequence":{number}}}\n\n'.encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                time.sleep(.1)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            mark("completed")
        except (BrokenPipeError, ConnectionResetError, OSError):
            mark("disconnected")
            self.close_connection = True


ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
