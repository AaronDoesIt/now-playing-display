"""Configuration: a TOML file, typed defaults, and environment overrides for secrets.

Unknown options are an error, so a typo is reported instead of being ignored.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, get_type_hints

from . import __version__

KNOWN_RECOGNIZERS = ("shazam", "acoustid", "audd")
ART_SOURCES = ("recognizer", "coverartarchive", "itunes")


class ConfigError(ValueError):
    """The configuration file is missing, malformed or inconsistent."""


@dataclass
class GeneralConfig:
    name: str = "Now Playing"
    instance: str = "now_playing"
    log_level: str = "INFO"
    data_dir: str = "data"


@dataclass
class CaptureConfig:
    input: str = "default"
    format: str = "alsa"
    input_args: list[str] = field(default_factory=list)
    sample_rate: int = 16000
    buffer_seconds: int = 30
    ffmpeg_path: str = "ffmpeg"
    stall_seconds: float = 15.0


@dataclass
class DetectionConfig:
    window_seconds: float = 8.0
    min_window_seconds: float = 6.0
    max_window_seconds: float = 12.0
    search_interval: float = 15.0
    search_max_interval: float = 120.0
    locked_interval: float = 30.0
    silence_dbfs: float = -50.0
    gap_seconds: float = 1.0
    idle_after: float = 20.0
    hold_seconds: float = 60.0
    confirmations: int = 1
    freeze_seconds: float = 0.0
    max_freeze_seconds: float = 600.0
    use_track_length: bool = False


@dataclass
class MusicDetectorConfig:
    enabled: bool = True
    model_path: str = ""
    threshold: float = 0.35
    release: float = 0.15
    smooth_seconds: float = 5.0
    hop_seconds: float = 1.0
    threads: int = 1


@dataclass
class AcoustIDConfig:
    api_key: str = ""
    min_score: float = 0.6
    fpcalc_path: str = "fpcalc"
    min_interval: float = 1.0
    daily_limit: int = 0
    timeout: float = 10.0


@dataclass
class ShazamConfig:
    min_interval: float = 10.0
    max_per_minute: int = 3
    daily_limit: int = 0
    timeout: float = 20.0
    language: str = "en-US"
    endpoint_country: str = "US"
    confirm_skew: float = 0.001


@dataclass
class AudDConfig:
    api_token: str = ""
    min_interval: float = 5.0
    daily_limit: int = 300
    timeout: float = 20.0


@dataclass
class RecognizersConfig:
    order: list[str] = field(default_factory=lambda: ["shazam"])
    acoustid: AcoustIDConfig = field(default_factory=AcoustIDConfig)
    shazam: ShazamConfig = field(default_factory=ShazamConfig)
    audd: AudDConfig = field(default_factory=AudDConfig)


@dataclass
class MetadataConfig:
    contact: str = ""
    musicbrainz: bool = True


@dataclass
class ArtworkConfig:
    size: int = 480
    fit: str = "cover"
    quality: int = 85
    sources: list[str] = field(default_factory=lambda: list(ART_SOURCES))
    wait_seconds: float = 2.0
    cache_max_files: int = 500
    font_path: str = ""


@dataclass
class HttpConfig:
    host: str = "0.0.0.0"
    port: int = 8777
    public_base_url: str = ""


@dataclass
class MqttConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    client_id: str = ""
    base_topic: str = "nowplaying"
    discovery: bool = True
    discovery_prefix: str = "homeassistant"
    level_interval: float = 15.0


@dataclass
class ExternalConfig:
    enabled: bool = True
    ha_base_url: str = ""


@dataclass
class IdleConfig:
    images_dir: str = ""
    slide_seconds: float = 10.0


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    music_detector: MusicDetectorConfig = field(default_factory=MusicDetectorConfig)
    recognizers: RecognizersConfig = field(default_factory=RecognizersConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    artwork: ArtworkConfig = field(default_factory=ArtworkConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    external: ExternalConfig = field(default_factory=ExternalConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    path: Path | None = None

    @property
    def data_dir(self) -> Path:
        """Where the art cache and the saved switch state live."""
        configured = Path(self.general.data_dir).expanduser()
        if configured.is_absolute():
            return configured
        base = self.path.parent if self.path else Path.cwd()
        return (base / configured).resolve()

    @property
    def music_model_path(self) -> Path | None:
        """The detector model: None means the copy shipped in server/models."""
        configured = self.music_detector.model_path.strip()
        if not configured:
            return None
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
        base = self.path.parent if self.path else Path.cwd()
        return (base / path).resolve()

    @property
    def idle_images_dir(self) -> Path | None:
        """The folder of pictures shown while nothing is playing, or None when there is none."""
        configured = self.idle.images_dir.strip()
        if not configured:
            return None
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
        base = self.path.parent if self.path else Path.cwd()
        return (base / path).resolve()

    @property
    def user_agent(self) -> str:
        contact = self.metadata.contact.strip()
        suffix = f" ( {contact} )" if contact else ""
        return f"NowPlayingDisplay/{__version__}{suffix}"


# Secrets may come from the environment so they can stay out of the config file
# (the systemd unit reads them from an EnvironmentFile).
ENV_OVERRIDES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("NOWPLAYING_MQTT_USERNAME", ("mqtt", "username")),
    ("NOWPLAYING_MQTT_PASSWORD", ("mqtt", "password")),
    ("NOWPLAYING_ACOUSTID_API_KEY", ("recognizers", "acoustid", "api_key")),
    ("NOWPLAYING_AUDD_API_TOKEN", ("recognizers", "audd", "api_token")),
)


def load_config(path: str | Path | None, env: Mapping[str, str] | None = None) -> Config:
    """Read *path* (or use pure defaults when it is None), apply env overrides, validate."""
    env = os.environ if env is None else env
    data: dict[str, Any] = {}
    resolved: Path | None = None
    if path is not None:
        resolved = Path(path).expanduser().resolve()
        try:
            with resolved.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {resolved}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{resolved}: {exc}") from None

    cfg = _build(Config, data, "")
    cfg.path = resolved
    for variable, target in ENV_OVERRIDES:
        value = env.get(variable)
        if value:
            section: Any = cfg
            for name in target[:-1]:
                section = getattr(section, name)
            setattr(section, target[-1], value)
    validate(cfg)
    return cfg


def _build(cls: type, data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"[{where}] must be a table")
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)} - {"path"}
    unknown = sorted(set(data) - known)
    if unknown:
        label = f"[{where}]" if where else "top level"
        raise ConfigError(f"{label}: unknown option(s) {', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        typ = hints[name]
        dotted = f"{where}.{name}" if where else name
        if is_dataclass(typ):
            kwargs[name] = _build(typ, value, dotted)
        else:
            kwargs[name] = _coerce(value, typ, dotted)
    return cls(**kwargs)


def _coerce(value: Any, typ: Any, where: str) -> Any:
    if typ is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{where} must be true or false")
    if typ is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ConfigError(f"{where} must be a whole number")
    if typ is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ConfigError(f"{where} must be a number")
    if typ is str:
        if isinstance(value, str):
            return value
        raise ConfigError(f"{where} must be a string")
    if typ == list[str]:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        raise ConfigError(f"{where} must be a list of strings")
    raise ConfigError(f"{where}: unsupported option type {typ!r}")


_SLUG = re.compile(r"^[a-z0-9_]+$")
_TOPIC = re.compile(r"^[^#+\s]+$")


def validate(cfg: Config) -> None:
    """Raise ConfigError listing every problem found."""
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    check(bool(_SLUG.match(cfg.general.instance)),
          "general.instance must contain only a-z, 0-9 and _")
    check(cfg.general.log_level.upper() in ("DEBUG", "INFO", "WARNING", "ERROR"),
          "general.log_level must be DEBUG, INFO, WARNING or ERROR")

    cap = cfg.capture
    check(bool(cap.input.strip()), "capture.input must not be empty")
    check(8000 <= cap.sample_rate <= 48000, "capture.sample_rate must be 8000-48000")
    check(cap.stall_seconds == 0 or cap.stall_seconds >= 5,
          "capture.stall_seconds must be 0 (never restart) or at least 5")

    det = cfg.detection
    check(1.0 <= det.min_window_seconds <= det.window_seconds <= det.max_window_seconds,
          "detection: need 1 <= min_window_seconds <= window_seconds <= max_window_seconds")
    check(cap.buffer_seconds >= det.max_window_seconds + 2,
          "capture.buffer_seconds must be at least detection.max_window_seconds + 2")
    check(det.search_interval >= 1.0, "detection.search_interval must be >= 1")
    check(det.search_max_interval >= det.search_interval,
          "detection.search_max_interval must be >= search_interval")
    check(det.locked_interval >= 1.0, "detection.locked_interval must be >= 1")
    check(-100.0 <= det.silence_dbfs <= 0.0, "detection.silence_dbfs must be -100..0")
    check(det.gap_seconds >= 0.3, "detection.gap_seconds must be >= 0.3")
    check(det.confirmations >= 1, "detection.confirmations must be >= 1")
    check(det.idle_after > 0 and det.hold_seconds > 0,
          "detection.idle_after and hold_seconds must be > 0")
    check(det.freeze_seconds >= 0, "detection.freeze_seconds must be 0 (off) or more")
    check(det.max_freeze_seconds >= det.locked_interval,
          "detection.max_freeze_seconds must be >= locked_interval")

    md = cfg.music_detector
    if md.enabled:
        check(cap.sample_rate == 16000,
              "music_detector needs capture.sample_rate = 16000 (the model's input rate)")
    check(0.0 < md.release < md.threshold < 1.0,
          "music_detector: need 0 < release < threshold < 1")
    check(0.2 <= md.hop_seconds <= 5.0, "music_detector.hop_seconds must be 0.2-5")
    check(md.smooth_seconds >= md.hop_seconds,
          "music_detector.smooth_seconds must be >= hop_seconds")
    check(1 <= md.threads <= 8, "music_detector.threads must be 1-8")

    rec = cfg.recognizers
    check(bool(rec.order), "recognizers.order must list at least one recognizer")
    for name in rec.order:
        check(name in KNOWN_RECOGNIZERS,
              f"recognizers.order: unknown recognizer {name!r} "
              f"(known: {', '.join(KNOWN_RECOGNIZERS)})")
    check(len(set(rec.order)) == len(rec.order), "recognizers.order has duplicates")
    if "acoustid" in rec.order:
        check(bool(rec.acoustid.api_key),
              "recognizers.acoustid.api_key (or NOWPLAYING_ACOUSTID_API_KEY) is required "
              "when acoustid is in recognizers.order")
    if "audd" in rec.order:
        check(bool(rec.audd.api_token),
              "recognizers.audd.api_token (or NOWPLAYING_AUDD_API_TOKEN) is required "
              "when audd is in recognizers.order")
    check(0.0 <= rec.acoustid.min_score <= 1.0, "recognizers.acoustid.min_score must be 0..1")
    check(0.0 <= rec.shazam.confirm_skew < 1.0, "recognizers.shazam.confirm_skew must be 0 "
          "(off) or a small fraction such as 0.001")
    if "shazam" in rec.order:
        # Shazam's servers answer "no match" to any clip of 15 s or more.
        check(det.max_window_seconds <= 12.0,
              "detection.max_window_seconds must be <= 12 when shazam is used")

    art = cfg.artwork
    check(64 <= art.size <= 1024, "artwork.size must be 64-1024")
    check(art.fit in ("cover", "contain"), 'artwork.fit must be "cover" or "contain"')
    check(40 <= art.quality <= 95, "artwork.quality must be 40-95")
    for name in art.sources:
        check(name in ART_SOURCES,
              f"artwork.sources: unknown source {name!r} (known: {', '.join(ART_SOURCES)})")
    check(art.cache_max_files >= 10, "artwork.cache_max_files must be >= 10")

    check(cfg.idle.slide_seconds >= 3.0,
          "idle.slide_seconds must be at least 3 (the panel needs a second or two per picture)")

    check(1 <= cfg.http.port <= 65535, "http.port must be 1-65535")
    if cfg.http.public_base_url:
        check(bool(re.match(r"^https?://[^/\s]+", cfg.http.public_base_url)),
              "http.public_base_url must look like http://10.0.0.5:8777")

    mq = cfg.mqtt
    check(1 <= mq.port <= 65535, "mqtt.port must be 1-65535")
    check(bool(_TOPIC.match(mq.base_topic.strip("/"))) and bool(mq.base_topic.strip("/")),
          "mqtt.base_topic must be a plain topic prefix without wildcards or spaces")
    check(bool(_TOPIC.match(mq.discovery_prefix)), "mqtt.discovery_prefix is not a valid topic")

    if problems:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
