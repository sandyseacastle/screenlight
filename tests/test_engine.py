import threading
import time

import numpy as np
import pytest

from screenlight.config import Bulb, Settings
from screenlight.engine import SyncEngine

RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)


class FakeGrabber:
    def __init__(self, frame):
        self.frame = frame
        self.closed = False

    def grab(self):
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame

    def close(self):
        self.closed = True


class FakeWorker:
    def __init__(self, bulb, max_rate):
        self.bulb = bulb
        self.max_rate = max_rate
        self.status = "online"
        self.submitted = []
        self.identified = 0
        self.stopped_with = None

    def start(self):
        pass

    def submit(self, hsv):
        self.submitted.append(hsv)

    def identify(self):
        self.identified += 1

    def stop(self, on_stop="keep"):
        self.stopped_with = on_stop


def halves(left, right, h=90, w=160):
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, : w // 2] = left
    frame[:, w // 2 :] = right
    return frame


def quadrants(tl, tr, bl, br, h=90, w=160):
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[: h // 2, : w // 2], frame[: h // 2, w // 2 :] = tl, tr
    frame[h // 2 :, : w // 2], frame[h // 2 :, w // 2 :] = bl, br
    return frame


def bulbs(n, **kw):
    return tuple(Bulb(name=f"b{i}", id=f"id{i}", **kw) for i in range(n))


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def run_engine(settings, frame, **kwargs):
    grabber = FakeGrabber(frame)
    workers = []

    def worker_factory(bulb, max_rate):
        workers.append(FakeWorker(bulb, max_rate))
        return workers[-1]

    engine = SyncEngine(settings, grabber_factory=lambda monitor: grabber, worker_factory=worker_factory,
                        **kwargs)
    engine.start()
    return engine, grabber, workers


def hue(hsv):
    return round(hsv[0], 3)


def test_two_bulbs_follow_left_and_right_halves():
    settings = Settings(bulbs=bulbs(2), smoothing=0.0, fps=100)
    engine, _, workers = run_engine(settings, halves(RED, BLUE))
    assert wait_for(lambda: all(w.submitted for w in workers))
    engine.stop()
    assert [hue(w.submitted[-1]) for w in workers] == [0.0, round(2 / 3, 3)]


def test_four_bulbs_follow_quadrants():
    settings = Settings(bulbs=bulbs(4), smoothing=0.0, fps=100)
    engine, _, workers = run_engine(settings, quadrants(RED, GREEN, BLUE, YELLOW))
    assert wait_for(lambda: all(w.submitted for w in workers))
    engine.stop()
    assert [hue(w.submitted[-1]) for w in workers] == [0.0, round(1 / 3, 3), round(2 / 3, 3), round(1 / 6, 3)]


def test_disabled_bulbs_are_skipped():
    settings = Settings(bulbs=(Bulb("a", "a"), Bulb("b", "b", enabled=False), Bulb("c", "c")),
                        smoothing=0.0, fps=100)
    engine, _, workers = run_engine(settings, halves(RED, BLUE))
    assert [w.bulb.id for w in workers] == ["a", "c"]
    assert wait_for(lambda: all(w.submitted for w in workers))
    engine.stop()
    assert hue(workers[1].submitted[-1]) == round(2 / 3, 3)


def test_no_bulbs_still_previews():
    frames = []
    engine, _, workers = run_engine(Settings(fps=100), halves(RED, RED), on_frame=frames.append,
                                    preview_interval=0.0)
    assert wait_for(lambda: frames)
    engine.stop()
    preview = frames[-1]
    assert workers == []
    assert len(preview.colours) == 1 and hue(preview.colours[0]) == 0.0
    assert preview.regions == [(0, 0, 160, 90)]
    assert preview.frame.shape == (90, 160, 3)


def test_preview_reports_bulb_statuses():
    frames = []
    engine, _, workers = run_engine(Settings(bulbs=bulbs(2), fps=100), halves(RED, BLUE),
                                    on_frame=frames.append, preview_interval=0.0)
    workers[1].status = "offline: timeout"
    assert wait_for(lambda: frames and frames[-1].statuses == ["online", "offline: timeout"])
    engine.stop()


def test_stop_applies_on_stop_and_closes_grabber():
    settings = Settings(bulbs=bulbs(2), on_stop="off", fps=100)
    engine, grabber, workers = run_engine(settings, halves(RED, BLUE))
    assert wait_for(lambda: all(w.submitted for w in workers))
    engine.stop()
    assert not engine.running
    assert grabber.closed
    assert [w.stopped_with for w in workers] == ["off", "off"]


def test_live_settings_update():
    settings = Settings(bulbs=bulbs(1), smoothing=0.0, fps=100, max_bulb_rate=8.0, min_brightness=0.0,
                        max_brightness=1.0)
    engine, _, workers = run_engine(settings, halves(RED, RED))
    assert wait_for(lambda: workers[0].submitted)
    engine.update_settings(Settings(bulbs=bulbs(1), smoothing=0.0, fps=100, max_bulb_rate=3.0,
                                    min_brightness=0.0, max_brightness=0.5))
    assert wait_for(lambda: workers[0].submitted[-1][2] == pytest.approx(0.5))
    assert workers[0].max_rate == 3.0
    engine.stop()


def test_identify_by_bulb_id():
    engine, _, workers = run_engine(Settings(bulbs=bulbs(2), fps=100), halves(RED, BLUE))
    assert engine.identify("id1")
    assert not engine.identify("missing")
    engine.stop()
    assert [w.identified for w in workers] == [0, 1]


def test_capture_error_is_reported_and_engine_stops():
    errors = []
    done = threading.Event()

    def on_error(msg):
        errors.append(msg)
        done.set()

    engine, grabber, _ = run_engine(Settings(bulbs=bulbs(1)), RuntimeError("monitor unplugged"),
                                    on_error=on_error)
    assert done.wait(2)
    assert "monitor unplugged" in errors[0]
    assert wait_for(lambda: not engine.running)
    engine.stop()
    assert grabber.closed


def manual_settings(**kw):
    return Settings(source="manual", smoothing=0.0, fps=100,
                    bulbs=(Bulb("A", "a", colour="#ff0000"), Bulb("B", "b", colour="#0000ff")), **kw)


def test_manual_mode_sends_each_bulb_its_colour_without_capturing():
    engine, _, workers = run_engine(manual_settings(), RuntimeError("must not grab in manual mode"))
    assert wait_for(lambda: all(w.submitted for w in workers))
    engine.stop()
    assert [hue(w.submitted[-1]) for w in workers] == [0.0, round(2 / 3, 3)]


def test_manual_mode_never_creates_a_grabber():
    def no_grabber(monitor):
        raise AssertionError("grabber created in manual mode")

    workers = []
    engine = SyncEngine(manual_settings(), grabber_factory=no_grabber,
                        worker_factory=lambda b, r: workers.append(FakeWorker(b, r)) or workers[-1])
    engine.start()
    assert wait_for(lambda: all(w.submitted for w in workers))
    assert engine.running
    engine.stop()


def test_manual_colour_change_applies_live():
    import dataclasses
    settings = manual_settings()
    engine, _, workers = run_engine(settings, RuntimeError("no grab"))
    assert wait_for(lambda: workers[0].submitted)
    a, b = settings.bulbs
    engine.update_settings(dataclasses.replace(settings, bulbs=(dataclasses.replace(a, colour="#00ff00"), b)))
    assert wait_for(lambda: hue(workers[0].submitted[-1]) == round(1 / 3, 3))
    engine.stop()


def test_manual_mode_max_brightness_dims():
    engine, _, workers = run_engine(manual_settings(max_brightness=0.5), RuntimeError("no grab"))
    assert wait_for(lambda: all(w.submitted for w in workers))
    engine.stop()
    assert workers[0].submitted[-1][2] == pytest.approx(0.5)


def test_manual_mode_preview_has_no_frame():
    previews = []
    engine, _, workers = run_engine(manual_settings(), RuntimeError("no grab"), on_frame=previews.append)
    assert wait_for(lambda: previews)
    engine.stop()
    assert previews[0].frame is None and len(previews[0].colours) == 2
