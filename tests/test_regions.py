import numpy as np
import pytest

from screenlight.regions import region_label, split_regions


def coverage(regions, w, h):
    grid = np.zeros((h, w), dtype=int)
    for x0, y0, x1, y1 in regions:
        grid[y0:y1, x0:x1] += 1
    return grid


@pytest.mark.parametrize("n", range(0, 8))
@pytest.mark.parametrize("w,h", [(160, 90), (161, 91), (7, 5)])
def test_regions_cover_every_pixel_exactly_once(n, w, h):
    regions = split_regions(n, w, h)
    assert len(regions) == max(n, 1)
    assert (coverage(regions, w, h) == 1).all()


def test_one_region_is_whole_frame():
    assert split_regions(1, 100, 50) == [(0, 0, 100, 50)]


def test_two_regions_are_left_and_right_halves():
    assert split_regions(2, 100, 50) == [(0, 0, 50, 50), (50, 0, 100, 50)]


def test_three_regions_are_vertical_thirds():
    assert split_regions(3, 90, 50) == [(0, 0, 30, 50), (30, 0, 60, 50), (60, 0, 90, 50)]


def test_four_regions_are_quadrants_tl_tr_bl_br():
    assert split_regions(4, 100, 50) == [
        (0, 0, 50, 25),
        (50, 0, 100, 25),
        (0, 25, 50, 50),
        (50, 25, 100, 50),
    ]


def test_five_or_more_regions_are_vertical_strips():
    regions = split_regions(5, 100, 50)
    assert all(y0 == 0 and y1 == 50 for _, y0, _, y1 in regions)
    assert [x0 for x0, _, _, _ in regions] == [0, 20, 40, 60, 80]


@pytest.mark.parametrize(
    "n,labels",
    [
        (1, ["Whole screen"]),
        (2, ["Left", "Right"]),
        (3, ["Left", "Middle", "Right"]),
        (4, ["Top-left", "Top-right", "Bottom-left", "Bottom-right"]),
        (5, ["Strip 1", "Strip 2", "Strip 3", "Strip 4", "Strip 5"]),
    ],
)
def test_region_labels(n, labels):
    assert [region_label(i, n) for i in range(n)] == labels
