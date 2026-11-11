import json
import os
import stat
import sys

import pytest

from screenlight.config import Bulb, Settings, load, save


def test_round_trip(tmp_path):
    path = tmp_path / "config.json"
    settings = Settings(
        monitor=2,
        bulbs=(Bulb(name="Desk", id="abc", ip="192.168.1.5", key="k3y", version=3.4, enabled=False),),
        mode="plain",
        smoothing=0.7,
        on_stop="off",
    )
    save(settings, path)
    assert load(path) == settings


def test_missing_file_gives_defaults(tmp_path):
    assert load(tmp_path / "nope.json") == Settings()


def test_corrupt_file_gives_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    assert load(path) == Settings()


def test_missing_and_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"fps": 30, "future_option": True, "bulbs": [{"id": "x1"}]}))
    settings = load(path)
    assert settings.fps == 30
    assert settings.mode == Settings().mode
    assert settings.bulbs == (Bulb(name="x1", id="x1"),)


def test_bad_values_fall_back_per_field(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"fps": "fast", "smoothing": 0.2, "bulbs": [{"name": "no id"}, 5]}))
    settings = load(path)
    assert settings.fps == Settings().fps
    assert settings.smoothing == 0.2
    assert settings.bulbs == ()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_saved_file_is_private(tmp_path):
    path = tmp_path / "sub" / "config.json"
    save(Settings(), path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_old_config_loads_bulbs_as_wifi(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bulbs": [{"id": "x1", "name": "Desk", "ip": "10.0.0.2", "key": "k"}]}))
    bulb = load(path).bulbs[0]
    assert bulb.kind == "wifi" and not bulb.is_beacon and bulb.node_id == 0 and bulb.beacon_key == ""


def test_beacon_bulb_round_trip(tmp_path):
    path = tmp_path / "config.json"
    b = Bulb(name="BT", id="x", ip="Bluetooth", key="0123456789abcdef", kind="beacon", node_id=2,
             beacon_key="00" * 16)
    save(Settings(bulbs=(b,)), path)
    assert load(path).bulbs == (b,)
    assert load(path).bulbs[0].is_beacon


def test_beacon_state_path_sits_next_to_config():
    from screenlight.config import beacon_state_path, config_path
    assert beacon_state_path() == config_path().parent / "beacon_state.json"


def test_manual_colour_fields_default_and_round_trip(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bulbs": [{"id": "x1"}]}))
    old = load(path)
    assert old.source == "screen" and old.bulbs[0].colour == "#ffffff"
    s = Settings(source="manual", bulbs=(Bulb(name="A", id="a", colour="#ff8800"),))
    save(s, path)
    assert load(path) == s
