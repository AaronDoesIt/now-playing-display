"""MQTT: track state for the display, discovery for Home Assistant, commands back in.

Topics (with the defaults base_topic = "nowplaying", instance = "now_playing"):

  nowplaying/now_playing/state           retained JSON: state, title, artist, album, art_url, ...
  nowplaying/now_playing/availability    retained "online" / "offline" (last will)
  nowplaying/now_playing/audio           JSON: level_dbfs, music, speech, is_music (what the
                                         always-on listener hears; sent on change and on a timer)
  nowplaying/now_playing/listening       retained "ON" / "OFF"
  nowplaying/now_playing/listening/set   command: "ON" / "OFF"
  nowplaying/now_playing/identify        command: any payload = look up right now
  nowplaying/now_playing/external/set    command: JSON track pushed in from Home Assistant

Home Assistant has no MQTT media_player platform, so discovery creates sensors,
binary sensors, an image entity (the cover), a switch and a button instead.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from . import __version__
from .config import Config

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, bytes], None]


class Topics:
    def __init__(self, base_topic: str, instance: str) -> None:
        self.root = f"{base_topic.strip('/')}/{instance}"
        self.state = f"{self.root}/state"
        self.availability = f"{self.root}/availability"
        self.audio = f"{self.root}/audio"
        self.listening = f"{self.root}/listening"
        self.listening_set = f"{self.root}/listening/set"
        self.identify = f"{self.root}/identify"
        self.external_set = f"{self.root}/external/set"


def _text(field: str) -> str:
    """Template for a text field.

    Empty renders as None, which Home Assistant turns into the "unknown" state.
    The value is cut to 255 characters because a longer state is rejected; the
    full text is still in the title sensor's attributes.
    """
    return "{{ value_json." + field + "[:255] if value_json." + field + " else None }}"


def discovery_messages(cfg: Config, topics: Topics,
                       configuration_url: str = "") -> list[tuple[str, dict[str, Any] | None]]:
    """The Home Assistant MQTT discovery configs as (topic, payload) pairs.

    A payload of None means "remove this entity" (an empty retained message):
    used for the music-detector entities when the detector is switched off, so
    they do not linger in Home Assistant.
    """
    node = f"nowplaying_{cfg.general.instance}"
    prefix = cfg.mqtt.discovery_prefix.strip("/")
    device: dict[str, Any] = {
        "identifiers": [node],
        "name": cfg.general.name,
        "manufacturer": "now-playing-display",
        "model": "Audio recognition daemon",
        "sw_version": __version__,
    }
    if configuration_url:
        device["configuration_url"] = configuration_url
    origin = {"name": "now-playing-display", "sw_version": __version__}

    def entity(component: str, key: str, name: str, **extra: Any) -> tuple[str, dict[str, Any]]:
        payload = {
            "name": name,
            "unique_id": f"{node}_{key}",
            "availability_topic": topics.availability,
            "device": device,
            "origin": origin,
            **extra,
        }
        return f"{prefix}/{component}/{node}/{key}/config", payload

    detector_entities = [
        entity("binary_sensor", "music", "Music detected", state_topic=topics.audio,
               value_template="{{ 'ON' if value_json.is_music else 'OFF' }}",
               icon="mdi:music-circle"),
        entity("sensor", "music_score", "Music score", state_topic=topics.audio,
               value_template="{{ (value_json.music * 100) | round(0) "
                              "if value_json.music is not none else None }}",
               unit_of_measurement="%", state_class="measurement",
               entity_category="diagnostic", icon="mdi:music-note-eighth"),
    ]
    if not cfg.music_detector.enabled:
        detector_entities = [(topic, None) for topic, _ in detector_entities]

    return detector_entities + [
        entity("sensor", "title", "Title", state_topic=topics.state,
               value_template=_text("title"), json_attributes_topic=topics.state,
               icon="mdi:music-note"),
        entity("sensor", "artist", "Artist", state_topic=topics.state,
               value_template=_text("artist"), icon="mdi:account-music"),
        entity("sensor", "album", "Album", state_topic=topics.state,
               value_template=_text("album"), icon="mdi:album"),
        entity("binary_sensor", "playing", "Playing", state_topic=topics.state,
               value_template="{{ 'ON' if value_json.state == 'playing' else 'OFF' }}",
               icon="mdi:play-circle"),
        entity("image", "cover", "Cover", url_topic=topics.state,
               url_template="{{ value_json.art_url }}"),
        entity("sensor", "source", "Identified by", state_topic=topics.state,
               value_template=_text("source"), entity_category="diagnostic",
               icon="mdi:waveform"),
        entity("sensor", "level", "Input level", state_topic=topics.audio,
               value_template="{{ value_json.level_dbfs }}",
               unit_of_measurement="dBFS", state_class="measurement",
               suggested_display_precision=0, entity_category="diagnostic",
               icon="mdi:microphone"),
        entity("switch", "listening", "Listening", state_topic=topics.listening,
               command_topic=topics.listening_set, payload_on="ON", payload_off="OFF",
               entity_category="config", icon="mdi:ear-hearing"),
        entity("button", "identify", "Identify now", command_topic=topics.identify,
               payload_press="PRESS", entity_category="config", icon="mdi:magnify"),
    ]


def _default_client(client_id: str) -> Any:
    import paho.mqtt.client as mqtt

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


class MqttPublisher:
    def __init__(
        self,
        cfg: Config,
        on_command: CommandHandler,
        configuration_url: Callable[[], str] | None = None,
        client_factory: Callable[[str], Any] = _default_client,
    ) -> None:
        self.cfg = cfg
        self.topics = Topics(cfg.mqtt.base_topic, cfg.general.instance)
        self._on_command = on_command
        self._configuration_url = configuration_url or (lambda: "")
        client_id = cfg.mqtt.client_id or f"nowplaying-{cfg.general.instance}"
        self._client = client_factory(client_id)
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._last_state: str | None = None
        self._last_listening: str | None = None
        self._stopping = False
        self._ha_status_topic = f"{cfg.mqtt.discovery_prefix.strip('/')}/status"

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        mq, client = self.cfg.mqtt, self._client
        if mq.username:
            client.username_pw_set(mq.username, mq.password or None)
        if mq.tls:
            client.tls_set()
        client.will_set(self.topics.availability, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_message = self._handle_message
        log.info("connecting to MQTT broker %s:%d", mq.host, mq.port)
        client.connect_async(mq.host, mq.port, keepalive=60)
        client.loop_start()

    def stop(self) -> None:
        client = self._client
        self._stopping = True
        try:
            if self.connected:
                info = client.publish(self.topics.availability, "offline", qos=1, retain=True)
                try:
                    info.wait_for_publish(timeout=2)
                except Exception:
                    pass
            client.disconnect()
        except Exception:
            log.debug("MQTT disconnect failed", exc_info=True)
        finally:
            client.loop_stop()
            self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    # ------------------------------------------------------------- publishing

    def publish_state(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._last_state = text
        if self.connected:
            self._client.publish(self.topics.state, text, qos=1, retain=True)

    def publish_listening(self, listening: bool) -> None:
        text = "ON" if listening else "OFF"
        with self._lock:
            self._last_listening = text
        if self.connected:
            self._client.publish(self.topics.listening, text, qos=1, retain=True)

    def publish_audio(self, report: dict[str, Any]) -> None:
        if self.connected:
            self._client.publish(self.topics.audio, json.dumps(report, separators=(",", ":")),
                                 qos=0, retain=False)

    # -------------------------------------------------------------- callbacks

    def _handle_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any,
                        properties: Any = None) -> None:
        if getattr(reason_code, "is_failure", bool(reason_code)):
            log.error("MQTT broker refused the connection: %s", reason_code)
            return
        log.info("MQTT connected")
        self._connected.set()
        subscriptions = [self.topics.listening_set, self.topics.identify, self._ha_status_topic]
        if self.cfg.external.enabled:
            subscriptions.append(self.topics.external_set)
        for topic in subscriptions:
            client.subscribe(topic, qos=1)
        self._announce()

    def _announce(self) -> None:
        """(Re)publish everything a broker or Home Assistant restart could have lost."""
        client = self._client
        client.publish(self.topics.availability, "online", qos=1, retain=True)
        if self.cfg.mqtt.discovery:
            for topic, payload in discovery_messages(self.cfg, self.topics,
                                                     self._configuration_url()):
                text = "" if payload is None else json.dumps(payload, separators=(",", ":"))
                client.publish(topic, text, qos=1, retain=True)
        with self._lock:
            state, listening = self._last_state, self._last_listening
        if listening is not None:
            client.publish(self.topics.listening, listening, qos=1, retain=True)
        if state is not None:
            client.publish(self.topics.state, state, qos=1, retain=True)

    def _handle_disconnect(self, client: Any, userdata: Any, flags: Any, reason_code: Any,
                           properties: Any = None) -> None:
        if self._connected.is_set() and not self._stopping:
            log.warning("MQTT disconnected (%s); reconnecting automatically", reason_code)
        self._connected.clear()

    def _handle_message(self, client: Any, userdata: Any, message: Any) -> None:
        topic, payload = message.topic, bytes(message.payload or b"")
        try:
            if topic == self._ha_status_topic:
                if payload.strip().lower() == b"online":
                    log.info("Home Assistant came online; re-announcing")
                    self._announce()
            elif topic == self.topics.listening_set:
                self._on_command("listening", payload)
            elif topic == self.topics.identify:
                self._on_command("identify", payload)
            elif topic == self.topics.external_set:
                self._on_command("external", payload)
        except Exception:
            log.exception("error handling MQTT message on %s", topic)


class NullPublisher:
    """Stands in when MQTT is disabled: the daemon still runs and serves HTTP."""

    connected = False

    def __init__(self, cfg: Config) -> None:
        self.topics = Topics(cfg.mqtt.base_topic, cfg.general.instance)

    def start(self) -> None:
        log.info("MQTT disabled; state is available at /state.json only")

    def stop(self) -> None:
        pass

    def wait_connected(self, timeout: float) -> bool:
        return False

    def publish_state(self, payload: dict[str, Any]) -> None:
        log.debug("state: %s", payload)

    def publish_listening(self, listening: bool) -> None:
        pass

    def publish_audio(self, report: dict[str, Any]) -> None:
        pass
