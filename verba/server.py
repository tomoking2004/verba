"""The HTTP server that serves the UI (ui/) and the local API, on 127.0.0.1 only."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from .config import MODELS, THEME_IDS, THEMES, UI_DIR, VERSION
from .controller import Controller
from .errors import UserError
from .store import NotFound
from .themes import icon_svg

log = logging.getLogger(__name__)

_SESSION = r"/api/sessions/([0-9_\-]+)"


def make_server(controller: Controller, port: int, on_quit: Callable[[], None],
                on_theme: Callable[[str], None] = lambda theme: None) -> ThreadingHTTPServer:
    """Build the server. on_quit is called when the UI asks to stop recording and quit, on_theme when the theme changes."""
    routes: list[tuple[str, re.Pattern, Callable]] = []
    host, origin = f"127.0.0.1:{port}", f"http://127.0.0.1:{port}"
    settings_lock = threading.Lock()  # So the window wears the theme saved last when two changes cross

    def route(method: str, pattern: str):
        def register(fn):
            routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return register

    @route("GET", "/api/state")
    def _state(h, body):
        return controller.state()

    @route("GET", "/api/config")
    def _config(h, body):
        return {"settings": controller.settings.all(), "models": MODELS, "themes": THEMES,
                "sessions_dir": str(controller.store.root), "version": VERSION, "newer": controller.store.newer()}

    @route("GET", "/api/devices")
    def _devices(h, body):
        return controller.devices()

    @route("GET", "/api/usage")
    def _usage(h, body):
        return controller.usage.snapshot()

    @route("POST", "/api/settings")
    def _settings(h, body):
        with settings_lock:
            theme = controller.settings.get("theme")
            settings = controller.settings.update(body)
            if settings["theme"] != theme:
                on_theme(settings["theme"])
        return settings

    @route("POST", "/api/key")
    def _key(h, body):
        return controller.set_api_key(str(body.get("key", "")))

    @route("POST", "/api/recording/start")
    def _start(h, body):
        return controller.start(body.get("title", ""), body.get("memo", ""), body.get("muted"))

    @route("POST", "/api/recording/mute")
    def _mute(h, body):
        controller.mute(body.get("me"), body.get("other"))
        return {"ok": True}

    @route("POST", "/api/recording/pause")
    def _pause(h, body):
        controller.pause()
        return {"ok": True}

    @route("POST", "/api/recording/resume")
    def _resume(h, body):
        controller.resume()
        return {"ok": True}

    @route("POST", "/api/recording/stop")
    def _stop(h, body):
        controller.stop()
        return {"ok": True}

    @route("GET", "/api/sessions")
    def _list(h, body):
        return controller.store.list(parse_qs(urlparse(h.path).query).get("q", [""])[0])

    @route("GET", _SESSION)
    def _get(h, body, sid):
        return controller.session(sid)

    @route("PATCH", _SESSION)
    def _patch(h, body, sid):
        return controller.update_session(sid, body)

    @route("DELETE", _SESSION)
    def _delete(h, body, sid):
        controller.delete_session(sid)
        return {"ok": True}

    @route("POST", _SESSION + "/retry")
    def _retry(h, body, sid):
        return {"queued": controller.retry(sid)}

    @route("POST", _SESSION + "/retranscribe")
    def _retranscribe(h, body, sid):
        controller.retranscribe(sid)
        return {"ok": True}

    @route("POST", _SESSION + "/cancel")
    def _cancel(h, body, sid):
        return {"restored": controller.cancel(sid)}

    @route("POST", _SESSION + "/version")
    def _version(h, body, sid):
        controller.switch_version(sid, body.get("index"))
        return {"ok": True}

    @route("POST", _SESSION + "/fix-names")
    def _fix_names(h, body, sid):
        controller.fix_names(sid)
        return {"ok": True}

    @route("POST", _SESSION + "/summarize")
    def _summarize(h, body, sid):
        controller.summarize(sid)
        return {"ok": True}

    @route("POST", _SESSION + "/reveal")
    def _reveal(h, body, sid):
        controller.store.get(sid)  # Raises NotFound if the session is gone.
        os.startfile(controller.store.folder(sid))  # Opens the folder in Explorer.
        return {"ok": True}

    @route("GET", _SESSION + "/audio")
    def _audio(h, body, sid):
        h.send_file(controller.store.audio_path(sid), "audio/ogg")

    @route("POST", "/api/quit")
    def _quit(h, body):
        on_quit()
        return {"ok": True}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            # Only the app's own page is served. A page of another site, open in a browser on this PC, could
            # otherwise post to the API, or read the sessions through a host name it points at 127.0.0.1.
            if self.headers.get("Host") != host or self.headers.get("Origin", origin) != origin:
                return self.send_json(403, {"error": "forbidden"})
            url = urlparse(self.path)
            path = url.path
            if not path.startswith("/api/"):
                if method != "GET":
                    return self.send_json(404, {"error": "not found"})
                return self.send_static(path, parse_qs(url.query))
            for m, pattern, fn in routes:
                match = pattern.match(path)
                if m == method and match:
                    try:
                        body = self.read_json() if method in ("POST", "PATCH") else {}
                        result = fn(self, body, *match.groups())
                        if result is not None:
                            self.send_json(200, result)
                    except UserError as exc:
                        self.send_json(404 if isinstance(exc, NotFound) else 400, {"error": str(exc)})
                    except ConnectionError:
                        # The UI went away mid-response (seeking in the audio drops the previous request).
                        self.close_connection = True
                    except Exception:  # noqa: BLE001
                        log.exception("%s %s failed", method, path)
                        # Exception text (paths, codes) never reaches the screen; the line above keeps it in app.log.
                        self.send_json(500, {"error": "処理に失敗しました。もう一度試してください"})
                    return
            self.send_json(404, {"error": "not found"})

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def send_json(self, code: int, obj) -> None:
            self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

        def send_static(self, path: str, query: dict) -> None:
            name = unquote(path).lstrip("/") or "index.html"
            theme = controller.settings.get("theme")
            if name == "index.html":  # The page is painted in the theme from the first frame.
                page = (UI_DIR / name).read_text(encoding="utf-8").replace("<html ", f'<html data-theme="{theme}" ', 1)
                return self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
            if name == "icon.svg":  # In the theme asked for (the choices in the settings), or the current one.
                asked = query.get("theme", [theme])[0]
                return self.send_bytes(icon_svg(asked if asked in THEME_IDS else theme).encode("utf-8"), "image/svg+xml")
            target = (UI_DIR / name).resolve()
            if UI_DIR.resolve() not in target.parents or not target.is_file():
                return self.send_json(404, {"error": "not found"})
            self.send_file(target, mimetypes.guess_type(target.name)[0] or "application/octet-stream")

        def send_bytes(self, data: bytes, content_type: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_file(self, path, content_type: str) -> None:
            """Send a file, honouring Range requests (needed to seek in the audio)."""
            try:
                size = os.path.getsize(path)
            except OSError:
                return self.send_json(404, {"error": "ファイルがありません"})
            start, end = 0, size - 1
            match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            ranged = bool(match and (match.group(1) or match.group(2)))
            if ranged:
                if match.group(1):
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else size - 1
                else:
                    start = max(0, size - int(match.group(2)))
                end = min(end, size - 1)
                if start > end:  # The range starts past the end of the file.
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if ranged else 200)
            if content_type.startswith("text/") or content_type.endswith("javascript"):
                content_type += "; charset=utf-8"
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "no-store")
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def log_message(self, *args) -> None:
            pass

    return _Server(("127.0.0.1", port), Handler)


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is in use,
    # which would defeat the second-instance check, so turn it off.
    allow_reuse_address = False
    daemon_threads = True
