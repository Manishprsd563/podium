"""Static file + token-mint server for the Podium web client.

Stdlib-only. Serves web/index.html, app.js, style.css; mints short-lived
LiveKit room-join tokens (the browser can never hold LIVEKIT_API_SECRET);
and serves rendered clip/session artifacts from the repo's sessions/ dir.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

WEB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEB_ROOT.parent
SESSIONS_ROOT = REPO_ROOT / "sessions"
DEFAULT_PORT = 8080

load_dotenv(REPO_ROOT / ".env")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("audio/wav", ".wav")


def mint_token(room: str, identity: str) -> str:
    # Imported lazily so a missing/broken livekit-api install fails the
    # /token request with a clear JSON error instead of the whole server.
    from livekit.api import AccessToken, VideoGrants

    token = (
        AccessToken(api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(
            VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
        )
    )
    return token.to_jwt()


def safe_join(root: Path, rel_url_path: str) -> Path | None:
    """Resolve rel_url_path under root, refusing any escape via `..` etc."""
    rel = urllib.parse.unquote(rel_url_path).lstrip("/")
    resolved_root = root.resolve()
    candidate = (resolved_root / rel).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


class PodiumHandler(BaseHTTPRequestHandler):
    server_version = "PodiumServe/1.0"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if path is None or not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        ctype, _ = mimetypes.guess_type(str(path))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/token":
            self._handle_token(parsed.query)
            return

        if path.startswith("/sessions/"):
            rel = path[len("/sessions/") :]
            target = safe_join(SESSIONS_ROOT, rel)
            if target is None:
                self._send_json(403, {"error": "forbidden"})
                return
            self._send_file(target)
            return

        rel = path.lstrip("/") or "index.html"
        target = safe_join(WEB_ROOT, rel)
        if target is None:
            self._send_json(403, {"error": "forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        self._send_file(target)

    def _handle_token(self, query: str) -> None:
        qs = urllib.parse.parse_qs(query)
        room = (qs.get("room") or [""])[0]
        identity = (qs.get("identity") or [""])[0]
        if not room or not identity:
            self._send_json(400, {"error": "room and identity query params are required"})
            return
        if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
            self._send_json(500, {"error": "LiveKit credentials not configured in .env"})
            return
        try:
            jwt = mint_token(room, identity)
        except Exception as exc:  # surfaces bad key/secret etc. as JSON, not a stack trace
            self._send_json(500, {"error": f"token mint failed: {exc}"})
            return
        self._send_json(200, {"url": LIVEKIT_URL, "token": jwt})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), PodiumHandler)
    print(f"Podium web server on http://127.0.0.1:{port}  (serving {WEB_ROOT})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
