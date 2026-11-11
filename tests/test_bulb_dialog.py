from screenlight.config import Bulb
from screenlight.gui.bulb_dialog import edited_bulb

BT = Bulb(name="Lamp", id="x", ip="Bluetooth", key="aaaaaaaaaaaaaaaa", enabled=False, kind="beacon", node_id=2,
          beacon_key="11" * 16)


def values(**kw):
    base = {"name": "Lamp", "id": "x", "ip": "Bluetooth", "key": "aaaaaaaaaaaaaaaa"}
    return {**base, **kw}


def test_renaming_a_bluetooth_bulb_keeps_it_a_bluetooth_bulb():
    edited = edited_bulb(BT, values(name="Desk lamp"), 3.3)
    assert edited == Bulb(name="Desk lamp", id="x", ip="Bluetooth", key="aaaaaaaaaaaaaaaa", enabled=False,
                          kind="beacon", node_id=2, beacon_key="11" * 16)


def test_changing_a_bluetooth_bulbs_local_key_clears_its_stale_beacon_key():
    edited = edited_bulb(BT, values(key="bbbbbbbbbbbbbbbb"), 3.3)
    assert edited.is_beacon and edited.beacon_key == "" and edited.key == "bbbbbbbbbbbbbbbb"


def test_wifi_bulb_edit_is_unchanged_behaviour():
    wifi = Bulb(name="Desk", id="y", ip="10.0.0.2", key="k", version=3.3)
    edited = edited_bulb(wifi, {"name": "", "id": "y", "ip": "", "key": "k2"}, 3.5)
    assert edited == Bulb(name="y", id="y", ip="Auto", key="k2", version=3.5)
