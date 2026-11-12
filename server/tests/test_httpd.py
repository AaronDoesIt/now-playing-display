import http.client
import json

import pytest

from nowplaying.artwork import ArtCache
from nowplaying.config import HttpConfig
from nowplaying.httpd import ArtServer, detect_lan_ip

COVER = b"\xff\xd8fake-jpeg-bytes\xff\xd9"


@pytest.fixture
def server(tmp_path):
    cache = ArtCache(tmp_path)
    cache.write("c0123abc", COVER)
    cache.write("idle480", b"idle-cover")
    box = {"state": {"state": "playing", "title": "Tïtle"}, "health": {"ok": True},
           "current": "c0123abc"}
    srv = ArtServer(HttpConfig(host="127.0.0.1", port=0), cache,
                    state=lambda: box["state"], health=lambda: box["health"],
                    current_key=lambda: box["current"])
    srv.start()
    srv.box = box
    yield srv
    srv.stop()


def request(server, method, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.request(method, path)
        reply = conn.getresponse()
        return reply.status, dict(reply.getheaders()), reply.read()
    finally:
        conn.close()


def test_serves_a_cover_with_length_and_long_cache(server):
    status, headers, body = request(server, "GET", "/art/c0123abc.jpg")
    assert status == 200 and body == COVER
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Content-Length"] == str(len(COVER))
    assert "immutable" in headers["Cache-Control"]
    assert "Transfer-Encoding" not in headers        # never chunked


def test_head_has_headers_but_no_body(server):
    status, headers, body = request(server, "HEAD", "/art/c0123abc.jpg")
    assert status == 200 and body == b""
    assert headers["Content-Length"] == str(len(COVER))


def test_current_follows_the_playing_track(server):
    status, headers, body = request(server, "GET", "/art/current.jpg")
    assert (status, body) == (200, COVER)
    assert headers["Cache-Control"] == "no-cache"
    server.box["current"] = "idle480"
    assert request(server, "GET", "/art/current.jpg")[2] == b"idle-cover"
    server.box["current"] = None
    assert request(server, "GET", "/art/current.jpg")[0] == 404


@pytest.mark.parametrize("path", [
    "/art/missing.jpg",
    "/art/../config.toml",
    "/art/..%2f..%2fsecret.jpg",
    "/art/C0123ABC.jpg",                 # keys are lower case only
    "/art/c0123abc.png",
    "/art/",
    "/nothing-here",
])
def test_unknown_paths_are_404(server, path):
    assert request(server, "GET", path)[0] == 404


def test_state_health_and_preview(server):
    status, headers, body = request(server, "GET", "/state.json")
    assert status == 200 and json.loads(body) == {"state": "playing", "title": "Tïtle"}
    assert request(server, "GET", "/healthz")[0] == 200
    server.box["health"] = {"ok": False, "reason": "no audio"}
    status, _, body = request(server, "GET", "/healthz")
    assert status == 503 and json.loads(body)["reason"] == "no audio"
    status, headers, body = request(server, "GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"state.json" in body


def test_keep_alive_serves_several_requests_on_one_connection(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        for _ in range(3):
            conn.request("GET", "/art/c0123abc.jpg")
            reply = conn.getresponse()
            assert reply.status == 200 and reply.read() == COVER
    finally:
        conn.close()


def test_urls(server, tmp_path):
    assert server.url_for("c0123abc") == f"http://{detect_lan_ip()}:{server.port}/art/c0123abc.jpg"
    fixed = ArtServer(HttpConfig(public_base_url="http://10.0.0.151:8777/"), ArtCache(tmp_path),
                      state=dict, health=dict, current_key=lambda: None)
    assert fixed.url_for("k") == "http://10.0.0.151:8777/art/k.jpg"
