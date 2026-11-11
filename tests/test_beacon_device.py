import time

import numpy as np
import pytest

from screenlight.beacon.protocol import dp_colour, dp_power
from screenlight.beacon.radio import RadioError
from screenlight.bulbs import BEACON_STATUS, BeaconDevice, BulbError, BulbWorker, make_device
from screenlight.colour import correct_for_beacon
from screenlight.config import Bulb, Settings
from screenlight.engine import SyncEngine

BULB = Bulb(name="BT", id="bt1", ip="Bluetooth", key="0123456789abcdef", kind="beacon", node_id=2,
            beacon_key="00" * 16)


class FakeRadio:
    def __init__(self, error=None):
        self.error = error
        self.payloads = []
        self.started = 0
        self.released = []
        self.closed = False

    def _maybe_fail(self):
        if self.error:
            raise RadioError(self.error)

    def start(self):
        self._maybe_fail()
        self.started += 1

    def acquire(self, bulb_id):
        self._maybe_fail()
        return 4

    def send(self, bulb, payload):
        self._maybe_fail()
        self.payloads.append(payload)

    def release(self, bulb_id):
        self.released.append(bulb_id)

    def close(self):
        self.closed = True


def test_set_hsv_sends_corrected_colour():
    radio = FakeRadio()
    d = BeaconDevice(BULB, radio)
    d.set_hsv(0.1, 0.8, 0.5)
    assert radio.payloads == [dp_power(True) + dp_colour(*correct_for_beacon((0.1, 0.8, 0.5)))]


def test_colour_after_off_prefixes_power_on():
    radio = FakeRadio()
    d = BeaconDevice(BULB, radio)
    d.turn_off()
    d.set_hsv(0, 1, 1)
    d.set_hsv(0, 1, 1)
    assert radio.payloads == [dp_power(False), dp_power(True) + dp_colour(0, 1, 1),
                              dp_power(True) + dp_colour(0, 1, 1)]


def test_warm_white_uses_white_mode_datapoints():
    radio = FakeRadio()
    BeaconDevice(BULB, radio).set_white_percentage(100, 30)
    assert radio.payloads == [bytes.fromhex("024100") + bytes.fromhex("0324000003e8") + bytes.fromhex("04240000012c")]


@pytest.mark.parametrize("code", sorted(BEACON_STATUS))
def test_status_maps_radio_errors(code):
    with pytest.raises(BulbError) as e:
        BeaconDevice(BULB, FakeRadio(error=code)).status()
    assert e.value.status == BEACON_STATUS[code]


def test_status_texts_are_the_specified_ones():
    assert BEACON_STATUS == {
        "denied": "no permission",
        "unsupported": "Bluetooth bulbs need Linux",
        "adapter_off": "Bluetooth off",
        "adapter_stuck": "Bluetooth adapter needs a reset: toggle Bluetooth off/on",
        "helper_stopped": "helper stopped",
        "no_slot": "too many Bluetooth bulbs",
    }


def test_missing_beacon_key_says_run_setup_again():
    radio = FakeRadio()
    with pytest.raises(BulbError) as e:
        BeaconDevice(Bulb(name="BT", id="bt1", key="k", kind="beacon"), radio).status()
    assert e.value.status == "run Bluetooth setup again" and radio.started == 0


def test_make_device_dispatches_on_kind():
    assert isinstance(make_device(BULB, FakeRadio()), BeaconDevice)
    with pytest.raises(BulbError):
        make_device(BULB, None)


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_worker_shows_sending_for_beacon_bulbs_and_spec_text_on_errors():
    worker = BulbWorker(BULB, max_rate=1000, device_factory=lambda b: BeaconDevice(b, FakeRadio()), retry_delay=0.01)
    worker.start()
    try:
        assert wait_for(lambda: worker.status == "connecting")
        worker.submit((0.0, 1.0, 1.0))           # "sending" only once a send has actually gone through
        assert wait_for(lambda: worker.status == "sending")
    finally:
        worker.stop()
    failing = BulbWorker(BULB, max_rate=1000, device_factory=lambda b: BeaconDevice(b, FakeRadio("denied")),
                         retry_delay=0.01)
    failing.start()
    try:
        assert wait_for(lambda: failing.status == "no permission")
    finally:
        failing.stop()


class Grabber:
    def __init__(self, monitor):
        pass

    def grab(self):
        return np.full((9, 16, 3), (255, 0, 0), dtype=np.uint8)

    def close(self):
        pass


def test_engine_borrows_radio_and_never_closes_it():
    radio = FakeRadio()
    engine = SyncEngine(Settings(bulbs=(BULB,), max_bulb_rate=50), radio=radio, grabber_factory=Grabber)
    engine.start()
    try:
        assert wait_for(lambda: radio.payloads)
    finally:
        engine.stop()
    assert not radio.closed and radio.released == ["bt1"]


def test_engine_restarts_reuse_one_helper(tmp_path):
    from test_beacon_radio import Launcher, make_radio
    launcher = Launcher(tmp_path)
    radio = make_radio(tmp_path, launcher)
    try:
        for _ in range(2):  # e.g. the user edits the bulb list while syncing
            engine = SyncEngine(Settings(bulbs=(BULB,), max_bulb_rate=50), radio=radio, grabber_factory=Grabber)
            engine.start()
            assert wait_for(lambda: any(r["op"] == "advertise" for r in launcher.requests()))
            engine.stop()
        assert launcher.launches == 1
    finally:
        radio.close()


class SendFailRadio(FakeRadio):
    """The helper is up (start/acquire fine) but every send fails, e.g. Bluetooth switched off mid-sync."""

    def __init__(self, code):
        super().__init__()
        self.send_error = code
        self.attempts = 0

    def send(self, bulb, payload):
        self.attempts += 1
        if self.send_error:
            raise RadioError(self.send_error)
        self.payloads.append(payload)


def test_send_failures_show_the_error_and_back_off_until_sending_works():
    radio = SendFailRadio("adapter_off")
    worker = BulbWorker(BULB, max_rate=1000, device_factory=lambda b: BeaconDevice(b, radio), retry_delay=0.01)
    worker.start()
    seen = set()
    try:
        end = time.monotonic() + 1.0
        i = 0
        while time.monotonic() < end:             # the engine submits ~200 colours/s here
            worker.submit((i % 2 / 2, 1.0, 1.0))
            seen.add(worker.status)
            i += 1
            time.sleep(0.005)
        assert "sending" not in seen
        assert worker.status == "Bluetooth off"
        assert radio.attempts <= 12               # backing off, not hammering the helper
        radio.send_error = None                   # Bluetooth back on
        assert wait_for(lambda: (worker.submit((0.5, 1.0, 1.0)), worker.status == "sending")[1], timeout=15)
    finally:
        worker.stop()


def test_every_colour_frame_turns_the_bulb_on():
    # A fresh device (e.g. Start after "Turn off" on Stop, or the bulb switched off from the phone) must still
    # wake the bulb: with no acknowledgements, every colour command has to stand on its own.
    radio = FakeRadio()
    d = BeaconDevice(BULB, radio)
    d.set_hsv(0, 1, 1)
    d.set_hsv(2 / 3, 1, 1)
    assert radio.payloads == [dp_power(True) + dp_colour(*correct_for_beacon((0, 1, 1))),
                              dp_power(True) + dp_colour(*correct_for_beacon((2 / 3, 1, 1)))]
