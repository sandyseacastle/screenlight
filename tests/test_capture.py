import os
import sys

import numpy as np
import pytest

from screenlight.capture import Grabber, list_monitors, scale_frame, thumbnail_ppm

needs_display = pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DISPLAY"), reason="needs an X11 display"
)


@needs_display
def test_list_monitors_numbers_from_one():
    monitors = list_monitors()
    assert monitors and [m.index for m in monitors] == list(range(1, len(monitors) + 1))
    assert all(m.width > 0 and m.height > 0 for m in monitors)
    assert monitors[0].label.startswith("1: ")


@needs_display
def test_grab_returns_small_rgb_frame():
    grabber = Grabber(1, target_width=160)
    try:
        frame = grabber.grab()
    finally:
        grabber.close()
    assert frame.dtype == np.uint8 and frame.ndim == 3 and frame.shape[2] == 3
    assert 80 <= frame.shape[1] <= 320


@needs_display
def test_unknown_monitor_falls_back_to_first():
    grabber = Grabber(99)
    try:
        assert grabber.monitor.index == 1
    finally:
        grabber.close()


def test_thumbnail_ppm_header_and_size():
    frame = np.zeros((9, 16, 3), dtype=np.uint8)
    frame[..., 0] = 255
    data = thumbnail_ppm(frame)
    assert data.startswith(b"P6 16 9 255\n")
    assert data.endswith(b"\xff\x00\x00")
    assert len(data) == len(b"P6 16 9 255\n") + 16 * 9 * 3


def test_scale_frame_nearest_neighbour():
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    frame[0, 1] = (255, 0, 0)
    big = scale_frame(frame, 4, 4)
    assert big.shape == (4, 4, 3)
    assert (big[:2, 2:] == (255, 0, 0)).all()
    assert (big[2:, :] == 0).all()


def test_downsample_box_averages_bgra_to_rgb():
    from screenlight.capture import downsample

    bgra = np.zeros((4, 8, 4), dtype=np.uint8)
    bgra[:, :4, 2] = 255  # left half red (BGRA)
    bgra[0, 4:, 0] = 255  # top row of right half blue
    out = downsample(bgra, stride=1, box=4)
    assert out.shape == (1, 2, 3)
    assert out[0, 0].tolist() == [255, 0, 0]
    assert out[0, 1].tolist() == [0, 0, 63]


def test_downsample_drops_ragged_edges():
    from screenlight.capture import downsample

    out = downsample(np.zeros((1081, 1921, 4), dtype=np.uint8), stride=4, box=3)
    assert out.shape == (90, 160, 3)
