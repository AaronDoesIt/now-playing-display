"""Builds the daemon's parts from a Config and wires them together."""

from __future__ import annotations

from .artwork import ArtCache, ArtworkService
from .capture import AudioCapture
from .config import Config
from .httpd import ArtServer
from .metadata import TrackLengths, build_finders
from .musicdetect import MusicDetector
from .net import HttpClient
from .publisher import MqttPublisher, NullPublisher
from .recognizers import Recognizer, RecognizerChain
from .service import NowPlayingService


def build_recognizer(cfg: Config, name: str) -> Recognizer:
    if name == "shazam":
        from .recognizers.shazam import ShazamRecognizer

        return ShazamRecognizer(cfg.recognizers.shazam)
    if name == "acoustid":
        from .recognizers.acoustid import AcoustIDRecognizer

        return AcoustIDRecognizer(cfg.recognizers.acoustid, HttpClient(cfg.user_agent))
    if name == "audd":
        from .recognizers.audd import AudDRecognizer

        return AudDRecognizer(cfg.recognizers.audd, HttpClient(cfg.user_agent))
    raise ValueError(f"unknown recognizer {name!r}")


def build_artwork(cfg: Config) -> ArtworkService:
    cache = ArtCache(cfg.data_dir / "art", cfg.artwork.cache_max_files)
    images = HttpClient(cfg.user_agent)
    lookups = HttpClient(cfg.user_agent)
    return ArtworkService(cfg.artwork, cache, images.get_bytes, build_finders(cfg, lookups),
                          slides_dir=cfg.idle_images_dir)


def build_detector(cfg: Config, capture: AudioCapture | None) -> MusicDetector | None:
    """The local song-vs-talk detector, or None when it is switched off."""
    if not cfg.music_detector.enabled:
        return None
    return MusicDetector(cfg.music_detector, capture, silence_dbfs=cfg.detection.silence_dbfs,
                         model_path=cfg.music_model_path)


def build_service(cfg: Config, with_capture: bool = True) -> NowPlayingService:
    capture = None
    if with_capture:
        capture = AudioCapture(cfg.capture, silence_dbfs=cfg.detection.silence_dbfs,
                               gap_seconds=cfg.detection.gap_seconds)
    detector = build_detector(cfg, capture) if capture is not None else None
    chain = RecognizerChain([build_recognizer(cfg, name) for name in cfg.recognizers.order])
    artwork = build_artwork(cfg)
    lengths = TrackLengths(HttpClient(cfg.user_agent)) if cfg.detection.use_track_length else None
    service = NowPlayingService(cfg, capture, chain, artwork, detector=detector, lengths=lengths)
    server = ArtServer(cfg.http, artwork.cache, service.state_snapshot, service.health,
                       service.current_art_key)
    if cfg.mqtt.enabled:
        publisher: MqttPublisher | NullPublisher = MqttPublisher(
            cfg, service.handle_command, configuration_url=lambda: server.base_url + "/")
    else:
        publisher = NullPublisher(cfg)
    service.attach(server, publisher)
    return service
