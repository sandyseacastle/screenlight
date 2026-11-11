import colorsys

import numpy as np
import pytest

from screenlight.colour import Smoother, pick_colour


def solid(rgb, h=10, w=10):
    return np.full((h, w, 3), rgb, dtype=np.uint8)


@pytest.mark.parametrize("mode", ["vibrant", "plain"])
@pytest.mark.parametrize(
    "rgb,hue",
    [((255, 0, 0), 0.0), ((0, 255, 0), 1 / 3), ((0, 0, 255), 2 / 3), ((255, 255, 0), 1 / 6)],
)
def test_solid_colour_round_trips(mode, rgb, hue):
    h, s, v = pick_colour(solid(rgb), mode=mode, saturation_boost=1.0)
    assert h == pytest.approx(hue, abs=1e-3)
    assert s == pytest.approx(1.0)
    assert v == pytest.approx(1.0)


def test_vibrant_leans_towards_colourful_pixels():
    frame = np.concatenate([solid((255, 0, 0)), solid((128, 128, 128))], axis=1)
    _, plain_s, _ = pick_colour(frame, mode="plain", saturation_boost=1.0)
    h, vib_s, _ = pick_colour(frame, mode="vibrant", saturation_boost=1.0)
    assert h == pytest.approx(0.0, abs=1e-3)
    assert vib_s > 0.95
    assert vib_s > plain_s + 0.2


def test_black_bars_do_not_affect_hue_or_saturation():
    frame = np.concatenate([solid((0, 0, 0), h=30), solid((0, 0, 255), h=10)], axis=0)
    h, s, _ = pick_colour(frame, mode="vibrant", saturation_boost=1.0)
    assert h == pytest.approx(2 / 3, abs=1e-3)
    assert s > 0.95


def test_mostly_grey_scene_stays_near_white():
    frame = solid((100, 100, 100), h=100, w=100)
    frame[0, :] = (255, 0, 0)  # 1% red pixels
    _, s, _ = pick_colour(frame, mode="vibrant", saturation_boost=1.0)
    assert s < 0.3


def test_saturation_boost_is_clamped():
    _, s, _ = pick_colour(solid((200, 100, 100)), mode="plain", saturation_boost=1.5)
    assert s == pytest.approx(0.5 * 1.5)
    _, s, _ = pick_colour(solid((255, 50, 50)), mode="plain", saturation_boost=3.0)
    assert s == 1.0


def test_brightness_maps_into_min_max_range():
    kwargs = dict(min_brightness=0.2, max_brightness=0.8)
    assert pick_colour(solid((0, 0, 0)), **kwargs)[2] == pytest.approx(0.2)
    assert pick_colour(solid((255, 255, 255)), **kwargs)[2] == pytest.approx(0.8)
    assert pick_colour(solid((0, 0, 255)), **kwargs)[2] == pytest.approx(0.8)


def test_smoother_passes_first_value_then_converges():
    sm = Smoother(0.5)
    red, blue = (0.0, 1.0, 1.0), (2 / 3, 1.0, 1.0)
    assert sm.update(red) == pytest.approx(red)
    mid = sm.update(blue)
    assert colorsys.hsv_to_rgb(*mid) == pytest.approx((0.5, 0.0, 0.5))
    for _ in range(40):
        out = sm.update(blue)
    assert out == pytest.approx(blue, abs=1e-6)


def test_smoother_zero_is_passthrough():
    sm = Smoother(0.0)
    sm.update((0.0, 1.0, 1.0))
    assert sm.update((0.5, 0.5, 0.5)) == pytest.approx((0.5, 0.5, 0.5))


def test_beacon_correction_is_identity_with_default_curve(monkeypatch):
    from screenlight import colour
    monkeypatch.setattr(colour, "BEACON_HUE_POINTS", ((0, 0), (360, 360)))
    monkeypatch.setattr(colour, "BEACON_MIN_SATURATION", 0.0)
    assert colour.correct_for_beacon((0.25, 0.7, 0.4)) == pytest.approx((0.25, 0.7, 0.4))


def test_beacon_hue_curve_interpolates(monkeypatch):
    from screenlight import colour
    monkeypatch.setattr(colour, "BEACON_HUE_POINTS", ((0, 0), (30, 15), (360, 360)))
    monkeypatch.setattr(colour, "BEACON_MIN_SATURATION", 0.0)
    assert colour.correct_for_beacon((30 / 360, 1, 1))[0] == pytest.approx(15 / 360)
    assert colour.correct_for_beacon((15 / 360, 1, 1))[0] == pytest.approx(7.5 / 360)
    assert colour.correct_for_beacon((195 / 360, 1, 1))[0] == pytest.approx(187.5 / 360)


def test_beacon_saturation_floor_keeps_near_white_white(monkeypatch):
    from screenlight import colour
    monkeypatch.setattr(colour, "BEACON_HUE_POINTS", ((0, 0), (360, 360)))
    monkeypatch.setattr(colour, "BEACON_MIN_SATURATION", 0.4)
    assert colour.correct_for_beacon((0.5, 0.05, 1))[1] == pytest.approx(0.05)
    assert colour.correct_for_beacon((0.5, 0.5, 1))[1] == pytest.approx(0.7)
    assert colour.correct_for_beacon((0.5, 1.0, 1))[1] == pytest.approx(1.0)
