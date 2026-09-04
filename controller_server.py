#!/usr/bin/env python3
"""
Finance Controller HTTP server.

  python controller_server.py
  open http://127.0.0.1:8765/

Control setup → Data validation → Reconciliation → Exception review
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import controller_batch as cb  # noqa: E402

HOST = os.environ.get("CONTROLLER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CONTROLLER_PORT", "8765"))


def _json_bytes(payload: dict, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, indent=2).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def _read_multipart(handler: BaseHTTPRequestHandler) -> tuple[str, bytes]:
    """Minimal multipart/form-data parser for a single file field named 'file'."""
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    if "multipart/form-data" not in content_type:
        filename = handler.headers.get("X-Filename", "upload.csv")
        return filename, body

    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
            break
    if not boundary:
        raise ValueError("multipart boundary missing")

    marker = ("--" + boundary).encode("utf-8")
    chunks = body.split(marker)
    for chunk in chunks:
        if b"Content-Disposition" not in chunk:
            continue
        if b'name="file"' not in chunk and b"name=file" not in chunk:
            if b"filename=" not in chunk:
                continue
        header_blob, _, file_body = chunk.partition(b"\r\n\r\n")
        if not file_body:
            continue
        if file_body.endswith(b"\r\n"):
            file_body = file_body[:-2]
        if file_body.endswith(b"--"):
            file_body = file_body[:-2]
        if file_body.endswith(b"\r\n"):
            file_body = file_body[:-2]
        filename = "upload.csv"
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            if "filename=" in line:
                filename = line.split("filename=", 1)[1].strip().strip('"')
                break
        return filename, file_body
    raise ValueError("no file part found in multipart body")


class ControllerHandler(BaseHTTPRequestHandler):
    server_version = "ReconController/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Filename")

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200):
        status, body, ctype = _json_bytes(payload, status)
        self._send(status, body, ctype)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/controller", "/controller.html"):
            html = (ROOT / "controller.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/health":
            self._send_json({"ok": True, "service": "finance-controller"})
            return

        if path == "/api/batches":
            self._send_json({"batches": cb.list_batches()})
            return

        if path.startswith("/api/batches/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                batch_id = parts[2]
                try:
                    if len(parts) == 3:
                        self._send_json(cb.load_batch(batch_id))
                        return
                    if len(parts) == 4 and parts[3] == "results":
                        self._send_json(cb.get_results(batch_id))
                        return
                    if len(parts) == 4 and parts[3] == "validation":
                        vpath = cb.batch_dir(batch_id) / "validation.json"
                        if not vpath.exists():
                            self._send_json({"error": "validation not run"}, 404)
                            return
                        self._send_json(json.loads(vpath.read_text(encoding="utf-8")))
                        return
                except FileNotFoundError as e:
                    self._send_json({"error": str(e)}, 404)
                    return

        # static files under the project root
        safe = (ROOT / path.lstrip("/")).resolve()
        if str(safe).startswith(str(ROOT)) and safe.is_file() and safe.suffix in {
            ".html", ".css", ".js", ".json", ".svg", ".png",
        }:
            ctype = mimetypes.guess_type(str(safe))[0] or "application/octet-stream"
            self._send(200, safe.read_bytes(), ctype)
            return

        self._send_json({"error": "not found", "path": path}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/batches":
                self._send_json(cb.create_batch(), 201)
                return

            if path.startswith("/api/batches/") and path.endswith("/validate"):
                batch_id = path.strip("/").split("/")[2]
                state = cb.validate_batch(batch_id)
                self._send_json(state)
                return

            if path.startswith("/api/batches/") and path.endswith("/reconcile"):
                batch_id = path.strip("/").split("/")[2]
                state = cb.reconcile_batch(batch_id)
                self._send_json(state)
                return

            if path.startswith("/api/batches/") and path.endswith("/override"):
                batch_id = path.strip("/").split("/")[2]
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError as e:
                    self._send_json({"error": f"invalid JSON: {e}"}, 400)
                    return
                results = cb.apply_override(
                    batch_id,
                    order_id=str(payload.get("order_id") or ""),
                    action=str(payload.get("action") or ""),
                    note=str(payload.get("note") or ""),
                    operator=str(payload.get("operator") or "operator"),
                )
                self._send_json(results)
                return

            if "/upload/" in path and path.startswith("/api/batches/"):
                parts = path.strip("/").split("/")
                if len(parts) != 5 or parts[3] != "upload":
                    self._send_json({"error": "bad upload path"}, 400)
                    return
                batch_id, role = parts[2], parts[4]
                filename, content = _read_multipart(self)
                if not filename.lower().endswith(".csv"):
                    self._send_json({"error": "only CSV uploads are accepted"}, 400)
                    return
                state = cb.store_upload(batch_id, role, filename, content)
                self._send_json(state)
                return

            self._send_json({"error": "not found", "path": path}, 404)
        except FileNotFoundError as e:
            self._send_json({"error": str(e)}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": str(e), "type": type(e).__name__}, 500)


def main():
    cb.ensure_batch_root()
    if not (ROOT / "controller.html").exists():
        raise SystemExit("controller.html missing")
    server = ThreadingHTTPServer((HOST, PORT), ControllerHandler)
    print(f"Finance Controller listening on http://{HOST}:{PORT}/", flush=True)
    print("Flow: Control setup → Data validation → Reconciliation → Exception review", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
