"""The example config must stay in step with the code: valid, complete, same defaults."""

import tomllib
from dataclasses import fields, is_dataclass
from pathlib import Path

from nowplaying.config import Config, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def flatten(value, prefix=""):
    """{"section.option": value} for a dataclass tree or a parsed TOML table."""
    out = {}
    if is_dataclass(value):
        items = [(f.name, getattr(value, f.name)) for f in fields(value) if f.name != "path"]
    else:
        items = list(value.items())
    for name, item in items:
        dotted = f"{prefix}{name}"
        if is_dataclass(item) or isinstance(item, dict):
            out.update(flatten(item, dotted + "."))
        else:
            out[dotted] = item
    return out


def test_example_config_loads_and_validates():
    cfg = load_config(EXAMPLE, env={})
    assert cfg.recognizers.order == ["shazam"]


def test_preview_config_loads_and_needs_no_broker():
    cfg = load_config(EXAMPLE.with_name("config.preview.toml"), env={})
    assert cfg.mqtt.enabled is False
    assert cfg.capture.format == "" and cfg.capture.input.startswith("http://")
    assert cfg.http.host == "127.0.0.1"


def test_example_config_documents_every_option_with_its_real_default():
    with EXAMPLE.open("rb") as handle:
        example = flatten(tomllib.load(handle))
    defaults = flatten(Config())
    assert sorted(set(defaults) - set(example)) == [], "options missing from the example"
    assert sorted(set(example) - set(defaults)) == [], "example has options the code lacks"
    different = {key: (example[key], defaults[key]) for key in defaults
                 if example[key] != defaults[key]}
    assert different == {}, "example values differ from the defaults (example, default)"
