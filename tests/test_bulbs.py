import threading
import time

import pytest

from screenlight.bulbs import BulbWorker
from screenlight.config import Bulb

RED, GREEN, BLUE, WHITE = (0.0, 1.0, 1.0), (1 / 3, 1.0, 1.0), (2 / 3, 1.0, 1.0), (0.0, 0.0, 1.0)


class FakeDevice:
    def __init__(self, send_delay=0.0, results=None, colour=True):
        self.calls = []
        self.send_delay = send_delay
        self.results = list(results or [])
        self.colour = colour
        self.closed = False
        self.sent = threading.Event()

    def status(self):
        return {"dps": {"20": True, "21": "colour"}}

    def bulb_has_capability(self, feature):
        return self.colour

    def set_hsv(self, h, s, v, nowait=False):
        self.calls.append(("hsv", (h, s, v)))
        time.sleep(self.send_delay)
        self.sent.set()
        return self.results.pop(0) if self.results else {"dps": {}}

    def turn_on(self, nowait=False):
        self.calls.append(("on",))

    def turn_off(self, nowait=False):
        self.calls.append(("off",))

    def set_white_percentage(self, brightness=100, colourtemp=0, nowait=False):
        self.calls.append(("white", brightness, colourtemp))

    def close(self):
        self.closed = True

    def colours(self):
        return [c[1] for c in self.calls if c[0] == "hsv"]


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def make_worker(*devices, **kwargs):
    queue = list(devices)

    def factory(bulb):
        if not queue:
            raise RuntimeError("no more fake devices")
        dev = queue.pop(0)
        if isinstance(dev, Exception):
            raise dev
        return dev

    kwargs.setdefault("max_rate", 1000)
    worker = BulbWorker(Bulb(name="Test", id="abc", ip="10.0.0.2", key="k"), device_factory=factory,
                        retry_delay=0.01, **kwargs)
    worker.start()
    return worker


def test_burst_of_submits_only_sends_latest():
    dev = FakeDevice(send_delay=0.2)
    worker = make_worker(dev)
    worker.submit(RED)
    assert dev.sent.wait(1)
    for colour in (GREEN, WHITE, BLUE):
        worker.submit(colour)
    assert wait_for(lambda: len(dev.colours()) == 2)
    time.sleep(0.3)
    worker.stop()
    assert dev.colours() == [RED, BLUE]
    assert worker.status == "stopped"


def test_near_identical_colours_are_skipped():
    dev = FakeDevice()
    worker = make_worker(dev)
    worker.submit(RED)
    assert wait_for(lambda: len(dev.colours()) == 1)
    worker.submit((0.0, 0.995, 0.995))
    time.sleep(0.05)
    worker.submit(BLUE)
    assert wait_for(lambda: len(dev.colours()) == 2)
    worker.stop()
    assert dev.colours() == [RED, BLUE]


def test_rate_limit_caps_sends():
    dev = FakeDevice()
    worker = make_worker(dev, max_rate=5)
    end = time.monotonic() + 1.0
    i = 0
    while time.monotonic() < end:
        worker.submit((i % 10 / 10, 1.0, 1.0))
        i += 1
        time.sleep(0.01)
    worker.stop()
    assert 2 <= len(dev.colours()) <= 7


def test_error_response_reconnects_and_resends():
    first = FakeDevice(results=[{"Error": "Network Error: Device Unreachable", "Err": "905"}])
    second = FakeDevice()
    worker = make_worker(first, second)
    worker.submit(RED)
    assert wait_for(lambda: first.closed)
    worker.submit(RED)  # same colour must be resent after reconnecting
    assert wait_for(lambda: second.colours() == [RED])
    assert worker.status == "online"
    worker.stop()


def test_connect_failure_reports_offline_then_recovers():
    dev = FakeDevice()
    worker = make_worker(RuntimeError("Unable to find device on network"), dev)
    worker.submit(GREEN)
    assert wait_for(lambda: dev.colours() == [GREEN])
    worker.stop()


def test_bulb_without_colour_is_reported():
    worker = make_worker(*(FakeDevice(colour=False) for _ in range(20)))
    assert wait_for(lambda: "colour" in worker.status)
    worker.stop()


@pytest.mark.parametrize(
    "on_stop,expected",
    [("keep", []), ("off", [("off",)]), ("white", [("white", 100, 30)])],
)
def test_stop_behaviour(on_stop, expected):
    dev = FakeDevice()
    worker = make_worker(dev)
    worker.submit(RED)
    assert wait_for(lambda: dev.colours() == [RED])
    worker.stop(on_stop)
    assert dev.calls[1:] == expected
    assert dev.closed


FLASH = [("hsv", RED), ("off",)] * 3


def test_identify_flashes_then_restores_colour():
    dev = FakeDevice()
    worker = make_worker(dev)
    worker.flash_interval = 0.01
    worker.submit(BLUE)
    assert wait_for(lambda: dev.colours() == [BLUE])
    worker.identify()
    assert wait_for(lambda: len(dev.calls) == 8)
    worker.stop()
    assert dev.calls[1:] == FLASH + [("hsv", BLUE)]


def test_identify_without_previous_colour_ends_white():
    dev = FakeDevice()
    worker = make_worker(dev)
    worker.flash_interval = 0.01
    worker.identify()
    assert wait_for(lambda: len(dev.calls) == 7)
    worker.stop()
    assert dev.calls == FLASH + [("white", 100, 30)]


def test_identify_bulb_while_stopped():
    from screenlight.bulbs import identify_bulb

    dev = FakeDevice()
    identify_bulb(Bulb(name="x", id="x"), device_factory=lambda bulb: dev, interval=0.0)
    assert dev.calls == FLASH + [("white", 100, 30)]
    assert dev.closed


def test_identify_bulb_raises_on_error():
    from screenlight.bulbs import BulbError, identify_bulb

    dev = FakeDevice(results=[{"Error": "Device Unreachable"}])
    with pytest.raises(BulbError, match="Unreachable"):
        identify_bulb(Bulb(name="x", id="x"), device_factory=lambda bulb: dev, interval=0.0)
    assert dev.closed
