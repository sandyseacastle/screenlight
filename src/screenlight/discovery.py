"""One-time bulb discovery: local keys from the Tuya cloud, IPs and protocol versions from a LAN scan."""

import dataclasses
from dataclasses import dataclass

from screenlight.config import Bulb

REGIONS = {
    "us": "Western America",
    "us-e": "Eastern America",
    "eu": "Central Europe",
    "eu-w": "Western Europe",
    "in": "India",
    "sg": "Singapore",
    "cn": "China",
}
# Tuya product categories for lights: bulb, strip, string, ceiling, ambiance.
LIGHT_CATEGORIES = {"dj", "dd", "dc", "xdd", "fwd"}
DEFAULT_VERSION = 3.3


class DiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    bulb: Bulb
    category: str
    product: str
    on_lan: bool
    is_light: bool


def _error_text(err: dict) -> str:
    return " - ".join(str(err[k]) for k in ("Error", "Payload") if err.get(k))


def fetch_cloud_devices(region: str, api_key: str, api_secret: str, device_id: str | None = None) -> list[dict]:
    import tinytuya

    try:
        cloud = tinytuya.Cloud(apiRegion=region, apiKey=api_key, apiSecret=api_secret,
                               apiDeviceID=device_id or None)
    except Exception as e:
        raise DiscoveryError(f"Could not connect to Tuya cloud: {e}") from e
    if cloud.error:
        raise DiscoveryError(_error_text(cloud.error))
    devices = cloud.getdevices()
    if isinstance(devices, dict) and "Error" in devices:
        raise DiscoveryError(_error_text(devices))
    return list(devices)


def scan_lan(cloud_devices: list[dict] | None = None) -> dict:
    """Listen for Tuya UDP broadcasts (~18 s). Returns {device id: info} with 'ip' and 'version'."""
    from tinytuya import scanner

    return scanner.devices(verbose=False, color=False, poll=False, byID=True,
                           tuyadevices=cloud_devices or None)


def _version(value) -> float:
    try:
        return float(value) or DEFAULT_VERSION
    except (TypeError, ValueError):
        return DEFAULT_VERSION


def merge_devices(cloud: list[dict], scan: dict) -> list[Candidate]:
    seen = {info.get("gwId") or info.get("id") or key: info for key, info in scan.items()}
    candidates = []
    for dev in cloud:
        # Hub children (Zigbee/BLE) have no IP of their own and can't be driven directly.
        if dev.get("gateway_id") or not dev.get("id"):
            continue
        local = seen.get(dev["id"])
        category = dev.get("category", "")
        bulb = Bulb(
            name=dev.get("name") or dev["id"],
            id=dev["id"],
            ip=(local or {}).get("ip") or "Auto",
            key=dev.get("key", ""),
            version=_version((local or {}).get("version")),
        )
        candidates.append(Candidate(bulb, category, dev.get("product_name", ""), local is not None,
                                    category in LIGHT_CATEGORIES))
    candidates.sort(key=lambda c: (not c.is_light, c.bulb.name.lower()))
    return candidates


def merge_into(existing: tuple[Bulb, ...], found: list[Bulb]) -> tuple[tuple[Bulb, ...], int, int]:
    """Add newly found bulbs; refresh key/IP/version of ones already configured (keeping name, order, enabled).

    Returns (bulbs, added, updated). A bulb the LAN scan missed keeps its last known IP and version.
    """
    bulbs = list(existing)
    index = {b.id: i for i, b in enumerate(bulbs)}
    added = updated = 0
    for new in found:
        if new.id not in index:
            index[new.id] = len(bulbs)
            bulbs.append(new)
            added += 1
            continue
        old = bulbs[index[new.id]]
        changes = {"key": new.key}
        if new.is_beacon:  # a Bluetooth setup result: take its beacon identity
            changes.update(kind=new.kind, node_id=new.node_id, beacon_key=new.beacon_key, ip=new.ip)
        elif old.is_beacon:
            if new.key != old.key:
                changes["beacon_key"] = ""  # re-paired: the beacon key is stale, Bluetooth setup must run again
        elif new.ip != "Auto":
            changes.update(ip=new.ip, version=new.version)
        bulbs[index[new.id]] = dataclasses.replace(old, **changes)
        updated += 1
    return tuple(bulbs), added, updated
