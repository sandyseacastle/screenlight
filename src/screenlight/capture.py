"""Screen capture of a single monitor, downsampled for cheap colour maths."""

from dataclasses import dataclass

import mss
import numpy as np


@dataclass(frozen=True)
class Monitor:
    index: int  # 1-based, matching mss.monitors (0 is the combined virtual screen)
    left: int
    top: int
    width: int
    height: int
    name: str = ""
    primary: bool = False

    @property
    def label(self) -> str:
        name = f" {self.name}" if self.name else ""
        primary = " (primary)" if self.primary else ""
        return f"{self.index}:{name} {self.width}×{self.height} @ {self.left},{self.top}{primary}"


def _monitors(sct) -> list[Monitor]:
    return [
        Monitor(i, m["left"], m["top"], m["width"], m["height"], m.get("output", ""), bool(m.get("is_primary")))
        for i, m in enumerate(sct.monitors[1:], start=1)
    ]


def list_monitors() -> list[Monitor]:
    with mss.MSS() as sct:
        return _monitors(sct)


class Grabber:
    """Captures one monitor. Create and use it on the same thread: mss is not thread-safe on X11."""

    def __init__(self, monitor_index: int, target_width: int = 160):
        self._sct = mss.MSS()
        monitors = _monitors(self._sct)
        self.monitor = next((m for m in monitors if m.index == monitor_index), monitors[0])
        self._region = {k: getattr(self.monitor, k) for k in ("left", "top", "width", "height")}
        step = max(1, self.monitor.width // target_width)
        # Sample every few pixels, then box-average: smooth enough for a preview, cheap enough for 15+ fps.
        self._stride = max(1, step // 3)
        self._box = max(1, step // self._stride)

    def grab(self) -> np.ndarray:
        """Return an (h, w, 3) uint8 RGB frame roughly `target_width` pixels wide."""
        return downsample(np.asarray(self._sct.grab(self._region)), self._stride, self._box)

    def close(self) -> None:
        self._sct.close()


def downsample(bgra: np.ndarray, stride: int, box: int) -> np.ndarray:
    """BGRA capture -> RGB, taking every `stride`th pixel then averaging `box`x`box` blocks."""
    sampled = bgra[::stride, ::stride, :3]
    h, w = sampled.shape[0] // box * box, sampled.shape[1] // box * box
    blocks = sampled[:h, :w].reshape(h // box, box, w // box, box, 3).astype(np.uint16)
    mean = blocks.sum(axis=3).sum(axis=1) // (box * box)
    return np.ascontiguousarray(mean.astype(np.uint8)[..., ::-1])


def thumbnail_ppm(frame: np.ndarray) -> bytes:
    """Encode an RGB frame as binary PPM, which Tk's PhotoImage reads without PIL."""
    h, w, _ = frame.shape
    return b"P6 %d %d 255\n" % (w, h) + np.ascontiguousarray(frame, dtype=np.uint8).tobytes()


def scale_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Nearest-neighbour resize, enough for a preview thumbnail."""
    h, w, _ = frame.shape
    ys = np.arange(height) * h // height
    xs = np.arange(width) * w // width
    return frame[ys][:, xs]
