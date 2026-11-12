import json
from types import SimpleNamespace

import pytest

from nowplaying.config import Config, load_config
from nowplaying.publisher import MqttPublisher, Topics, discovery_messages

OK = SimpleNamespace(is_failure=False)
REFUSED = SimpleNamespace(is_failure=True)


class FakeClient:
    def __init__(self, client_id="x"):
        self.client_id = client_id
        self.published = []          # (topic, payload, qos, retain)
        self.subscribed = []
        self.will = None
        self.credentials = None
        self.connect_args = None
        self.tls = False
        self.loop_running = False
        self.disconnected = False
        self.on_connect = self.on_disconnect = self.on_message = None

    def username_pw_set(self, username, password=None):
        self.credentials = (username, password)

    def tls_set(self):
        self.tls = True

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port)

    def loop_start(self):
        self.loop_running = True

    def loop_stop(self):
        self.loop_running = False

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(wait_for_publish=lambda timeout=None: None)

    # test helpers
    def topics(self):
        return [p[0] for p in self.published]

    def last(self, topic):
        return [p for p in self.published if p[0] == topic][-1]


def make(**mqtt):
    cfg = load_config(None, env={})
    cfg.general.name = "Living Room"
    cfg.general.instance = "living_room"
    for key, value in mqtt.items():
        setattr(cfg.mqtt, key, value)
    commands = []
    box = {}

    def factory(client_id):
        box["client"] = FakeClient(client_id)
        return box["client"]

    pub = MqttPublisher(cfg, lambda name, payload: commands.append((name, payload)),
                        configuration_url=lambda: "http://10.0.0.151:8777/",
                        client_factory=factory)
    return pub, box["client"], commands, cfg


def connect(pub, client):
    client.on_connect(client, None, None, OK, None)


def test_topics():
    t = Topics("nowplaying/", "living_room")
    assert t.state == "nowplaying/living_room/state"
    assert t.listening_set == "nowplaying/living_room/listening/set"


def test_start_sets_last_will_credentials_and_connects():
    pub, client, _, _ = make(host="10.0.0.151", port=1884, username="mq", password="pw")
    pub.start()
    assert client.client_id == "nowplaying-living_room"
    assert client.will == ("nowplaying/living_room/availability", "offline", 1, True)
    assert client.credentials == ("mq", "pw")
    assert client.connect_args == ("10.0.0.151", 1884)
    assert client.loop_running and not pub.connected


def test_connect_announces_availability_discovery_and_subscribes():
    pub, client, _, _ = make()
    pub.start()
    connect(pub, client)
    assert pub.connected
    assert client.last("nowplaying/living_room/availability")[1:] == ("online", 1, True)
    configs = [p for p in client.published if p[0].startswith("homeassistant/")]
    assert len(configs) == 11
    assert all(retain and qos == 1 for _, _, qos, retain in configs)
    assert set(client.subscribed) == {
        "nowplaying/living_room/listening/set", "nowplaying/living_room/identify",
        "nowplaying/living_room/external/set", "homeassistant/status"}


def test_discovery_payloads_are_valid_for_home_assistant():
    cfg = load_config(None, env={})
    cfg.general.name, cfg.general.instance = "Living Room", "living_room"
    messages = discovery_messages(cfg, Topics("nowplaying", "living_room"), "http://h:8777/")
    by_topic = dict(messages)
    components = sorted(topic.split("/")[1] for topic in by_topic)
    # Only platforms the MQTT integration really has: there is no MQTT media_player.
    assert components == ["binary_sensor", "binary_sensor", "button", "image", "sensor",
                          "sensor", "sensor", "sensor", "sensor", "sensor", "switch"]
    unique = [p["unique_id"] for p in by_topic.values()]
    assert len(set(unique)) == len(unique)
    for topic, payload in messages:
        json.dumps(payload)
        assert topic.startswith("homeassistant/") and topic.endswith("/config")
        assert payload["device"]["identifiers"] == ["nowplaying_living_room"]
        assert payload["device"]["name"] == "Living Room"
        assert payload["availability_topic"] == "nowplaying/living_room/availability"
        assert "object_id" not in payload           # removed from HA in 2026.4
    image = by_topic["homeassistant/image/nowplaying_living_room/cover/config"]
    assert image["url_topic"] == "nowplaying/living_room/state"
    assert image["url_template"] == "{{ value_json.art_url }}"
    assert "content_type" not in image and "image_topic" not in image
    title = by_topic["homeassistant/sensor/nowplaying_living_room/title/config"]
    assert title["json_attributes_topic"] == "nowplaying/living_room/state"
    assert "[:255]" in title["value_template"] and "None" in title["value_template"]
    switch = by_topic["homeassistant/switch/nowplaying_living_room/listening/config"]
    assert switch["command_topic"] == "nowplaying/living_room/listening/set"
    assert switch["state_topic"] == "nowplaying/living_room/listening"
    button = by_topic["homeassistant/button/nowplaying_living_room/identify/config"]
    assert button["command_topic"] == "nowplaying/living_room/identify"
    level = by_topic["homeassistant/sensor/nowplaying_living_room/level/config"]
    assert level["state_topic"] == "nowplaying/living_room/audio"
    assert level["value_template"] == "{{ value_json.level_dbfs }}"
    music = by_topic["homeassistant/binary_sensor/nowplaying_living_room/music/config"]
    assert music["state_topic"] == "nowplaying/living_room/audio"
    assert "is_music" in music["value_template"]
    score = by_topic["homeassistant/sensor/nowplaying_living_room/music_score/config"]
    assert score["unit_of_measurement"] == "%"


def test_detector_entities_are_removed_when_the_detector_is_off():
    pub, client, _, cfg = make()
    cfg.music_detector.enabled = False
    messages = discovery_messages(cfg, pub.topics)
    removed = [topic for topic, payload in messages if payload is None]
    assert removed == ["homeassistant/binary_sensor/nowplaying_living_room/music/config",
                       "homeassistant/sensor/nowplaying_living_room/music_score/config"]
    assert sum(payload is not None for _, payload in messages) == 9

    pub.start()
    connect(pub, client)
    # an empty retained message is how MQTT discovery deletes an entity
    assert client.last(removed[0])[1:] == ("", 1, True)
    assert client.last(removed[1])[1:] == ("", 1, True)


def test_state_published_before_connecting_is_sent_on_connect():
    pub, client, _, _ = make()
    pub.start()
    pub.publish_state({"state": "playing", "title": "Tïtle"})
    pub.publish_listening(True)
    assert client.published == []
    connect(pub, client)
    topic, payload, qos, retain = client.last("nowplaying/living_room/state")
    assert json.loads(payload) == {"state": "playing", "title": "Tïtle"}
    assert "Tïtle" in payload and (qos, retain) == (1, True)       # UTF-8, not \\u escapes
    assert client.last("nowplaying/living_room/listening")[1] == "ON"
    # state must come after discovery so Home Assistant has the entities first
    order = client.topics()
    assert order.index("nowplaying/living_room/state") > max(
        i for i, t in enumerate(order) if t.startswith("homeassistant/"))


def test_publishing_while_connected():
    pub, client, _, _ = make()
    pub.start()
    connect(pub, client)
    client.published.clear()
    pub.publish_state({"state": "idle"})
    pub.publish_audio({"level_dbfs": -37.3, "music": 0.91, "speech": 0.02, "is_music": True})
    pub.publish_listening(False)
    assert client.published == [
        ("nowplaying/living_room/state", '{"state":"idle"}', 1, True),
        ("nowplaying/living_room/audio",
         '{"level_dbfs":-37.3,"music":0.91,"speech":0.02,"is_music":true}', 0, False),
        ("nowplaying/living_room/listening", "OFF", 1, True),
    ]


def message(topic, payload):
    return SimpleNamespace(topic=topic, payload=payload)


def test_commands_are_routed():
    pub, client, commands, _ = make()
    pub.start()
    connect(pub, client)
    client.on_message(client, None, message("nowplaying/living_room/listening/set", b"OFF"))
    client.on_message(client, None, message("nowplaying/living_room/identify", b"PRESS"))
    client.on_message(client, None, message("nowplaying/living_room/external/set", b'{"title":"x"}'))
    client.on_message(client, None, message("some/other/topic", b"ignored"))
    assert commands == [("listening", b"OFF"), ("identify", b"PRESS"),
                        ("external", b'{"title":"x"}')]


def test_home_assistant_birth_message_triggers_a_re_announce():
    pub, client, _, _ = make()
    pub.start()
    connect(pub, client)
    pub.publish_state({"state": "idle"})
    client.published.clear()
    client.on_message(client, None, message("homeassistant/status", b"offline"))
    assert client.published == []
    client.on_message(client, None, message("homeassistant/status", b"online"))
    assert sum(t.startswith("homeassistant/") for t in client.topics()) == 11
    assert "nowplaying/living_room/state" in client.topics()


def test_refused_connection_publishes_nothing():
    pub, client, _, _ = make()
    pub.start()
    client.on_connect(client, None, None, REFUSED, None)
    assert not pub.connected and client.published == []


def test_disconnect_and_reconnect():
    pub, client, _, _ = make()
    pub.start()
    connect(pub, client)
    client.on_disconnect(client, None, None, "connection lost", None)
    assert not pub.connected
    pub.publish_state({"state": "playing", "title": "During the outage"})
    client.published.clear()
    connect(pub, client)
    assert "During the outage" in client.last("nowplaying/living_room/state")[1]


def test_stop_says_offline_and_disconnects(caplog):
    pub, client, _, _ = make()
    pub.start()
    connect(pub, client)
    with caplog.at_level("WARNING"):
        pub.stop()
        client.on_disconnect(client, None, None, "Normal disconnection", None)
    assert client.last("nowplaying/living_room/availability")[1:] == ("offline", 1, True)
    assert client.disconnected and not client.loop_running and not pub.connected
    assert caplog.records == []                     # a deliberate stop is not a warning


def test_options_discovery_off_external_off_tls():
    pub, client, _, cfg = make(discovery=False, tls=True)
    cfg.external.enabled = False
    pub.start()
    connect(pub, client)
    assert client.tls is True
    assert not any(t.startswith("homeassistant/") for t in client.topics())
    assert "nowplaying/living_room/external/set" not in client.subscribed


def test_real_paho_client_can_be_constructed():
    """The default factory must work with the installed paho-mqtt (v2 callback API)."""
    cfg = load_config(None, env={})
    pub = MqttPublisher(cfg, lambda *a: None)
    assert hasattr(pub._client, "connect_async") and hasattr(pub._client, "will_set")
