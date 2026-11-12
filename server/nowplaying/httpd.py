"""The small HTTP server the display fetches covers from.

  /art/<key>.jpg    a rendered cover (immutable: the key changes when the art does)
  /art/current.jpg  whatever is showing now
  /state.json       the same JSON that goes to MQTT, plus diagnostics
  /healthz          200 while audio is arriving, 503 otherwise
  /                 a browser preview of the 480x480 panel

Responses always carry Content-Length and are never chunked, which is what
simple embedded HTTP clients expect.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

from .artwork import ArtCache
from .config import HttpConfig

log = logging.getLogger(__name__)

_ART_PATH = re.compile(r"^/art/([a-z0-9]{1,40})\.jpg$")


def detect_lan_ip() -> str:
    """The address this machine uses to reach the LAN (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NowPlaying"
    sys_version = ""
    timeout = 20            # drop idle keep-alive connections

    def do_GET(self) -> None:           # noqa: N802 (http.server naming)
        self._route(body=True)

    def do_HEAD(self) -> None:          # noqa: N802
        self._route(body=False)

    def _route(self, body: bool) -> None:
        app: ArtServer = self.server.app            # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PREVIEW_HTML.encode("utf-8"),
                           "no-cache", body)
            elif path == "/state.json":
                self._send(200, "application/json", _json(app.state()), "no-store", body)
            elif path == "/healthz":
                health = app.health()
                self._send(200 if health.get("ok") else 503, "application/json",
                           _json(health), "no-store", body)
            elif (found := _ART_PATH.match(path)):
                key = found.group(1)
                cache_control = "public, max-age=604800, immutable"
                if key == "current":
                    key = app.current_key() or ""
                    cache_control = "no-cache"
                data = app.cache.read(key) if key else None
                if data is None:
                    self._send(404, "text/plain; charset=utf-8", b"no such cover\n",
                               "no-store", body)
                else:
                    self._send(200, "image/jpeg", data, cache_control, body)
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found\n", "no-store", body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _send(self, status: int, content_type: str, payload: bytes, cache_control: str,
              body: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:     # noqa: A002
        log.debug("http %s %s", self.address_string(), format % args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    app: "ArtServer"

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("http client %s went away", client_address, exc_info=True)


class ArtServer:
    def __init__(
        self,
        cfg: HttpConfig,
        cache: ArtCache,
        state: Callable[[], dict],
        health: Callable[[], dict],
        current_key: Callable[[], str | None],
    ) -> None:
        self.cfg = cfg
        self.cache = cache
        self.state = state
        self.health = health
        self.current_key = current_key
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.port = cfg.port

    def start(self) -> None:
        server = _Server((self.cfg.host, self.cfg.port), _Handler)
        server.app = self
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
        self._thread.start()
        log.info("art server listening on %s:%d (panel URL base %s)",
                 self.cfg.host, self.port, self.base_url)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        """The address the display must use to reach this server."""
        if self.cfg.public_base_url:
            return self.cfg.public_base_url.rstrip("/")
        return f"http://{detect_lan_ip()}:{self.port}"

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/art/{key}.jpg"


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


PREVIEW_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Now Playing preview</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0e0f12; color: #d7d9de; font: 14px/1.45 system-ui, sans-serif; }
  main { display: grid; gap: 18px; justify-items: center; padding: 28px 16px; }
  #panel { width: 480px; height: 480px; max-width: 92vw; max-height: 92vw; position: relative;
           background: #000 center / cover no-repeat; border-radius: 10px; overflow: hidden;
           box-shadow: 0 0 0 10px #1b1d22, 0 14px 40px rgba(0, 0, 0, .6); }
  #scrim { position: absolute; left: 0; right: 0; bottom: 0; padding: 80px 20px 18px;
           background: linear-gradient(transparent, rgba(0, 0, 0, .78) 55%, rgba(0, 0, 0, .9)); }
  #scrim div { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #title { font-size: 26px; font-weight: 650; color: #fff; }
  #artist { font-size: 19px; color: #e3e5ea; }
  #album { font-size: 15px; color: #a9adb6; }
  #badge { position: absolute; top: 14px; left: 14px; padding: 3px 10px; border-radius: 99px;
           background: rgba(0, 0, 0, .62); font-size: 12px; letter-spacing: .06em;
           text-transform: uppercase; }
  .idle #scrim { display: none; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 3px 16px; margin: 0;
       width: 480px; max-width: 92vw; }
  dt { color: #8b909a; }
  dd { margin: 0; overflow-wrap: anywhere; }
  p { margin: 0; color: #8b909a; max-width: 480px; text-align: center; }
</style>
</head>
<body>
<main>
  <div id="panel" class="idle">
    <div id="badge">connecting</div>
    <div id="scrim"><div id="title"></div><div id="artist"></div><div id="album"></div></div>
  </div>
  <dl id="meta"></dl>
  <p>Browser preview of what the 480 &times; 480 panel is sent. It is not the device firmware.</p>
</main>
<script>
  const el = (id) => document.getElementById(id);
  const fields = [["source", "Identified by"], ["art_source", "Cover from"],
                  ["confidence", "Confidence"], ["level_dbfs", "Input level (dBFS)"],
                  ["is_music", "Music detected"], ["music", "Music score"],
                  ["speech", "Speech score"],
                  ["listening", "Listening"], ["capture_ok", "Audio arriving"],
                  ["mqtt_connected", "MQTT connected"], ["updated", "Updated"]];
  let lastArt = "";
  async function refresh() {
    try {
      const reply = await fetch("state.json", { cache: "no-store" });
      const s = await reply.json();
      el("panel").className = s.state === "playing" ? "" : "idle";
      el("badge").textContent = s.state || "unknown";
      el("title").textContent = s.title || "";
      el("artist").textContent = s.artist || "";
      el("album").textContent = s.album || "";
      if (s.art_url && s.art_url !== lastArt) {
        lastArt = s.art_url;
        el("panel").style.backgroundImage = 'url("' + new URL(s.art_url).pathname + '")';
      }
      const meta = el("meta");
      meta.replaceChildren();
      for (const [key, label] of fields) {
        if (s[key] === undefined || s[key] === null || s[key] === "") continue;
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = label;
        dd.textContent = String(s[key]);
        meta.append(dt, dd);
      }
    } catch (error) {
      el("badge").textContent = "server unreachable";
    }
  }
  refresh();
  setInterval(refresh, 2000);
</script>
</body>
</html>
"""
