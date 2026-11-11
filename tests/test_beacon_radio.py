import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from screenlight.beacon.protocol import build_frame, dp_colour, dp_power, parse_frame
from screenlight.beacon.radio import BeaconRadio, RadioError, SeqState

FAKE = Path(__file__).with_name("fake_helper.py")
LK, BK = "0123456789abcdef", bytes(range(16))


def bulb(bulb_id="b1", node_id=2):
    return SimpleNamespace(id=bulb_id, key=LK, node_id=node_id, beacon_key=BK.hex())


class Launcher:
    def __init__(self, tmp_path, **env):
        self.env = {**os.environ, "FAKE_LOG": str(tmp_path / "requests.jsonl"),
                    **{f"FAKE_{k.upper()}": v for k, v in env.items()}}
        self.log = tmp_path / "requests.jsonl"
        self.launches = 0

    def __call__(self):
        self.launches += 1
        return subprocess.Popen([sys.executable, str(FAKE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, bufsize=1, env=self.env)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def make_radio(tmp_path, launcher, **kwargs):
    radio = BeaconRadio(tmp_path / "beacon_state.json", launcher=launcher, supported=lambda: True, **kwargs)
    return radio


@pytest.fixture
def closing():
    radios = []
    yield radios.append
    for r in radios:
        r.close()


def test_seq_state_reserves_before_use_and_survives_restart(tmp_path):
    p = tmp_path / "beacon_state.json"
    s = SeqState(p)
    src, first = s.next()
    assert 0x4000 <= src <= 0x7EFF
    assert json.loads(p.read_text())["reserved"] >= first + 1      # persisted before it was returned
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert SeqState(p).next()[1] > first                           # a "crash" never reuses a sequence
    assert SeqState(p).next()[0] == src


def test_seq_state_rotates_source_before_0xff00(tmp_path):
    p = tmp_path / "beacon_state.json"
    p.write_text(json.dumps({"source_id": 0x4ABC, "reserved": 0xFF00}))
    src, seq = SeqState(p).next()
    assert src != 0x4ABC and 0x4000 <= src <= 0x7EFF and seq == 1


def test_seq_state_recovers_from_corrupt_file(tmp_path):
    p = tmp_path / "beacon_state.json"
    p.write_text("{nope")
    src, seq = SeqState(p).next()
    assert 0x4000 <= src <= 0x7EFF and seq >= 1


def test_unsupported_platform(tmp_path):
    radio = BeaconRadio(tmp_path / "s.json", launcher=Launcher(tmp_path), supported=lambda: False)
    with pytest.raises(RadioError) as e:
        radio.start()
    assert e.value.code == "unsupported"


def test_denied_is_remembered_until_reset(tmp_path, closing):
    launcher = Launcher(tmp_path, mode="exit126")
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    for _ in range(2):
        with pytest.raises(RadioError) as e:
            radio.start()
        assert e.value.code == "denied"
    assert launcher.launches == 1                                   # no second password prompt
    radio.reset_denied()
    with pytest.raises(RadioError):
        radio.start()
    assert launcher.launches == 2


def test_slots_highest_first_skip_active_and_keep_one_free(tmp_path, closing):
    radio = make_radio(tmp_path, Launcher(tmp_path, active="4"))
    closing(radio)
    assert radio.acquire("a") == 3
    assert radio.acquire("b") == 2
    assert radio.acquire("a") == 3                                  # idempotent
    with pytest.raises(RadioError) as e:
        radio.acquire("c")
    assert e.value.code == "no_slot"


def test_send_builds_advert_with_next_sequence(tmp_path, closing):
    launcher = Launcher(tmp_path)
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    radio.send(bulb(), dp_colour(2 / 3, 1, 1))
    radio.send(bulb(), dp_power(False))
    adverts = [r for r in launcher.requests() if r["op"] == "advertise"]
    assert [a["slot"] for a in adverts] == [4, 4] and adverts[0]["interval_ms"] == 20
    frames = [parse_frame(bytes.fromhex(a["adv"])[5:], LK, BK) for a in adverts]
    assert frames[0].node_id == 2 and frames[0].datapoints == {11: bytes((0, 240, 100, 100))}
    assert frames[1].seq == frames[0].seq + 1 and frames[1].source_id == frames[0].source_id
    assert bytes.fromhex(adverts[0]["adv"])[:5] == bytes.fromhex("0201061b03")


def test_idle_stop_after_quiet_period(tmp_path, closing):
    launcher = Launcher(tmp_path)
    radio = make_radio(tmp_path, launcher, idle_stop=0.05)
    closing(radio)
    radio.send(bulb(), dp_power(True))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(r["op"] == "stop" for r in launcher.requests()):
        time.sleep(0.02)
    assert {"op": "stop", "slot": 4} in launcher.requests()


def test_helper_crash_restarts_once_then_stays_stopped(tmp_path, closing):
    launcher = Launcher(tmp_path, mode="crash_after_hello")
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    for expected_launches in (1, 2, 2):
        with pytest.raises(RadioError) as e:
            radio.send(bulb(), dp_power(True))
        assert e.value.code == "helper_stopped"
        assert launcher.launches == expected_launches


def test_cancelled_prompt_does_not_use_up_the_crash_restart(tmp_path, closing):
    launcher = Launcher(tmp_path, mode="exit126")
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    with pytest.raises(RadioError):
        radio.start()
    radio.reset_denied()
    launcher.env["FAKE_MODE"] = "crash_after_hello"
    for expected_launches in (2, 3, 3):
        with pytest.raises(RadioError) as e:
            radio.send(bulb(), dp_power(True))
        assert e.value.code == "helper_stopped"
        assert launcher.launches == expected_launches


def test_adapter_off_error_propagates_without_relaunch(tmp_path, closing):
    launcher = Launcher(tmp_path, mode="adapter_off")
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    for _ in range(2):
        with pytest.raises(RadioError) as e:
            radio.send(bulb(), dp_power(True))
        assert e.value.code == "adapter_off"
    assert launcher.launches == 1


def test_capture_streams_frames(tmp_path, closing):
    frames = [build_frame(0x7A09, 2, n, dp_power(n % 2 == 0), LK, BK) for n in (1, 2)]
    radio = make_radio(tmp_path, Launcher(tmp_path, frames=",".join(f.hex() for f in frames)))
    closing(radio)
    got = []
    radio.capture(1.0, got.append)
    assert got == frames


def test_release_stops_the_slot(tmp_path, closing):
    launcher = Launcher(tmp_path)
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    radio.send(bulb(), dp_power(True))
    radio.release("b1")
    assert launcher.requests()[-1] == {"op": "stop", "slot": 4}
    assert radio.acquire("other") == 4                              # slot is free again


def test_close_does_not_wait_for_a_running_capture(tmp_path):
    import threading
    radio = make_radio(tmp_path, Launcher(tmp_path, mode="slow_capture"))
    errors = []

    def listen():
        try:
            radio.capture(30, lambda f: None)
        except RadioError as e:
            errors.append(e.code)

    t = threading.Thread(target=listen)
    t.start()
    time.sleep(0.5)                      # capture is now holding the radio
    started = time.monotonic()
    radio.close()                        # e.g. the user closes the main window
    assert time.monotonic() - started < 1.0
    t.join(5)
    assert not t.is_alive() and errors == ["helper_stopped"]
    with pytest.raises(RadioError):
        radio.start()                    # a closed radio never relaunches the helper


def test_kill_survives_a_root_helper_that_cannot_be_signalled(tmp_path):
    class RootProc:
        def __init__(self):
            self.stdin = open(os.devnull, "w")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("helper", timeout)

        def terminate(self):
            raise PermissionError(1, "Operation not permitted")

        def poll(self):
            return None

    radio = make_radio(tmp_path, Launcher(tmp_path))
    radio._proc = RootProc()
    radio._kill()                        # must not raise
    assert radio._proc is None


def test_release_lets_the_last_command_be_heard_first(tmp_path, closing):
    launcher = Launcher(tmp_path)
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    radio.send(bulb(), dp_power(False))           # e.g. "Turn off" when sync stops
    started = time.monotonic()
    radio.release("b1")
    assert time.monotonic() - started >= 0.28     # the advert stays up long enough for the bulb to hear it
    assert launcher.requests()[-1] == {"op": "stop", "slot": 4}


def test_release_of_an_old_advert_does_not_wait(tmp_path, closing):
    radio = make_radio(tmp_path, Launcher(tmp_path))
    closing(radio)
    radio.send(bulb(), dp_power(False))
    time.sleep(0.35)
    started = time.monotonic()
    radio.release("b1")
    assert time.monotonic() - started < 0.1


def test_reset_denied_never_waits_for_a_password_prompt(tmp_path):
    import threading
    radio = make_radio(tmp_path, Launcher(tmp_path, mode="slow_ready"))
    t = threading.Thread(target=radio.start)
    t.start()
    time.sleep(0.3)                               # start() now holds the radio while the prompt is open
    started = time.monotonic()
    radio.reset_denied()                          # the GUI thread pressing Start or Flash
    assert time.monotonic() - started < 0.2
    t.join(5)
    radio.close()


def test_slot_taken_after_hello_moves_to_another_slot(tmp_path, closing):
    launcher = Launcher(tmp_path, busy="4")
    radio = make_radio(tmp_path, launcher)
    closing(radio)
    radio.send(bulb(), dp_power(True))
    radio.send(bulb(), dp_power(False))
    slots = [r["slot"] for r in launcher.requests() if r["op"] == "advertise"]
    assert slots == [4, 3, 3]


def test_reply_timeout_covers_the_helpers_worst_case():
    from screenlight.beacon.helper import COMMAND_TIMEOUT
    from screenlight.beacon.radio import REPLY_TIMEOUT
    assert REPLY_TIMEOUT > 7 * COMMAND_TIMEOUT    # one advertise can issue up to 7 management commands
