from screenlight.config import Bulb
from screenlight.discovery import merge_devices

CLOUD = [
    {"id": "bulb1", "name": "Desk lamp", "key": "k1", "category": "dj", "product_name": "RGB Bulb"},
    {"id": "plug1", "name": "Kettle", "key": "k2", "category": "cz", "product_name": "Smart Plug"},
    {"id": "bulb2", "name": "Shelf", "key": "k3", "category": "dd", "product_name": "LED Strip"},
    {"id": "zb1", "name": "Zigbee bulb", "key": "k4", "category": "dj", "gateway_id": "hub1"},
]
SCAN = {
    "bulb1": {"gwId": "bulb1", "ip": "192.168.1.40", "version": "3.5"},
    "plug1": {"gwId": "plug1", "ip": "192.168.1.41", "version": "3.3"},
}


def test_merge_fills_ip_and_version_from_scan():
    found = {c.bulb.id: c for c in merge_devices(CLOUD, SCAN)}
    assert found["bulb1"].bulb == Bulb(name="Desk lamp", id="bulb1", ip="192.168.1.40", key="k1", version=3.5)
    assert found["bulb1"].on_lan


def test_bulb_missing_from_scan_uses_auto_ip():
    found = {c.bulb.id: c for c in merge_devices(CLOUD, SCAN)}
    assert found["bulb2"].bulb.ip == "Auto"
    assert found["bulb2"].bulb.version == 3.3
    assert not found["bulb2"].on_lan


def test_lights_flagged_and_listed_first_and_hub_children_skipped():
    candidates = merge_devices(CLOUD, SCAN)
    assert [(c.bulb.id, c.is_light) for c in candidates] == [
        ("bulb1", True),
        ("bulb2", True),
        ("plug1", False),
    ]


def test_scan_keyed_by_ip_still_matches():
    by_ip = {v["ip"]: v for v in SCAN.values()}
    found = {c.bulb.id: c for c in merge_devices(CLOUD, by_ip)}
    assert found["bulb1"].bulb.ip == "192.168.1.40"


def test_bad_scan_version_falls_back():
    scan = {"bulb1": {"gwId": "bulb1", "ip": "10.0.0.9", "version": ""}}
    found = {c.bulb.id: c for c in merge_devices(CLOUD, scan)}
    assert found["bulb1"].bulb.version == 3.3


def test_merge_into_appends_new_and_updates_existing():
    from screenlight.discovery import merge_into

    existing = (Bulb("My desk", "bulb1", ip="10.0.0.5", key="old", version=3.3, enabled=False),)
    found = [Bulb("Desk lamp", "bulb1", ip="10.0.0.9", key="new", version=3.5), Bulb("Hall", "h1", key="kh")]
    bulbs, added, updated = merge_into(existing, found)
    assert bulbs == (Bulb("My desk", "bulb1", ip="10.0.0.9", key="new", version=3.5, enabled=False),
                     Bulb("Hall", "h1", key="kh"))
    assert (added, updated) == (1, 1)


def test_merge_into_keeps_known_ip_when_bulb_not_seen_on_lan():
    from screenlight.discovery import merge_into

    existing = (Bulb("Desk", "bulb1", ip="10.0.0.5", key="old", version=3.4),)
    bulbs, _, _ = merge_into(existing, [Bulb("Desk", "bulb1", ip="Auto", key="new")])
    assert bulbs == (Bulb("Desk", "bulb1", ip="10.0.0.5", key="new", version=3.4),)


def test_refresh_keeps_beacon_fields_and_clears_stale_beacon_key():
    from screenlight.discovery import merge_into
    old = Bulb(name="BT", id="x", ip="Bluetooth", key="aaaaaaaaaaaaaaaa", kind="beacon", node_id=2,
               beacon_key="11" * 16)
    same, _, _ = merge_into((old,), [Bulb(name="BT", id="x", ip="Auto", key="aaaaaaaaaaaaaaaa")])
    assert same[0] == old
    changed, _, _ = merge_into((old,), [Bulb(name="BT", id="x", ip="10.0.0.9", key="bbbbbbbbbbbbbbbb", version=3.5)])
    assert changed[0].beacon_key == "" and changed[0].key == "bbbbbbbbbbbbbbbb"
    assert changed[0].kind == "beacon" and changed[0].ip == "Bluetooth" and changed[0].version == old.version


def test_bluetooth_setup_result_converts_or_refreshes_existing_bulb():
    from screenlight.discovery import merge_into
    wifi = Bulb(name="My lamp", id="x", ip="Auto", key="aaaaaaaaaaaaaaaa", enabled=False)
    setup = Bulb(name="Smart BT", id="x", ip="Bluetooth", key="aaaaaaaaaaaaaaaa", kind="beacon", node_id=2,
                 beacon_key="22" * 16)
    bulbs, added, updated = merge_into((wifi,), [setup])
    assert (added, updated) == (0, 1)
    assert bulbs[0] == Bulb(name="My lamp", id="x", ip="Bluetooth", key="aaaaaaaaaaaaaaaa", enabled=False,
                            kind="beacon", node_id=2, beacon_key="22" * 16)
