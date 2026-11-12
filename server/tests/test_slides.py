"""Pictures shown in turn while nothing is playing (the idle slideshow)."""

import io

from PIL import Image

from nowplaying.artwork import SLIDES_RESCAN, ArtCache, ArtworkService
from nowplaying.config import ArtworkConfig

from helpers import FakeClock
from test_artwork import assert_panel_jpeg
from test_service import HEY_JUDE, Rig, hit


def picture(path, colour, size=(600, 400), fmt=None):
    Image.new("RGB", size, colour).save(path, fmt)
    return path


def folder(tmp_path):
    pics = tmp_path / "pics"
    pics.mkdir()
    picture(pics / "b-palm.png", (200, 30, 30))
    picture(pics / "a-ocean.jpg", (30, 30, 200))
    picture(pics / "C-cactus.PNG", (30, 200, 30), fmt="PNG")
    (pics / "notes.txt").write_text("not a picture", encoding="utf-8")
    (pics / "broken.jpg").write_bytes(b"this is not a jpeg")
    return pics


def service(tmp_path, pics, clock=None, **options):
    return ArtworkService(ArtworkConfig(**options), ArtCache(tmp_path / "art"), lambda url: b"", {},
                          clock=clock or FakeClock(), slides_dir=pics)


# -------------------------------------------------------------------- the folder

def test_pictures_are_converted_like_covers_in_name_order(tmp_path):
    art = service(tmp_path, folder(tmp_path))
    slides = art.slides()
    assert [s.origin for s in slides] == ["a-ocean.jpg", "b-palm.png", "C-cactus.PNG"]
    assert all(s.source == "slide" for s in slides)
    assert len({s.key for s in slides}) == 3
    blue = assert_panel_jpeg(art.cache.read(slides[0].key))
    assert blue.getpixel((240, 240))[2] > 150       # the ocean one, cropped to the square


def test_pictures_follow_the_configured_size(tmp_path):
    art = service(tmp_path, folder(tmp_path), size=420)
    assert_panel_jpeg(art.cache.read(art.slides()[0].key), size=420)


def test_no_folder_or_a_missing_one_means_no_slides(tmp_path):
    assert service(tmp_path, None).slides() == []
    assert service(tmp_path, tmp_path / "nowhere").slides() == []


def test_the_folder_is_read_again_only_once_a_minute(tmp_path):
    clock = FakeClock()
    pics = folder(tmp_path)
    art = service(tmp_path, pics, clock)
    assert len(art.slides()) == 3
    picture(pics / "d-new.jpg", (120, 120, 0))
    assert len(art.slides()) == 3                   # not looked at again yet
    clock.advance(SLIDES_RESCAN + 1)
    names = [s.origin for s in art.slides()]
    assert names == ["a-ocean.jpg", "b-palm.png", "C-cactus.PNG", "d-new.jpg"]
    (pics / "a-ocean.jpg").unlink()
    clock.advance(SLIDES_RESCAN + 1)
    assert [s.origin for s in art.slides()][0] == "b-palm.png"


def test_a_slide_pruned_from_the_cache_is_made_again(tmp_path):
    art = service(tmp_path, folder(tmp_path))
    first = art.slides()[0]
    art.cache.path(first.key).unlink()
    again = art.slides()
    assert again[0].key == first.key and art.cache.has(first.key)


# ------------------------------------------------------------------- the service

def test_idle_shows_the_pictures_in_turn(tmp_path):
    rig = Rig(tmp_path, slides_dir=folder(tmp_path), idle__slide_seconds=10.0)
    rig.capture.silence = 5.0                       # a quiet room: no lookups
    rig.svc._publish()                              # what run() does at start
    first = rig.last
    assert first["state"] == "idle" and first["art_source"] == "slide"
    rig.step(advance=9.0)
    assert len(rig.pub.states) == 1                 # the first picture is still up
    rig.step(advance=1.1)
    second = rig.last
    assert second["state"] == "idle" and second["art_url"] != first["art_url"]
    rig.step(advance=10.1)
    third = rig.last
    rig.step(advance=10.1)
    assert rig.last["art_url"] == first["art_url"]  # three pictures: round again
    assert len({first["art_url"], second["art_url"], third["art_url"]}) == 3
    assert rig.chain.calls == 0


def test_a_track_replaces_the_slides_and_they_resume_afterwards(tmp_path):
    rig = Rig(tmp_path, slides_dir=folder(tmp_path), idle__slide_seconds=10.0)
    rig.svc._publish()
    rig.chain.script.append(hit(HEY_JUDE))
    rig.step()
    assert rig.last["state"] == "playing" and rig.last["art_source"] == "shazam"
    count = len(rig.pub.states)
    rig.step(advance=10.1)                          # still playing: no slide change
    rig.step(advance=10.1)
    assert all(s["state"] == "playing" for s in rig.pub.states[count:])
    rig.capture.silence = rig.det.idle_after + 1    # the music stops
    rig.step(advance=1.0)
    assert rig.last["state"] == "idle" and rig.last["art_source"] == "slide"
    shown = rig.last["art_url"]
    rig.step(advance=10.1)
    assert rig.last["art_url"] != shown


def test_one_picture_is_shown_without_republishing(tmp_path):
    pics = tmp_path / "one"
    pics.mkdir()
    picture(pics / "only.jpg", (90, 90, 90))
    rig = Rig(tmp_path, slides_dir=pics)
    rig.capture.silence = 5.0
    rig.svc._publish()
    assert rig.last["art_source"] == "slide"
    for _ in range(3):
        rig.step(advance=10.1)
    assert len(rig.pub.states) == 1


def test_without_pictures_the_plain_tile_is_used(tmp_path):
    rig = Rig(tmp_path)
    rig.capture.silence = 5.0
    rig.svc._publish()
    rig.step(advance=30.0)
    assert rig.last["art_source"] == "idle" and len(rig.pub.states) == 1


def test_listening_off_keeps_showing_the_pictures(tmp_path):
    rig = Rig(tmp_path, slides_dir=folder(tmp_path), idle__slide_seconds=10.0)
    rig.svc._publish()
    assert rig.last["art_source"] == "slide"
    rig.svc.handle_command("listening", b"OFF")
    rig.step()
    assert rig.last["state"] == "off" and rig.last["art_source"] == "slide"
    shown = rig.last["art_url"]
    rig.step(advance=9.0)
    assert rig.last["art_url"] == shown             # not yet
    rig.step(advance=1.1)
    assert rig.last["state"] == "off" and rig.last["art_source"] == "slide"
    assert rig.last["art_url"] != shown             # the next picture, still "off"
    assert rig.chain.calls == 0                     # and no lookups while off
    rig.svc.handle_command("listening", b"ON")
    rig.step()
    assert rig.last["state"] in ("idle", "playing")


def test_listening_off_without_pictures_shows_the_plain_tile(tmp_path):
    rig = Rig(tmp_path)
    rig.svc._publish()
    rig.svc.handle_command("listening", b"OFF")
    rig.step()
    assert rig.last["state"] == "off" and rig.last["art_source"] == "idle"
    count = len(rig.pub.states)
    rig.step(advance=30.0)
    assert len(rig.pub.states) == count
