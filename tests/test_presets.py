import pytest

from screenlight.config import Bulb
from screenlight.presets import GRADIENTS, apply_gradient, gradient_colours, hex_to_hsv


def test_hex_to_hsv():
    assert hex_to_hsv("#ff0000") == pytest.approx((0.0, 1.0, 1.0))
    assert hex_to_hsv("#0000FF") == pytest.approx((2 / 3, 1.0, 1.0))
    assert hex_to_hsv("#ffffff") == pytest.approx((0.0, 0.0, 1.0))


def test_hex_to_hsv_falls_back_to_white_on_garbage():
    assert hex_to_hsv("not a colour") == (0.0, 0.0, 1.0)


def test_gradient_endpoints_are_exact_and_midpoints_interpolate():
    assert gradient_colours(["#ff0000", "#0000ff"], 3) == ["#ff0000", "#800080", "#0000ff"]


def test_gradient_with_one_bulb_takes_the_middle():
    assert gradient_colours(["#ff0000", "#00ff00", "#0000ff"], 1) == ["#00ff00"]


def test_gradient_with_more_bulbs_than_stops():
    colours = gradient_colours(["#000000", "#ffffff"], 5)
    assert colours == ["#000000", "#404040", "#808080", "#bfbfbf", "#ffffff"]


def test_gradient_with_no_bulbs():
    assert gradient_colours(["#000000", "#ffffff"], 0) == []


def test_presets_are_well_formed():
    assert len(GRADIENTS) == 8
    for name, stops in GRADIENTS.items():
        assert len(stops) >= 2, name
        for stop in stops:
            hex_to_hsv(stop)


def test_apply_gradient_colours_enabled_bulbs_in_order():
    bulbs = (Bulb("A", "a"), Bulb("Off", "x", enabled=False, colour="#123456"), Bulb("B", "b"), Bulb("C", "c"))
    out = apply_gradient(bulbs, ["#ff0000", "#0000ff"])
    assert [b.colour for b in out] == ["#ff0000", "#123456", "#800080", "#0000ff"]
    assert [b.id for b in out] == ["a", "x", "b", "c"]
