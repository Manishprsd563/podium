"""Static file + token-mint server for the Podium web client.

Stdlib-only. Serves web/index.html, app.js, style.css; mints short-lived
LiveKit room-join tokens (the browser can never hold LIVEKIT_API_SECRET);
and serves rendered clip/session artifacts from the repo's sessions/ dir.

This is a local development server, not a production edge. /token is
unauthenticated by design -- anyone who can reach the port can mint a join
token -- so it is bound to the loopback interface, refuses cross-origin reads,
and issues tokens that expire in TOKEN_TTL_S. Exposing this port to a network
would hand out room access to that network.
"""

from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _reexec_under_venv() -> None:
    """`python web\\serve.py` from a shell whose `python` is the system interpreter
    starts fine (stdlib server) and then fails every /token request with
    "No module named 'livekit'", which the browser shows as a coach that never
    connects. If the deps are missing here but the project venv exists, run the
    server under the venv interpreter instead of serving a broken /token."""
    if importlib.util.find_spec("livekit") and importlib.util.find_spec("dotenv"):
        return
    repo_root = Path(__file__).resolve().parent.parent
    candidates = (repo_root / ".venv" / "Scripts" / "python.exe",
                  repo_root / ".venv" / "bin" / "python")
    venv_python = next((p for p in candidates if p.is_file()), None)
    if venv_python is None or Path(sys.executable).resolve() == venv_python.resolve():
        sys.exit(f"serve.py: 'livekit' / 'python-dotenv' are not installed for "
                 f"{sys.executable}. Run it with the project venv interpreter "
                 f"(.venv\\Scripts\\python.exe web\\serve.py).")
    print(f"serve.py: {sys.executable} lacks livekit; re-running under {venv_python}",
          file=sys.stderr)
    # subprocess rather than os.execv: on Windows execv spawns a detached child
    # and returns the console to the shell while the server is still running.
    try:
        sys.exit(subprocess.call([str(venv_python), *sys.argv]))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    _reexec_under_venv()

from dotenv import load_dotenv  # noqa: E402  (after the interpreter guard on purpose)

WEB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEB_ROOT.parent
SESSIONS_ROOT = REPO_ROOT / "sessions"
DEFAULT_PORT = 8090  # 8080 collides with Docker Desktop's backend on this machine

load_dotenv(REPO_ROOT / ".env")

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("audio/wav", ".wav")


# A rehearsal never outlives this, and a leaked token stops working quickly.
# Without an explicit TTL, livekit-api's default is six hours.
TOKEN_TTL_S = 3600

# Room and identity end up inside a signed grant, so they are restricted to the
# shapes the client actually generates ("podium-<8 hex>" and "presenter-<n>")
# rather than passed through verbatim.
_TOKEN_ARG_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def mint_token(room: str, identity: str) -> str:
    """A join-only LiveKit JWT for one room, valid for TOKEN_TTL_S."""
    # Imported lazily so a missing/broken livekit-api install fails the
    # /token request with a clear JSON error instead of the whole server.
    from livekit.api import AccessToken, VideoGrants

    token = (
        AccessToken(api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_ttl(timedelta(seconds=TOKEN_TTL_S))
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
    """Dispatches GET to /token (LiveKit join JWT), /sessions/<...> (rendered clip and
    session artifacts), or a static file under WEB_ROOT; every filesystem read goes
    through safe_join so no request path can escape its root."""

    server_version = "PodiumServe/1.0"
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict) -> None:
        # Deliberately no Access-Control-Allow-Origin: /token answers on this
        # path, and a wildcard would let any page the presenter has open read a
        # join token for this LiveKit project.
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        # no-store: session clips are re-cut under the same URL on a rerecord/retry, and
        # a stale cached take would be indistinguishable from a fresh one.
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

    # Static files (index.html/app.js/style.css/...), rendered clip/session artifacts
    # under /sessions/, and the /token JWT mint are the server's only three routes.

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
        # A browser labels a cross-site request "cross-site" in Sec-Fetch-Site;
        # the client's own fetch is "same-origin". Refusing anything else stops
        # another page in the presenter's browser from reading a join token,
        # which is the one thing an unauthenticated mint endpoint must not
        # allow. Non-browser callers (curl, the harnesses) send no such header
        # and are still served.
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site not in ("same-origin", "none"):
            self._send_json(403, {"error": "cross-site token requests are refused"})
            return
        qs = urllib.parse.parse_qs(query)
        room = (qs.get("room") or [""])[0]
        identity = (qs.get("identity") or [""])[0]
        if not room or not identity:
            self._send_json(400, {"error": "room and identity query params are required"})
            return
        if not (_TOKEN_ARG_RE.match(room) and _TOKEN_ARG_RE.match(identity)):
            self._send_json(400, {"error": "room and identity must match [A-Za-z0-9._-]{1,64}"})
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
        # Overrides BaseHTTPRequestHandler's default (adds a redundant "- -" and a
        # timestamp already visible in the terminal); this is the request log a dev
        # watches while iterating, so keep it one line and readable.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    # 127.0.0.1 only, never 0.0.0.0 -- this is a local dev server minting real LiveKit
    # tokens; it must not be reachable from the LAN.
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
