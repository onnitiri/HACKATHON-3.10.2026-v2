"""Local CellShield server: static page + /api/verify (Allsolve backend).

Usage:  .venv/bin/python server.py [port]   -> http://localhost:8000
Reads ALLSOLVE_ACCESS_KEY / ALLSOLVE_SECRET_KEY / ALLSOLVE_HOST from .env next to this file.
"""
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "api"))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

import _allsolve_pipeline as pipeline  # noqa: E402


def safe(fn, *args):
    try:
        out = fn(*args)
    except Exception as e:  # pipeline already catches; belt and braces
        out = {"status": "error", "message": f"{type(e).__name__}: {e}"}
    out.pop("trace", None)
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/verify":
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            pid = qs.pop("project_id", None)
            return self._json(safe(pipeline.poll, pid, qs) if pid else safe(pipeline.availability))
        if u.path.startswith(("/.env", "/api/", "/.venv", "/server.py")):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/verify":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            design = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            design = {}
        self._json(safe(pipeline.submit, design))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"CellShield running at http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
