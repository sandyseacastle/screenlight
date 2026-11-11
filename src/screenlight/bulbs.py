"""One background thread per Tuya bulb, always sending the most recent colour it was given.

Each send waits for the bulb's acknowledgement, so a slow bulb simply skips intermediate
colours instead of building up a backlog, and an offline bulb never stalls the others.
"""

import colorsys
import threading
import time

from screenlight.config import Bulb

HSV = tuple[float, float, float]

_SAME_COLOUR = 0.015  # max RGB channel difference treated as "no change"
_MAX_RETRY_DELAY = 10.0
_WARM_WHITE = (100, 30)  # brightness %, colour temperature %


class BulbError(Exception):
    """status, when set, is shown as the bulb's status verbatim (instead of "offline: <message>")."""

    def __init__(self, message: str, status: str | None = None):
        super().__init__(message)
        self.status = status


BEACON_STATUS = {
    "denied": "no permission",
    "unsupported": "Bluetooth bulbs need Linux",
    "adapter_off": "Bluetooth off",
    "adapter_stuck": "Bluetooth adapter needs a reset: toggle Bluetooth off/on",
    "helper_stopped": "helper stopped",
    "no_slot": "too many Bluetooth bulbs",
}
_WHITE_MODE = bytes.fromhex("024100")  # DP 2 work_mode = white (verified on hardware 2026-11-11)


def _status_error(text: str) -> BulbError:
    return BulbError(text, status=text)


class BeaconDevice:
    """A Tuya Bluetooth beacon bulb behind the shared BeaconRadio. One-way: sends never get acknowledged."""

    online_label = "sending"

    def __init__(self, bulb: Bulb, radio):
        self.bulb = bulb
        self.radio = radio
        self._off = False

    def _radio(self, fn, *args):
        from screenlight.beacon.radio import RadioError

        try:
            return fn(*args)
        except RadioError as e:
            raise _status_error(BEACON_STATUS.get(e.code, str(e))) from e

    def _send(self, payload: bytes) -> dict:
        self._radio(self.radio.send, self.bulb, payload)
        return {}

    def status(self) -> dict:
        if not self.bulb.beacon_key:
            raise _status_error("run Bluetooth setup again")
        self._radio(self.radio.start)
        self._radio(self.radio.acquire, self.bulb.id)
        return {}

    def bulb_has_capability(self, feature: str) -> bool:
        return True

    def set_hsv(self, h: float, s: float, v: float, nowait: bool = False) -> dict:
        from screenlight.beacon.protocol import dp_colour, dp_power
        from screenlight.colour import correct_for_beacon

        # Always include power-on: there are no acknowledgements, so every frame must stand on its own (the bulb
        # may have been switched off by "Turn off" at a previous Stop, or from the phone).
        self._send(dp_power(True) + dp_colour(*correct_for_beacon((h, s, v))))
        self._off = False
        return {}

    def turn_on(self, nowait: bool = False) -> dict:
        from screenlight.beacon.protocol import dp_power

        self._off = False
        return self._send(dp_power(True))

    def turn_off(self, nowait: bool = False) -> dict:
        from screenlight.beacon.protocol import dp_power

        self._off = True
        return self._send(dp_power(False))

    def set_white_percentage(self, brightness: int = 100, colourtemp: int = 0, nowait: bool = False) -> dict:
        if self._off:
            self.turn_on()
            time.sleep(0.2)  # let the power-on advert be heard before it's replaced
        bright = max(10, min(1000, round(brightness * 10))).to_bytes(4, "big")
        temp = max(0, min(1000, round(colourtemp * 10))).to_bytes(4, "big")
        return self._send(_WHITE_MODE + b"\x03\x24" + bright + b"\x04\x24" + temp)

    def close(self) -> None:
        try:
            self.radio.release(self.bulb.id)
        except Exception:
            pass


def make_device(bulb: Bulb, radio=None):
    if not bulb.is_beacon:
        return tinytuya_device(bulb)
    if radio is None:
        raise _status_error(BEACON_STATUS["unsupported"])
    return BeaconDevice(bulb, radio)


def tinytuya_device(bulb: Bulb):
    import tinytuya

    return tinytuya.BulbDevice(
        bulb.id,
        bulb.ip or "Auto",
        bulb.key,
        version=bulb.version,
        persist=True,
        connection_timeout=2,
        connection_retry_limit=1,
        connection_retry_delay=1,
    )


def _check(result):
    # tinytuya reports failures as {"Error": ..., "Err": ...} instead of raising.
    if isinstance(result, dict) and "Error" in result:
        raise BulbError(result["Error"])
    return result


def _offline(e: Exception) -> str:
    return getattr(e, "status", None) or f"offline: {e}"


def _close_enough(a: HSV, b: HSV) -> bool:
    rgb_a, rgb_b = colorsys.hsv_to_rgb(*a), colorsys.hsv_to_rgb(*b)
    return max(abs(x - y) for x, y in zip(rgb_a, rgb_b)) < _SAME_COLOUR


def _flash_device(dev, restore: HSV | None, interval: float, sleep) -> None:
    for _ in range(3):
        _check(dev.set_hsv(0.0, 1.0, 1.0))
        sleep(interval)
        _check(dev.turn_off())
        sleep(interval)
    if restore is not None:
        _check(dev.set_hsv(*restore))
    else:
        _check(dev.set_white_percentage(*_WARM_WHITE))


def identify_bulb(bulb: Bulb, device_factory=tinytuya_device, interval: float = 0.4) -> None:
    """Flash a bulb while sync is not running (blocking; leaves it warm white). Raises on failure."""
    dev = device_factory(bulb)
    try:
        _check(dev.status())
        _flash_device(dev, None, interval, time.sleep)
    finally:
        dev.close()


class BulbWorker:
    flash_interval = 0.4

    def __init__(self, bulb: Bulb, max_rate: float = 8.0, device_factory=tinytuya_device,
                 retry_delay: float = 1.0):
        self.bulb = bulb
        self.max_rate = max_rate
        self.status = "connecting"
        self._factory = device_factory
        self._retry_delay = retry_delay
        self._cond = threading.Condition()
        self._pending: HSV | None = None
        self._identify = False
        self._stopping = False
        self._on_stop = "keep"
        self._dev = None
        self._last_sent: HSV | None = None
        self._next_send = 0.0
        self._failing = False  # the last send failed; keep showing its error until one succeeds
        self._thread = threading.Thread(target=self._run, name=f"bulb-{bulb.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, hsv: HSV) -> None:
        with self._cond:
            self._pending = hsv
            self._cond.notify()

    def identify(self) -> None:
        """Flash red three times so the user can tell which physical bulb this is."""
        with self._cond:
            self._identify = True
            self._cond.notify()

    def stop(self, on_stop: str = "keep", timeout: float = 5.0) -> None:
        with self._cond:
            self._on_stop = on_stop
            self._stopping = True
            self._cond.notify()
        self._thread.join(timeout)

    def _sleep(self, seconds: float) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._stopping, timeout=seconds)

    def _run(self) -> None:
        delay = self._retry_delay
        while not self._stopping:
            if self._dev is None:
                try:
                    self._connect()
                except Exception as e:
                    self._disconnect(_offline(e))
                    self._sleep(delay)
                    delay = min(delay * 2, _MAX_RETRY_DELAY)
                    continue
            self._sleep(self._next_send - time.monotonic())
            job = self._take()
            if job is None:
                continue
            try:
                job()
            except Exception as e:
                # Back off like a failed connect: a one-way bulb "reconnects" instantly, so without this a
                # failing send would be retried at the engine's frame rate.
                self._failing = True
                self._disconnect(_offline(e))
                self._sleep(delay)
                delay = min(delay * 2, _MAX_RETRY_DELAY)
                continue
            delay = self._retry_delay
            self._failing = False
            label = getattr(self._dev, "online_label", None)
            if label:
                self.status = label  # one-way devices are only "sending" once a send has gone through
        self._shutdown()

    def _take(self):
        with self._cond:
            self._cond.wait_for(lambda: self._stopping or self._identify or self._pending is not None)
            if self._stopping:
                return None
            if self._identify:
                self._identify = False
                return self._flash
            hsv, self._pending = self._pending, None
        return lambda: self._send(hsv)

    def _connect(self) -> None:
        if not self._failing:
            self.status = "connecting"
        self._dev = self._factory(self.bulb)
        _check(self._dev.status())
        if not self._dev.bulb_has_capability("colour"):
            raise BulbError("bulb has no colour mode")
        self._last_sent = None
        if getattr(self._dev, "online_label", None) is None:
            self.status = "online"  # two-way devices just answered status()

    def _disconnect(self, status: str) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None
        self._last_sent = None
        self.status = status

    def _send(self, hsv: HSV) -> None:
        if self._last_sent is not None and _close_enough(hsv, self._last_sent):
            return
        started = time.monotonic()
        _check(self._dev.set_hsv(*hsv))
        self._last_sent = hsv
        self._next_send = started + 1.0 / max(self.max_rate, 0.1)

    def _flash(self) -> None:
        _flash_device(self._dev, self._last_sent, self.flash_interval, self._sleep)

    def _shutdown(self) -> None:
        if self._dev is not None:
            try:
                if self._on_stop == "off":
                    self._dev.turn_off()
                elif self._on_stop == "white":
                    self._dev.set_white_percentage(*_WARM_WHITE)
            except Exception:
                pass
        self._disconnect("stopped")
