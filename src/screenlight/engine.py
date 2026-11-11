"""Capture loop: grab the screen, pick one colour per region, hand each colour to its bulb."""

import threading
import time
from dataclasses import dataclass

import numpy as np

from screenlight.bulbs import BulbWorker, make_device
from screenlight.capture import Grabber
from screenlight.colour import HSV, Smoother, pick_colour
from screenlight.config import Settings
from screenlight.presets import hex_to_hsv
from screenlight.regions import Region, split_regions


@dataclass(frozen=True)
class Preview:
    frame: np.ndarray | None  # downsampled RGB capture; None in manual mode
    regions: list[Region]  # in frame pixel coordinates
    colours: list[HSV]
    statuses: list[str]


def manual_colours(s: Settings, bulb_ids: list[str]) -> list[HSV]:
    """Each bulb's own colour (looked up live, so edits apply while running), dimmed by Max brightness."""
    by_id = {b.id: b for b in s.bulbs}
    colours = []
    for bulb_id in bulb_ids:
        h, sat, v = hex_to_hsv(by_id[bulb_id].colour) if bulb_id in by_id else (0.0, 0.0, 1.0)
        colours.append((h, sat, v * s.max_brightness))
    return colours


def region_colours(frame: np.ndarray, n: int, s: Settings) -> tuple[list[Region], list[HSV]]:
    regions = split_regions(n, frame.shape[1], frame.shape[0])
    colours = [
        pick_colour(frame[y0:y1, x0:x1], s.mode, s.saturation_boost, s.min_brightness, s.max_brightness)
        for x0, y0, x1, y1 in regions
    ]
    return regions, colours


class SyncEngine:
    """Runs until stop(). Monitor and bulb list are fixed for a run; other settings apply live.

    radio is the app's BeaconRadio for Bluetooth beacon bulbs; the engine borrows it and never closes it.
    """

    def __init__(self, settings: Settings, on_frame=None, on_error=None, grabber_factory=Grabber,
                 worker_factory=None, preview_interval: float = 0.2, radio=None):
        if worker_factory is None:
            def worker_factory(bulb, max_rate):
                return BulbWorker(bulb, max_rate=max_rate, device_factory=lambda b: make_device(b, radio))
        self.settings = settings
        self._on_frame = on_frame
        self._on_error = on_error
        self._grabber_factory = grabber_factory
        self._preview_interval = preview_interval
        self._workers = [worker_factory(b, settings.max_bulb_rate) for b in settings.enabled_bulbs]
        self._smoothers = [Smoother(settings.smoothing) for _ in range(max(len(self._workers), 1))]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sync-engine", daemon=True)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        for worker in self._workers:
            worker.start()
        self._thread.start()

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def identify(self, bulb_id: str) -> bool:
        for worker in self._workers:
            if worker.bulb.id == bulb_id:
                worker.identify()
                return True
        return False

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        # Stop bulbs in parallel so one unreachable bulb doesn't delay the rest.
        stoppers = [threading.Thread(target=w.stop, args=(self.settings.on_stop,)) for w in self._workers]
        for t in stoppers:
            t.start()
        for t in stoppers:
            t.join()

    def _run(self) -> None:
        grabber = None
        manual = self.settings.source == "manual"  # like the monitor, fixed for a run
        try:
            if not manual:
                grabber = self._grabber_factory(self.settings.monitor)
            next_preview = 0.0
            while not self._stop.is_set():
                started = time.monotonic()
                s = self.settings
                if manual:
                    frame, regions, raw = None, [], manual_colours(s, [w.bulb.id for w in self._workers])
                else:
                    frame = grabber.grab()
                    regions, raw = region_colours(frame, len(self._smoothers), s)
                colours = []
                for smoother, hsv in zip(self._smoothers, raw):
                    smoother.smoothing = s.smoothing
                    colours.append(smoother.update(hsv))
                for worker, hsv in zip(self._workers, colours):
                    worker.max_rate = s.max_bulb_rate
                    worker.submit(hsv)
                if self._on_frame and started >= next_preview:
                    next_preview = started + self._preview_interval
                    self._on_frame(Preview(frame, regions, colours,
                                           [w.status for w in self._workers]))
                self._stop.wait(max(0.0, 1.0 / max(s.fps, 1) - (time.monotonic() - started)))
        except Exception as e:
            self._stop.set()
            if self._on_error:
                self._on_error(f"Screen capture failed: {e}")
        finally:
            if grabber is not None:
                grabber.close()
