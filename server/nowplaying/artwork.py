"""Cover art: find it, convert it to exactly what the panel shows, cache it.

Whatever the source serves (PNG, progressive JPEG, CMYK, 3000 px scans, HTTPS
behind redirects) is turned into one square baseline JPEG at the panel's
resolution. The ESP32 then only ever fetches a small plain-HTTP file from this
machine and never needs TLS, redirects, scaling or a progressive decoder.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import ArtworkConfig
from .models import Match

log = logging.getLogger(__name__)

MAX_SOURCE_PIXELS = 50_000_000      # refuse absurd images before decoding them
NEGATIVE_TTL = 3600.0               # remember a dead art URL for an hour
PLACEHOLDER_RETRY = 1800.0          # look for real art again after this long
SLIDES_RESCAN = 60.0                # look at the idle pictures folder again this often
MAX_SLIDES = 200
SLIDE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

#: finder(track) yields (url, label) candidates, cheapest and best first
Finder = Callable[[Match], Iterable[tuple[str, str]]]


@dataclass(frozen=True)
class Art:
    key: str            # cache key, also the file name served as /art/<key>.jpg
    source: str         # where it came from: shazam, coverartarchive, itunes, placeholder...
    origin: str = ""    # the remote URL it was made from


# --------------------------------------------------------------------- rendering

def _save_jpeg(image: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    # Baseline (non-progressive), standard Huffman tables, 4:2:0: the most widely
    # decodable JPEG there is. Small embedded decoders reject progressive files.
    image.save(out, "JPEG", quality=quality, progressive=False, optimize=False, subsampling=2)
    return out.getvalue()


def render_cover(data: bytes, size: int, fit: str = "cover", quality: int = 85) -> bytes:
    """Convert any image to a size x size baseline RGB JPEG. Raises ValueError if unusable."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width < 16 or height < 16:
                raise ValueError(f"image too small ({width}x{height})")
            if width * height > MAX_SOURCE_PIXELS:
                raise ValueError(f"image too large ({width}x{height})")
            if image.format == "JPEG":
                image.draft("RGB", (size * 2, size * 2))     # decode big JPEGs at reduced size
            image = ImageOps.exif_transpose(image)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (0, 0, 0))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                image = flat
            else:
                image = image.convert("RGB")
            if fit == "contain":
                image = ImageOps.pad(image, (size, size), Image.Resampling.LANCZOS, color=(0, 0, 0))
            else:
                image = ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)
            return _save_jpeg(image, quality)
    except ValueError:
        raise
    except Exception as exc:    # PIL raises many types for corrupt or unsupported files
        raise ValueError(f"not a usable image: {exc.__class__.__name__}: {exc}") from None


def _font(size: int, font_path: str = "") -> ImageFont.ImageFont:
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            log.warning("artwork.font_path %r could not be loaded; using the built-in font",
                        font_path)
    try:
        return ImageFont.load_default(size)
    except Exception:           # Pillow built without FreeType: fixed-size bitmap font
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int,
          max_lines: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    overflow = len(lines) > max_lines
    lines = lines[:max_lines]
    fitted: list[str] = []
    for index, line in enumerate(lines):
        mark = "..." if overflow and index == len(lines) - 1 else ""
        if draw.textlength(line + mark, font=font) > width:
            # one very long word, or no room left for the ellipsis: cut characters
            mark = "..."
            while len(line) > 1 and draw.textlength(line + mark, font=font) > width:
                line = line[:-1]
        fitted.append(line.rstrip() + mark)
    return fitted


def render_text_cover(title: str, artist: str, size: int, quality: int = 85,
                      font_path: str = "") -> bytes:
    """A generated cover (title and artist on a colour derived from them) for art-less tracks."""
    digest = hashlib.sha1(f"{artist}|{title}".encode("utf-8")).digest()
    hue = digest[0] / 255.0
    top = _hsv(hue, 0.45, 0.42)
    bottom = _hsv((hue + 0.08) % 1.0, 0.55, 0.14)
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / max(1, size - 1)
        draw.line([(0, y), (size, y)],
                  fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    margin = round(size * 0.08)
    width = size - 2 * margin
    title_font = _font(round(size * 0.105), font_path)
    artist_font = _font(round(size * 0.07), font_path)
    title_lines = _wrap(draw, title or "Unknown title", title_font, width, 4)
    artist_lines = _wrap(draw, artist or "", artist_font, width, 2)
    title_step = round(size * 0.125)
    artist_step = round(size * 0.085)
    y = size - margin - len(artist_lines) * artist_step - len(title_lines) * title_step
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=(255, 255, 255))
        y += title_step
    y += round(size * 0.01)
    for line in artist_lines:
        draw.text((margin, y), line, font=artist_font, fill=(225, 228, 235))
        y += artist_step
    return _save_jpeg(image, quality)


def render_idle_cover(size: int, quality: int = 85) -> bytes:
    """A dark tile with a pair of beamed notes, shown when nothing is playing."""
    image = Image.new("RGB", (size, size), (14, 15, 18))
    draw = ImageDraw.Draw(image)
    k = size / 480.0
    ink = (58, 61, 70)

    def box(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0 * k, y0 * k, x1 * k, y1 * k]

    draw.ellipse(box(150, 296, 222, 348), fill=ink)
    draw.ellipse(box(270, 266, 342, 318), fill=ink)
    draw.rectangle(box(208, 170, 222, 322), fill=ink)
    draw.rectangle(box(328, 140, 342, 292), fill=ink)
    draw.polygon([(208 * k, 170 * k), (342 * k, 140 * k), (342 * k, 178 * k), (208 * k, 208 * k)],
                 fill=ink)
    return _save_jpeg(image, quality)


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


# ------------------------------------------------------------------------- cache

class ArtCache:
    """Rendered covers on disk, pruned by age of last use."""

    def __init__(self, directory: Path, max_files: int = 500) -> None:
        self.directory = Path(directory)
        self.max_files = max_files
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._writes = 0

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.jpg"

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def read(self, key: str) -> bytes | None:
        path = self.path(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            os.utime(path, None)        # mark as recently used
        except OSError:
            pass
        return data

    def write(self, key: str, data: bytes) -> None:
        final = self.path(key)
        temp = self.directory / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        temp.write_bytes(data)
        os.replace(temp, final)         # atomic: a reader never sees half a file
        with self._lock:
            self._writes += 1
            due = self._writes % 25 == 0
        if due:
            self.prune()

    def prune(self) -> int:
        """Delete the least recently used covers beyond max_files. Returns how many."""
        with self._lock:
            files = []
            for path in self.directory.glob("*.jpg"):
                try:
                    files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            excess = len(files) - self.max_files
            if excess <= 0:
                return 0
            files.sort()
            removed = 0
            for _, path in files[:excess]:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
            return removed


# ----------------------------------------------------------------------- service

class ArtworkService:
    """Resolves a track to a cached cover, trying each configured source in order."""

    def __init__(
        self,
        cfg: ArtworkConfig,
        cache: ArtCache,
        fetch: Callable[[str], bytes],
        finders: dict[str, Finder],
        clock: Callable[[], float] = time.monotonic,
        slides_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache = cache
        self._fetch = fetch
        self._finders = finders
        self._clock = clock
        self._lock = threading.Lock()
        self._by_track: dict[str, tuple[Art, float]] = {}
        self._dead: dict[str, float] = {}
        self._slides_dir = Path(slides_dir) if slides_dir else None
        self._slides: list[Art] = []
        self._slides_scanned: float | None = None
        self._slide_files: dict[str, tuple[float, int, str]] = {}   # name -> (mtime, size, key)

    # keys ---------------------------------------------------------------------

    def _variant(self) -> str:
        return f"{self.cfg.size}|{self.cfg.fit}|{self.cfg.quality}"

    def _url_key(self, url: str) -> str:
        return "c" + hashlib.sha1(f"{url}|{self._variant()}".encode("utf-8")).hexdigest()[:20]

    # public -------------------------------------------------------------------

    def idle(self) -> Art:
        key = f"idle{self.cfg.size}"
        if not self.cache.has(key):
            self.cache.write(key, render_idle_cover(self.cfg.size, self.cfg.quality))
        return Art(key=key, source="idle")

    def slides(self) -> list[Art]:
        """The pictures shown in turn while nothing is playing, converted like covers.

        They come from the folder given as ``idle.images_dir``, in name order;
        [] when there is none. The folder is looked at again once a minute, so
        pictures can be added or removed without a restart.
        """
        if self._slides_dir is None:
            return []
        now = self._clock()
        with self._lock:
            fresh = (self._slides_scanned is not None
                     and now - self._slides_scanned < SLIDES_RESCAN)
            known = list(self._slides)
        if fresh and all(self.cache.has(art.key) for art in known):
            return known
        slides = self._scan_slides()
        with self._lock:
            self._slides = slides
            self._slides_scanned = now
        return list(slides)

    def _scan_slides(self) -> list[Art]:
        assert self._slides_dir is not None
        try:
            files = sorted((path for path in self._slides_dir.iterdir()
                            if path.is_file() and path.suffix.lower() in SLIDE_SUFFIXES),
                           key=lambda path: path.name.lower())[:MAX_SLIDES]
        except OSError as exc:
            log.warning("idle.images_dir %s cannot be read: %s", self._slides_dir, exc)
            return []
        slides: list[Art] = []
        seen: dict[str, tuple[float, int, str]] = {}
        for path in files:
            try:
                stat = path.stat()
                known = self._slide_files.get(path.name)
                if (known and known[0] == stat.st_mtime and known[1] == stat.st_size
                        and self.cache.has(known[2])):
                    key = known[2]
                else:
                    raw = path.read_bytes()
                    digest = hashlib.sha1(raw + self._variant().encode("utf-8")).hexdigest()
                    key = "s" + digest[:20]
                    if not self.cache.has(key):
                        rendered = render_cover(raw, self.cfg.size, self.cfg.fit, self.cfg.quality)
                        self.cache.write(key, rendered)
                        log.info("idle picture %s: %d bytes in, %d bytes out",
                                 path.name, len(raw), len(rendered))
            except (OSError, ValueError) as exc:
                log.warning("idle picture %s skipped: %s", path.name, exc)
                continue
            seen[path.name] = (stat.st_mtime, stat.st_size, key)
            slides.append(Art(key=key, source="slide", origin=path.name))
        self._slide_files = seen
        return slides

    def placeholder(self, track: Match) -> Art:
        digest = hashlib.sha1(f"{track.identity()}|{self._variant()}".encode("utf-8")).hexdigest()
        key = "p" + digest[:20]
        if not self.cache.has(key):
            self.cache.write(key, render_text_cover(track.title, track.artist, self.cfg.size,
                                                    self.cfg.quality, self.cfg.font_path))
        return Art(key=key, source="placeholder")

    def cached(self, track: Match) -> Art | None:
        """The cover already known for this track, without any network access."""
        with self._lock:
            entry = self._by_track.get(track.identity())
        if not entry:
            return None
        art, when = entry
        if not self.cache.has(art.key):
            return None
        if art.source == "placeholder" and self._clock() - when > PLACEHOLDER_RETRY:
            return None
        return art

    def resolve(self, track: Match) -> Art:
        """Find, convert and cache the cover for *track*. Never raises."""
        known = self.cached(track)
        if known:
            return known
        art: Art | None = None
        try:
            for url, label in self._candidates(track):
                art = self._from_url(url, label)
                if art:
                    break
        except Exception:
            log.exception("cover art lookup failed for %s - %s", track.artist, track.title)
        if art is None:
            try:
                art = self.placeholder(track)
            except Exception:
                log.exception("could not render a placeholder cover")
                art = self.idle()
        with self._lock:
            self._by_track[track.identity()] = (art, self._clock())
            if len(self._by_track) > 512:
                oldest = min(self._by_track, key=lambda k: self._by_track[k][1])
                del self._by_track[oldest]
        return art

    # internals ----------------------------------------------------------------

    def _candidates(self, track: Match) -> Iterator[tuple[str, str]]:
        seen: set[str] = set()
        for name in self.cfg.sources:
            finder = self._finders.get(name)
            if finder is None:
                continue
            try:
                for url, label in finder(track):
                    if url and url not in seen:
                        seen.add(url)
                        yield url, label
            except Exception as exc:
                log.warning("art source %s failed: %s", name, exc)

    def _from_url(self, url: str, label: str) -> Art | None:
        key = self._url_key(url)
        if self.cache.has(key):
            return Art(key=key, source=label, origin=url)
        now = self._clock()
        with self._lock:
            dead_until = self._dead.get(url, 0.0)
        if dead_until > now:
            return None
        try:
            raw = self._fetch(url)
            rendered = render_cover(raw, self.cfg.size, self.cfg.fit, self.cfg.quality)
        except Exception as exc:
            log.info("no art from %s (%s): %s", label, _host(url), exc)
            with self._lock:
                self._dead[url] = now + NEGATIVE_TTL
                if len(self._dead) > 2048:
                    self._dead = {u: t for u, t in self._dead.items() if t > now}
            return None
        self.cache.write(key, rendered)
        log.info("cover from %s: %d bytes in, %d bytes out", label, len(raw), len(rendered))
        return Art(key=key, source=label, origin=url)


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]
