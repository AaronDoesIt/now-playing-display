import pytest

from nowplaying.config import ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_are_valid():
    cfg = load_config(None, env={})
    assert cfg.recognizers.order == ["shazam"]
    assert cfg.artwork.size == 480
    assert cfg.mqtt.base_topic == "nowplaying"
    assert cfg.user_agent.startswith("NowPlayingDisplay/")


def test_loads_sections_and_resolves_data_dir(tmp_path):
    path = write(tmp_path, """
[general]
name = "Living Room"
instance = "living_room"
data_dir = "cache"

[capture]
format = ""
input = "rtsp://cam/stream"
input_args = ["-rtsp_transport", "tcp"]

[detection]
window_seconds = 6
silence_dbfs = -45

[metadata]
contact = "https://example.org/me"

[mqtt]
host = "10.0.0.5"
""")
    cfg = load_config(path, env={})
    assert cfg.general.name == "Living Room"
    assert cfg.capture.input_args == ["-rtsp_transport", "tcp"]
    assert cfg.detection.window_seconds == 6.0          # int accepted for a float option
    assert cfg.detection.silence_dbfs == -45.0
    assert cfg.data_dir == (tmp_path / "cache").resolve()
    assert cfg.user_agent.endswith("( https://example.org/me )")
    assert cfg.mqtt.host == "10.0.0.5"


def test_unknown_option_is_reported(tmp_path):
    path = write(tmp_path, "[capture]\ninptu = 'hw:1'\n")
    with pytest.raises(ConfigError, match="inptu"):
        load_config(path, env={})


def test_unknown_section_is_reported(tmp_path):
    path = write(tmp_path, "[captrue]\ninput = 'hw:1'\n")
    with pytest.raises(ConfigError, match="captrue"):
        load_config(path, env={})


@pytest.mark.parametrize("text,needle", [
    ("[http]\nport = '8777'\n", "http.port"),
    ("[http]\nport = true\n", "http.port"),
    ("[mqtt]\nenabled = 1\n", "mqtt.enabled"),
    ("[capture]\ninput_args = '-re'\n", "capture.input_args"),
    ("[detection]\nwindow_seconds = 'long'\n", "detection.window_seconds"),
])
def test_wrong_types_are_reported(tmp_path, text, needle):
    with pytest.raises(ConfigError, match=needle):
        load_config(write(tmp_path, text), env={})


def test_bad_toml_and_missing_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "[general\nname = 1"), env={})
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})


def test_env_overrides_secrets(tmp_path):
    path = write(tmp_path, "[recognizers]\norder = ['acoustid', 'shazam']\n")
    with pytest.raises(ConfigError, match="api_key"):
        load_config(path, env={})
    cfg = load_config(path, env={
        "NOWPLAYING_ACOUSTID_API_KEY": "abc123",
        "NOWPLAYING_MQTT_PASSWORD": "secret",
    })
    assert cfg.recognizers.acoustid.api_key == "abc123"
    assert cfg.mqtt.password == "secret"


def test_validation_lists_every_problem(tmp_path):
    path = write(tmp_path, """
[general]
instance = "Living Room"

[detection]
window_seconds = 20
max_window_seconds = 10

[recognizers]
order = ["shazam", "magic"]

[artwork]
fit = "stretch"
""")
    with pytest.raises(ConfigError) as info:
        load_config(path, env={})
    message = str(info.value)
    for needle in ("general.instance", "window_seconds", "magic", "artwork.fit"):
        assert needle in message


def test_music_detector_settings(tmp_path):
    cfg = load_config(None, env={})
    assert cfg.music_detector.enabled is True
    assert cfg.music_model_path is None                     # the bundled model
    path = write(tmp_path, "[music_detector]\nmodel_path = 'models/custom.tflite'\n")
    assert load_config(path, env={}).music_model_path == (tmp_path / "models/custom.tflite").resolve()

    bad_rate = write(tmp_path, "[capture]\nsample_rate = 44100\n")
    with pytest.raises(ConfigError, match="16000"):
        load_config(bad_rate, env={})
    off = write(tmp_path, "[capture]\nsample_rate = 44100\n[music_detector]\nenabled = false\n")
    assert load_config(off, env={}).capture.sample_rate == 44100       # fine without the detector

    crossed = write(tmp_path, "[music_detector]\nthreshold = 0.2\nrelease = 0.4\n")
    with pytest.raises(ConfigError, match="release < threshold"):
        load_config(crossed, env={})


def test_shazam_window_limit(tmp_path):
    path = write(tmp_path, "[detection]\nmax_window_seconds = 15\n[capture]\nbuffer_seconds = 30\n")
    with pytest.raises(ConfigError, match="<= 12"):
        load_config(path, env={})


def test_audd_requires_token(tmp_path):
    path = write(tmp_path, "[recognizers]\norder = ['audd']\n")
    with pytest.raises(ConfigError, match="api_token"):
        load_config(path, env={})
    assert load_config(path, env={"NOWPLAYING_AUDD_API_TOKEN": "t"}).recognizers.audd.api_token == "t"


def test_quiet_period_and_idle_picture_settings(tmp_path):
    path = write(tmp_path, "[detection]\nfreeze_seconds = -1\nmax_freeze_seconds = 5\n"
                           "[idle]\nslide_seconds = 1\n")
    with pytest.raises(ConfigError) as err:
        load_config(path, env={})
    for needle in ("freeze_seconds", "max_freeze_seconds", "slide_seconds"):
        assert needle in str(err.value)

    cfg = load_config(write(tmp_path, '[idle]\nimages_dir = "pics"\nslide_seconds = 10\n'), env={})
    assert cfg.idle_images_dir == (tmp_path / "pics").resolve()
    assert load_config(None, env={}).idle_images_dir is None
    defaults = load_config(None, env={}).detection
    assert (defaults.freeze_seconds, defaults.max_freeze_seconds, defaults.use_track_length) \
        == (0.0, 600.0, False)             # off by default: music from a TV is mostly snippets
