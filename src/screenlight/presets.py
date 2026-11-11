"""Manual colours: hex helpers and built-in gradient presets spread across the bulbs."""

import colorsys
import dataclasses

from screenlight.colour import HSV
from screenlight.config import Bulb

# Kept bright and saturated: dark or pale stops look dim or washed out on bulbs.
GRADIENTS: dict[str, list[str]] = {
    "Sunset": ["#ff7b00", "#ff2a68", "#8a2be2"],
    "Ocean": ["#00f5d4", "#00bbf9", "#3a0ca3"],
    "Forest": ["#d9ed92", "#52b788", "#2d6a4f"],
    "Fire": ["#ffea00", "#ff7b00", "#d00000"],
    "Aurora": ["#00ffa3", "#00b4ff", "#b100ff"],
    "Candy": ["#ff70a6", "#c77dff", "#70d6ff"],
    "Rainbow": ["#ff0000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff"],
    "Warm white": ["#ffd8a8", "#ffb366"],
}


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(hex_colour)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def hex_to_hsv(hex_colour: str) -> HSV:
    try:
        r, g, b = _rgb(hex_colour)
    except ValueError:
        return (0.0, 0.0, 1.0)
    return colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)


def gradient_colours(stops: list[str], n: int) -> list[str]:
    """n evenly spaced colours along the stops (RGB interpolation); a single bulb gets the middle."""
    rgb = [_rgb(s) for s in stops]
    out = []
    for i in range(n):
        t = 0.5 if n == 1 else i / (n - 1)
        pos = t * (len(rgb) - 1)
        k = min(int(pos), len(rgb) - 2)
        f = pos - k
        mixed = (round(a + (b - a) * f) for a, b in zip(rgb[k], rgb[k + 1]))
        out.append("#%02x%02x%02x" % tuple(mixed))
    return out


def apply_gradient(bulbs: tuple[Bulb, ...], stops: list[str]) -> tuple[Bulb, ...]:
    """Spread the gradient over the enabled bulbs in list order (left to right, as in screen sync)."""
    enabled = [i for i, b in enumerate(bulbs) if b.enabled]
    colours = dict(zip(enabled, gradient_colours(stops, len(enabled))))
    return tuple(dataclasses.replace(b, colour=colours[i]) if i in colours else b for i, b in enumerate(bulbs))
