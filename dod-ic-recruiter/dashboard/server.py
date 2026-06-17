"""
Pure-stdlib HTTP server for the DOD/IC Recruiter dashboard.
Allowed imports: http.server, json, os, shutil, datetime, pathlib,
                 urllib.parse, io, cgi, email, sys
"""

import cgi
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DASHBOARD_DIR = Path(__file__).parent
SKILL_DIR = DASHBOARD_DIR.parent          # dod-ic-recruiter root

SESSION_DIR = SKILL_DIR / "session"
IMPORTS_DIR = SKILL_DIR / "imports"
CANDIDATES_DIR = SKILL_DIR / "data" / "candidates"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_response(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _file_response(handler, path: Path, content_type: str) -> None:
    if not path.is_file():
        _json_response(handler, 404, {"error": "not found"})
        return
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _read_json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):

    # Suppress default 'GET /path HTTP/1.1' logging; we do our own.
    def log_message(self, fmt, *args):
        pass

    def _log(self):
        print(f"{self.command} {self.path}", flush=True)

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------

    def do_GET(self):
        self._log()
        try:
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/":
                _file_response(self, DASHBOARD_DIR / "index.html", "text/html")
            elif route == "/style.css":
                _file_response(self, DASHBOARD_DIR / "style.css", "text/css")
            elif route == "/dashboard.js":
                _file_response(self, DASHBOARD_DIR / "dashboard.js", "application/javascript")
            elif route == "/results":
                self._get_results()
            elif route == "/results-modified":
                self._get_results_modified()
            else:
                _json_response(self, 404, {"error": "not found"})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def _get_results(self):
        results_file = SESSION_DIR / "last_results.json"
        if not results_file.is_file():
            _json_response(self, 200, {"status": "no_results"})
            return
        try:
            data = json.loads(results_file.read_text(encoding="utf-8"))
            _json_response(self, 200, {"status": "ok", "data": data})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def _get_results_modified(self):
        results_file = SESSION_DIR / "last_results.json"
        if not results_file.is_file():
            _json_response(self, 200, {"modified": 0})
            return
        mtime = results_file.stat().st_mtime
        _json_response(self, 200, {"modified": mtime})

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------

    def do_POST(self):
        self._log()
        try:
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/save-search":
                self._post_save_search()
            elif route == "/upload-icims":
                self._post_upload_icims()
            elif route == "/save-note":
                self._post_save_note()
            else:
                _json_response(self, 404, {"error": "not found"})
        except Exception as exc:
            _json_response(self, 500, {"error": str(exc)})

    def _post_save_search(self):
        try:
            body = _read_json_body(self)
        except Exception:
            _json_response(self, 400, {"error": "invalid JSON body"})
            return

        if not body.get("job_description", "").strip():
            _json_response(self, 400, {"error": "job_description is required"})
            return

        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        outfile = SESSION_DIR / "current_search.json"
        outfile.write_text(json.dumps(body, indent=2), encoding="utf-8")

        ts = datetime.now(timezone.utc).isoformat()
        _json_response(self, 200, {"status": "saved", "written_at": ts})

    def _post_upload_icims(self):
        content_type = self.headers.get("Content-Type", "")

        # cgi.FieldStorage needs a file-like rfile and the headers.
        # We must supply environ-style dict for it to parse multipart.
        length = int(self.headers.get("Content-Length", 0))
        raw_bytes = self.rfile.read(length)

        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(length),
        }

        form = cgi.FieldStorage(
            fp=io.BytesIO(raw_bytes),
            headers=self.headers,
            environ=environ,
        )

        if "file" not in form:
            _json_response(self, 400, {"error": "No file field in form data"})
            return

        file_item = form["file"]
        filename = file_item.filename or ""

        if not filename.lower().endswith(".csv"):
            _json_response(self, 400, {"error": "Only .csv files accepted"})
            return

        IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
        dest = IMPORTS_DIR / Path(filename).name
        dest.write_bytes(file_item.file.read())

        _json_response(self, 200, {"status": "uploaded", "filename": dest.name})

    def _post_save_note(self):
        try:
            body = _read_json_body(self)
        except Exception:
            _json_response(self, 400, {"error": "invalid JSON body"})
            return

        candidate_id = body.get("candidate_id", "").strip()
        note = body.get("note")

        if not candidate_id or note is None:
            _json_response(self, 400, {"error": "candidate_id and note are required"})
            return

        candidate_file = CANDIDATES_DIR / f"{candidate_id}.json"
        if not candidate_file.is_file():
            _json_response(self, 404, {"error": f"Candidate {candidate_id} not found"})
            return

        try:
            candidate = json.loads(candidate_file.read_text(encoding="utf-8"))
        except Exception as exc:
            _json_response(self, 500, {"error": f"Could not parse candidate file: {exc}"})
            return

        candidate["notes"] = note
        candidate_file.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        _json_response(self, 200, {"status": "saved"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = "127.0.0.1"
    port = 5000
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port} — press Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.", flush=True)
        sys.exit(0)
