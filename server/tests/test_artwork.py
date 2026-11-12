import io
import os

import pytest
from PIL import Image

from nowplaying.artwork import (PLACEHOLDER_RETRY, ArtCache, ArtworkService, render_cover,
                                render_idle_cover, render_text_cover)
from nowplaying.config import ArtworkConfig
from nowplaying.models import Match

from helpers import FakeClock


def encode(image, fmt, **options):
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()


def frame_markers(data):
    """The JPEG segment markers before the first scan (0xC0 = baseline, 0xC2 = progressive)."""
    assert data[:2] == b"\xff\xd8"
    markers, index = [], 2
    while index < len(data):
        assert data[index] == 0xFF
        marker = data[index + 1]
        if marker == 0xDA:
            break
        markers.append(marker)
        index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    return markers


def assert_panel_jpeg(data, size=480):
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG"
    assert image.size == (size, size)
    assert image.mode == "RGB"
    markers = frame_markers(data)
    assert 0xC0 in markers, "must be a baseline JPEG"
    assert 0xC2 not in markers, "must not be progressive"
    return image.convert("RGB")


# --------------------------------------------------------------------- rendering

def test_progressive_jpeg_becomes_baseline():
    source = encode(Image.new("RGB", (1000, 1000), (200, 40, 40)), "JPEG", progressive=True)
    assert 0xC2 in frame_markers(source)            # the input really is progressive
    assert_panel_jpeg(render_cover(source, 480))


def test_png_with_alpha_is_flattened_on_black():
    image = Image.new("RGBA", (300, 300), (255, 255, 255, 0))
    out = assert_panel_jpeg(render_cover(encode(image, "PNG"), 480))
    assert max(out.getpixel((240, 240))) < 12       # transparent became black, not white


def test_palette_png_with_transparency():
    image = Image.new("P", (64, 64), 0)
    image.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    assert_panel_jpeg(render_cover(encode(image, "PNG", transparency=0), 480))


@pytest.mark.parametrize("mode,colour", [("CMYK", (0, 255, 255, 0)), ("L", 128), ("1", 1)])
def test_other_colour_modes(mode, colour):
    fmt = "JPEG" if mode != "1" else "PNG"
    assert_panel_jpeg(render_cover(encode(Image.new(mode, (500, 500), colour), fmt), 480))


def two_tone():
    image = Image.new("RGB", (600, 300), (255, 0, 0))
    image.paste((0, 0, 255), (300, 0, 600, 300))
    return encode(image, "PNG")


def test_cover_fit_crops_and_fills_the_square():
    out = assert_panel_jpeg(render_cover(two_tone(), 480, fit="cover"))
    left, right, top = out.getpixel((60, 240)), out.getpixel((420, 240)), out.getpixel((60, 4))
    assert left[0] > 200 and left[2] < 60
    assert right[2] > 200 and right[0] < 60
    assert top[0] > 200                             # picture reaches the edge: no letterbox bars


def test_contain_fit_letterboxes():
    out = assert_panel_jpeg(render_cover(two_tone(), 480, fit="contain"))
    assert max(out.getpixel((240, 4))) < 12         # black bar above the picture
    assert out.getpixel((60, 240))[0] > 200


def test_other_sizes():
    assert_panel_jpeg(render_cover(two_tone(), 240), size=240)


@pytest.mark.parametrize("data", [
    b"",
    b"this is not an image at all",
    encode(Image.new("RGB", (8, 8)), "PNG"),
    b"\xff\xd8\xff\xe0" + b"\x00" * 64,             # truncated JPEG
])
def test_unusable_input_raises_value_error(data):
    with pytest.raises(ValueError):
        render_cover(data, 480)


def test_generated_covers():
    one = render_text_cover("Hey Jude", "The Beatles", 480)
    two = render_text_cover("Let It Be", "The Beatles", 480)
    assert_panel_jpeg(one)
    assert one != two
    assert one == render_text_cover("Hey Jude", "The Beatles", 480)     # deterministic
    assert_panel_jpeg(render_text_cover("A" * 300, "Supercalifragilistic " * 30, 480))
    assert_panel_jpeg(render_text_cover("", "", 480))
    assert_panel_jpeg(render_text_cover("夜に駆ける", "YOASOBI", 480))     # must not crash
    assert_panel_jpeg(render_idle_cover(480))


# ------------------------------------------------------------------------- cache

def test_cache_roundtrip_and_no_temp_files(tmp_path):
    cache = ArtCache(tmp_path / "art")
    assert cache.read("abc") is None and not cache.has("abc")
    cache.write("abc", b"jpeg-bytes")
    assert cache.has("abc") and cache.read("abc") == b"jpeg-bytes"
    assert [p.name for p in (tmp_path / "art").iterdir()] == ["abc.jpg"]


def test_cache_prunes_least_recently_used(tmp_path):
    cache = ArtCache(tmp_path, max_files=3)
    for index in range(6):
        cache.write(f"k{index}", b"x")
        os.utime(cache.path(f"k{index}"), (1000 + index, 1000 + index))
    cache.read("k0")                                # using a cover keeps it
    assert cache.prune() == 3
    assert sorted(p.stem for p in tmp_path.glob("*.jpg")) == ["k0", "k4", "k5"]


# ----------------------------------------------------------------------- service

PNG = encode(Image.new("RGB", (300, 300), (10, 200, 90)), "PNG")
TRACK = Match(title="Hey Jude", artist="The Beatles", source="shazam")


class Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise RuntimeError("HTTP 404")
        return self.pages[url]


def service(tmp_path, pages, finders, clock=None, **cfg):
    fetch = Fetcher(pages)
    svc = ArtworkService(ArtworkConfig(**cfg), ArtCache(tmp_path), fetch, finders,
                         clock=clock or FakeClock())
    return svc, fetch


def test_sources_are_tried_in_configured_order(tmp_path):
    finders = {
        "recognizer": lambda t: [("http://a/missing.jpg", "shazam")],
        "coverartarchive": lambda t: [("http://b/front.jpg", "coverartarchive")],
        "itunes": lambda t: [("http://c/never.jpg", "itunes")],
    }
    svc, fetch = service(tmp_path, {"http://b/front.jpg": PNG, "http://c/never.jpg": PNG}, finders)
    art = svc.resolve(TRACK)
    assert art.source == "coverartarchive" and art.origin == "http://b/front.jpg"
    assert fetch.calls == ["http://a/missing.jpg", "http://b/front.jpg"]     # stopped at the hit
    assert_panel_jpeg(svc.cache.read(art.key))


def test_second_resolve_uses_no_network(tmp_path):
    svc, fetch = service(tmp_path, {"http://a/x.jpg": PNG},
                         {"recognizer": lambda t: [("http://a/x.jpg", "shazam")]})
    assert svc.cached(TRACK) is None
    first = svc.resolve(TRACK)
    assert svc.cached(TRACK) == first
    assert svc.resolve(Match(title="Hey Jude - Remastered", artist="Beatles")) == first
    assert len(fetch.calls) == 1


def test_dead_urls_are_not_refetched(tmp_path):
    finders = {"recognizer": lambda t: [("http://a/dead.jpg", "shazam")]}
    svc, fetch = service(tmp_path, {}, finders)
    assert svc.resolve(TRACK).source == "placeholder"
    assert svc.resolve(Match(title="Other Song", artist="Someone")).source == "placeholder"
    assert fetch.calls == ["http://a/dead.jpg"]


def test_placeholder_when_nothing_found_and_retry_later(tmp_path):
    clock = FakeClock()
    pages = {}
    svc, fetch = service(tmp_path, pages, {"itunes": lambda t: [("http://c/late.jpg", "itunes")]},
                         clock=clock)
    art = svc.resolve(TRACK)
    assert art.source == "placeholder"
    assert_panel_jpeg(svc.cache.read(art.key))
    assert svc.cached(TRACK) == art                  # no lookups while the placeholder is fresh
    clock.advance(PLACEHOLDER_RETRY + 3600 + 1)      # past the retry time and the dead-URL memory
    pages["http://c/late.jpg"] = PNG
    assert svc.cached(TRACK) is None
    assert svc.resolve(TRACK).source == "itunes"


def test_broken_source_and_corrupt_image_fall_through(tmp_path):
    def boom(track):
        raise RuntimeError("service down")

    finders = {
        "recognizer": boom,
        "coverartarchive": lambda t: [("http://b/corrupt.jpg", "coverartarchive")],
        "itunes": lambda t: [("http://c/ok.jpg", "itunes")],
    }
    svc, _ = service(tmp_path, {"http://b/corrupt.jpg": b"garbage", "http://c/ok.jpg": PNG}, finders)
    assert svc.resolve(TRACK).source == "itunes"


def test_disabled_sources_are_skipped(tmp_path):
    finders = {
        "recognizer": lambda t: [("http://a/x.jpg", "shazam")],
        "itunes": lambda t: [("http://c/ok.jpg", "itunes")],
    }
    svc, fetch = service(tmp_path, {"http://a/x.jpg": PNG, "http://c/ok.jpg": PNG}, finders,
                         sources=["itunes"])
    assert svc.resolve(TRACK).source == "itunes"
    assert fetch.calls == ["http://c/ok.jpg"]


def test_idle_cover_and_size_specific_keys(tmp_path):
    small, _ = service(tmp_path, {"http://a/x.jpg": PNG},
                       {"recognizer": lambda t: [("http://a/x.jpg", "s")]}, size=240)
    large, _ = service(tmp_path, {"http://a/x.jpg": PNG},
                       {"recognizer": lambda t: [("http://a/x.jpg", "s")]}, size=480)
    a, b = small.resolve(TRACK), large.resolve(TRACK)
    assert a.key != b.key                            # a config change never serves stale sizes
    assert_panel_jpeg(small.cache.read(a.key), size=240)
    assert_panel_jpeg(large.cache.read(large.idle().key))
