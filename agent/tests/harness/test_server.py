"""Deterministic local HTTP test server for Lumine browser certification.

Two instances of this server are used by the suite. One is the "same origin"
origin for the fixtures; a second instance is a *different port*, which makes it
a genuinely different origin for iframe and CORS certification. Nothing here
reaches the network, and every failure mode is opt-in through a URL.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class CertificationServerHandler(BaseHTTPRequestHandler):
    server_version = "LumineCertificationServer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Suppress standard logging to keep test console clean
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: object, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _html(self, body: str, code: int = 200) -> None:
        self._send(code, body.encode("utf-8"), "text/html; charset=utf-8")

    # -- routes ----------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/status":
            code = int(query.get("code", ["200"])[0])
            kind = query.get("as", [""])[0]
            if kind == "image":
                self._send(code, b"not-an-image", "image/png")
            elif kind == "script":
                self._send(code, b"/* broken resource */", "text/javascript")
            else:
                self._send(code, f"HTTP Status {code}".encode("utf-8"), "text/plain")
            return

        if path == "/api/delay":
            ms = min(int(query.get("ms", ["500"])[0]), 10_000)
            time.sleep(ms / 1000.0)
            self._json({"delayed_ms": ms})
            return

        if path == "/api/search":
            term = query.get("q", [""])[0].strip()
            catalogue = (
                ("Alpha Widget", "SKU-ALPHA"),
                ("Bravo Widget", "SKU-BRAVO"),
                ("Charlie Widget", "SKU-CHARLIE"),
                ("Delta Gadget", "SKU-DELTA"),
            )
            rows = [{"title": title, "sku": sku} for title, sku in catalogue if term.casefold() in title.casefold()]
            self._json({"query": term, "results": rows})
            return

        if path == "/api/cors-echo":
            self._json({"origin": self.headers.get("Origin", ""), "ok": True})
            return

        if path == "/api/redirect":
            target = query.get("to", ["/"])[0]
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/redirect-chain":
            count = int(query.get("count", ["1"])[0])
            final = query.get("final", ["/fixtures/static_elements.html"])[0]
            if count <= 1:
                target = final
            else:
                target = f"/api/redirect-chain?count={count - 1}&final={final}"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/api/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/abort":
            # Close the connection without a response. Deterministic connection
            # failure, distinct from an HTTP error status.
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return

        if path == "/api/hang":
            # Hold the request open longer than any caller's timeout. Bounded so
            # a leaked thread cannot outlive the test process indefinitely.
            time.sleep(min(int(query.get("ms", ["8000"])[0]), 30_000))
            self._json({"hung_ms": query.get("ms", ["8000"])[0]})
            return

        if path == "/api/download":
            filename = query.get("file", ["test_download.txt"])[0]
            content = b"Lumine browser certification test download payload"
            self._send(
                200,
                content,
                "application/octet-stream",
                {"Content-Disposition": f'attachment; filename="{filename}"'},
            )
            return

        if path == "/api/stream-slow":
            body = b"<html><body><h1>Slow Loading Page</h1><p>First segment</p><p>Second segment</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:40])
            self.wfile.flush()
            time.sleep(0.3)
            self.wfile.write(body[40:])
            self.wfile.flush()
            return

        if path == "/api/cross-origin-parent":
            child = query.get("child", ["/fixtures/cross_origin_child.html"])[0]
            self._html(
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<title>Cross-Origin Parent</title></head><body>"
                "<h1>Cross-Origin Parent</h1>"
                "<button id='parent-action'>Parent Action</button>"
                f"<iframe id='cross-origin-frame' src='{child}'></iframe>"
                "<output id='parent-status'>parent idle</output>"
                "<script>document.getElementById('parent-action').addEventListener('click',"
                "() => document.getElementById('parent-status').textContent='parent clicked');</script>"
                "</body></html>"
            )
            return

        # Serve static fixture
        if path == "/" or path == "/index.html":
            file_path = self.server.fixtures_dir / "index.html"
        elif path.startswith("/fixtures/"):
            rel_path = path[len("/fixtures/") :]
            file_path = (self.server.fixtures_dir / rel_path).resolve()
        else:
            file_path = (self.server.fixtures_dir / path.lstrip("/")).resolve()

        fixtures_root = self.server.fixtures_dir.resolve()
        if not str(file_path).startswith(str(fixtures_root)):
            self._send(403, b"Forbidden fixture path", "text/plain")
            return

        if file_path.is_file():
            mime_type, _ = mimetypes.guess_type(str(file_path))
            body = file_path.read_bytes()
            self._send(200, body, mime_type or "text/html")
        else:
            self._send(404, f"Fixture Not Found: {path}".encode("utf-8"), "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        parsed = urlparse(self.path)
        path = parsed.path
        content_type = self.headers.get("Content-Type", "")

        if path in ("/api/form-submit", "/api/upload"):
            response = {
                "ok": True,
                "received_bytes": len(body),
                "content_type": content_type,
                "submitted_at": time.time(),
                "multipart": "multipart/form-data" in content_type,
            }
            if "application/json" in content_type:
                try:
                    response["json"] = json.loads(body.decode("utf-8"))
                except Exception:
                    pass
            elif "application/x-www-form-urlencoded" in content_type:
                try:
                    response["form"] = parse_qs(body.decode("utf-8"))
                except Exception:
                    pass
            elif "multipart/form-data" in content_type:
                response["upload_markers"] = {
                    "filename": b'filename="' in body,
                    "file_bytes": body.count(b"Lumine synthetic upload"),
                }
            self._json(response)
            return

        self._json({"status": "received", "length": len(body)})


class LocalTestServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, fixtures_dir: Path | None = None) -> None:
        self.server = ThreadingHTTPServer((host, port), CertificationServerHandler)
        self.server.daemon_threads = True
        self.server.fixtures_dir = Path(fixtures_dir or FIXTURES_DIR)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
