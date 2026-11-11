"""Turn a block of screen pixels into one bulb colour (HSV floats, 0-1)."""

import colorsys

import numpy as np

HSV = tuple[float, float, float]

# Pixels darker than this (letterbox bars, black UI) never influence the hue.
_DARK_CUTOFF = 0.08
# Fraction of "fully saturated" weight needed before vibrant mode trusts its hue
# completely; below that it blends towards the plain average (i.e. white-ish light).
_CONFIDENT_WEIGHT = 0.05


def _normalised(rgb: np.ndarray) -> np.ndarray:
    peak = rgb.max()
    return rgb / peak if peak > 0 else rgb


def pick_colour(
    pixels: np.ndarray,
    mode: str = "vibrant",
    saturation_boost: float = 1.3,
    min_brightness: float = 0.1,
    max_brightness: float = 1.0,
) -> HSV:
    rgb = pixels.reshape(-1, 3).astype(np.float32) / 255.0
    peak = rgb.max(axis=1)
    plain = rgb.mean(axis=0)

    if mode == "plain":
        colour = plain
    else:
        sat = (peak - rgb.min(axis=1)) / np.maximum(peak, 1e-6)
        weight = sat**2 * peak
        weight[peak < _DARK_CUTOFF] = 0.0
        total = float(weight.sum())
        if total > 0:
            vivid = (rgb * weight[:, None]).sum(axis=0) / total
            confidence = min(1.0, total / (len(rgb) * _CONFIDENT_WEIGHT))
            colour = confidence * _normalised(vivid) + (1 - confidence) * _normalised(plain)
        else:
            colour = plain

    h, s, _ = colorsys.rgb_to_hsv(*(float(c) for c in colour))
    s = min(1.0, s * saturation_boost)
    v = min_brightness + (max_brightness - min_brightness) * float(peak.mean())
    return (h, s, v)


class Smoother:
    """Exponential moving average in RGB space (avoids hue wrap-around artefacts)."""

    def __init__(self, smoothing: float):
        self.smoothing = smoothing
        self._rgb: np.ndarray | None = None

    def update(self, hsv: HSV) -> HSV:
        rgb = np.array(colorsys.hsv_to_rgb(*hsv))
        if self._rgb is None:
            self._rgb = rgb
        else:
            self._rgb = self.smoothing * self._rgb + (1 - self.smoothing) * rgb
        return colorsys.rgb_to_hsv(*(float(c) for c in self._rgb))


# Colour correction for Tuya Bluetooth beacon bulbs, tuned by eye with tools/beacon_calibrate.py.
# (screen hue°, hue° to send so the bulb looks the same), sorted, covering 0 and 360.
BEACON_HUE_POINTS: tuple[tuple[float, float], ...] = (
    (0, 0), (30, 4), (60, 14), (90, 28), (120, 50), (150, 122), (180, 152),
    (210, 238), (240, 242), (270, 268), (300, 306), (330, 358), (360, 360),
)  # tuned on the user's "Smart BT Lights" bulb, 2026-11-11
# Lowest saturation that still reads as a colour on the bulb (its white LEDs wash out pale colours).
BEACON_MIN_SATURATION = 0.1
_NEAR_WHITE = 0.1


def correct_for_beacon(hsv: HSV) -> HSV:
    h, s, v = hsv
    deg = (h * 360) % 360
    for (x0, y0), (x1, y1) in zip(BEACON_HUE_POINTS, BEACON_HUE_POINTS[1:]):
        if x0 <= deg <= x1:
            deg = y0 + (y1 - y0) * (deg - x0) / (x1 - x0) if x1 > x0 else y0
            break
    if s >= _NEAR_WHITE:
        s = BEACON_MIN_SATURATION + (1 - BEACON_MIN_SATURATION) * s
    return ((deg % 360) / 360, s, v)
