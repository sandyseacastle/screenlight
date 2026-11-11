"""Settings and bulb list, persisted as JSON in the user's config directory.

The file holds Tuya local keys, so it is written owner-readable only.
"""

import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Bulb:
    name: str
    id: str
    ip: str = "Auto"
    key: str = ""
    version: float = 3.3
    enabled: bool = True
    kind: str = "wifi"  # "wifi" | "beacon" (Tuya Bluetooth beacon bulb)
    node_id: int = 0  # beacon only: the bulb's mesh node ID
    beacon_key: str = ""  # beacon only: 16-byte mesh beacon key, hex
    colour: str = "#ffffff"  # manual mode colour, "#rrggbb"

    @property
    def is_beacon(self) -> bool:
        return self.kind == "beacon"


@dataclass(frozen=True)
class Settings:
    source: str = "screen"  # "screen" (follow the display) | "manual" (each bulb's own colour)
    monitor: int = 1
    bulbs: tuple[Bulb, ...] = field(default_factory=tuple)
    mode: str = "vibrant"  # "vibrant" | "plain"
    saturation_boost: float = 1.3
    smoothing: float = 0.4
    min_brightness: float = 0.1
    max_brightness: float = 1.0
    fps: int = 15
    max_bulb_rate: float = 8.0
    on_stop: str = "keep"  # "keep" | "white" | "off"

    @property
    def enabled_bulbs(self) -> tuple[Bulb, ...]:
        return tuple(b for b in self.bulbs if b.enabled)


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "screenlight" / "config.json"


def beacon_state_path() -> Path:
    """Source ID and sequence reservations for Bluetooth beacon bulbs (written by the radio)."""
    return config_path().parent / "beacon_state.json"


def _coerce(cls, data: dict, overrides: dict):
    """Build a dataclass from `data`, keeping the default for any field that is missing or invalid."""
    defaults = cls(**overrides)
    values = dict(overrides)
    for f in dataclasses.fields(cls):
        if f.name in values or f.name not in data:
            continue
        default = getattr(defaults, f.name)
        try:
            values[f.name] = type(default)(data[f.name])
        except (TypeError, ValueError):
            pass
    return cls(**values)


def _bulb_from_dict(data) -> Bulb | None:
    if not isinstance(data, dict) or not data.get("id"):
        return None
    dev_id = str(data["id"])
    return _coerce(Bulb, data, {"id": dev_id, "name": str(data.get("name") or dev_id)})


def from_dict(data: dict) -> Settings:
    raw_bulbs = data.get("bulbs")
    bulbs = tuple(b for b in map(_bulb_from_dict, raw_bulbs if isinstance(raw_bulbs, list) else []) if b)
    return _coerce(Settings, data, {"bulbs": bulbs})


def load(path: Path | None = None) -> Settings:
    path = path or config_path()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    return from_dict(data) if isinstance(data, dict) else Settings()


def save(settings: Settings, path: Path | None = None) -> None:
    path = Path(path or config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(settings), f, indent=2)
    os.replace(tmp, path)
