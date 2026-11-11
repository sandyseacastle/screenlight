"""Split a frame into one screen region per bulb.

1 bulb  -> whole screen
2 bulbs -> left / right halves
3 bulbs -> vertical thirds
4 bulbs -> 2x2 quadrants (top-left, top-right, bottom-left, bottom-right)
5+      -> equal vertical strips, left to right
"""

Region = tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)


def _edges(length: int, parts: int) -> list[int]:
    return [round(length * i / parts) for i in range(parts + 1)]


def split_regions(n: int, width: int, height: int) -> list[Region]:
    n = max(n, 1)
    if n == 4:
        xs, ys = _edges(width, 2), _edges(height, 2)
        return [(xs[c], ys[r], xs[c + 1], ys[r + 1]) for r in range(2) for c in range(2)]
    xs = _edges(width, n)
    return [(xs[i], 0, xs[i + 1], height) for i in range(n)]


_LABELS = {
    1: ["Whole screen"],
    2: ["Left", "Right"],
    3: ["Left", "Middle", "Right"],
    4: ["Top-left", "Top-right", "Bottom-left", "Bottom-right"],
}


def region_label(index: int, n: int) -> str:
    labels = _LABELS.get(max(n, 1))
    return labels[index] if labels else f"Strip {index + 1}"
