import struct

from screenlight.beacon.helper import (
    ADD_EXT_ADV_DATA,
    ADD_EXT_ADV_PARAMS,
    DEVICE_FOUND,
    DISCOVERING,
    READ_ADV_FEATURES,
    REMOVE_ADV,
    START_DISCOVERY,
    STOP_DISCOVERY,
    Helper,
    MgmtError,
    encode_command,
    ext_adv_data,
    ext_adv_params,
    p2_frames_from_device_found,
    parse_event,
)

ADV = bytes.fromhex("0201061b03") + bytes([0x0B]) + bytes(25)
ADV2 = bytes.fromhex("0201061b03") + bytes([0x0B]) + bytes(24) + b"\x01"
FRAME = bytes([0x0B]) + bytes(range(1, 26))


class FakeMgmt:
    def __init__(self, active=(), instances=4):
        self.active = list(active)
        self.instances = instances
        self.calls = []
        self.failures = []  # [op, status, remaining]
        self.events = []

    def fail_next(self, op, status, times=1):
        self.failures.append([op, status, times])

    def command(self, op, params=b""):
        self.calls.append((op, params))
        for f in self.failures:
            if f[0] == op and f[2] > 0:
                f[2] -= 1
                raise MgmtError(op, f[1])
        if op == READ_ADV_FEATURES:
            return struct.pack("<IBBBB", 0, 31, 31, self.instances, len(self.active)) + bytes(self.active)
        if op == ADD_EXT_ADV_PARAMS:
            return bytes((params[0], 0, 31, 31))
        return params[:1]

    def read_event(self, timeout):
        return self.events.pop(0) if self.events else None

    def ops(self):
        return [op for op, _ in self.calls]


def advertise(h, adv=ADV, slot=4):
    return h.handle({"op": "advertise", "slot": slot, "adv": adv.hex(), "interval_ms": 20})


def test_ext_adv_params_exact_bytes():
    assert ext_adv_params(4, 20, 15).hex() == "040160000000000f00200000002000000000"


def test_ext_adv_data_layout():
    assert ext_adv_data(4, ADV) == bytes((4, 31, 0)) + ADV


def test_encode_command_and_parse_event():
    assert encode_command(0x0054, 0, b"\x01\x02") == bytes.fromhex("540000000200") + b"\x01\x02"
    assert parse_event(bytes.fromhex("010000000300540000")) == (0x0001, 0, bytes.fromhex("540000"))
    assert parse_event(bytes.fromhex("0100000003005400")) is None   # truncated payload
    assert parse_event(b"\x01") is None


def test_hello_reports_adapter():
    h = Helper(FakeMgmt(active=[1]))
    assert h.handle({"op": "hello"}) == {"ok": True, "instances": 4, "active": [1], "max_len": 31}


def test_first_advertise_creates_then_updates_in_place():
    m = FakeMgmt()
    h = Helper(m)
    assert advertise(h) == {"ok": True}
    assert advertise(h, ADV2) == {"ok": True}
    assert m.ops() == [READ_ADV_FEATURES, ADD_EXT_ADV_PARAMS, ADD_EXT_ADV_DATA, ADD_EXT_ADV_DATA]
    assert m.calls[1][1] == ext_adv_params(4, 20, 15)
    assert m.calls[3][1] == ext_adv_data(4, ADV2)


def test_refuses_slot_active_elsewhere():
    m = FakeMgmt(active=[4])
    assert advertise(Helper(m))["error"] == "slot_busy"
    assert ADD_EXT_ADV_PARAMS not in m.ops()


def test_failed_update_recreates_once_then_reports_stuck():
    m = FakeMgmt()
    h = Helper(m)
    advertise(h)
    m.fail_next(ADD_EXT_ADV_DATA, status=0x03)
    assert advertise(h, ADV2) == {"ok": True}
    assert m.ops()[-3:] == [REMOVE_ADV, ADD_EXT_ADV_PARAMS, ADD_EXT_ADV_DATA]
    m.fail_next(ADD_EXT_ADV_DATA, status=0x03, times=2)
    assert advertise(h)["error"] == "adapter_stuck"


def test_not_powered_maps_to_adapter_off():
    m = FakeMgmt()
    m.fail_next(ADD_EXT_ADV_PARAMS, status=0x0F, times=2)
    assert advertise(Helper(m))["error"] == "adapter_off"


def test_rejected_params_mean_adapter_off():
    # The kernel answers Add Ext Adv Params with REJECTED (0x0b) when a timeout is set and the adapter is
    # powered off (seen on real hardware with Bluetooth soft-blocked).
    m = FakeMgmt()
    m.fail_next(ADD_EXT_ADV_PARAMS, status=0x0B, times=2)
    assert advertise(Helper(m))["error"] == "adapter_off"


def test_rearms_after_10_seconds():
    now = [0.0]
    m = FakeMgmt()
    h = Helper(m, clock=lambda: now[0])
    advertise(h)
    now[0] = 11.0
    advertise(h, ADV2)
    assert m.ops()[-3:] == [REMOVE_ADV, ADD_EXT_ADV_PARAMS, ADD_EXT_ADV_DATA]


def test_stop_removes_slot_and_cleanup_removes_only_own_slots():
    m = FakeMgmt(active=[1])
    h = Helper(m)
    advertise(h, slot=4)
    advertise(h, slot=3)
    assert h.handle({"op": "stop", "slot": 3}) == {"ok": True}
    h.cleanup()
    removed = [p[0] for op, p in m.calls if op == REMOVE_ADV]
    assert removed == [3, 4]


def test_socket_error_becomes_mgmt_error_instead_of_crashing():
    # Kernel 7.0 reports refused management commands as ENOMEM on send.
    m = FakeMgmt()

    def refuse(op, params=b""):
        raise OSError(12, "Cannot allocate memory")

    m.command = refuse
    assert Helper(m).handle({"op": "hello"})["error"] == "mgmt_error"


def test_bad_request():
    h = Helper(FakeMgmt())
    assert h.handle({"op": "nope"})["error"] == "bad_request"
    assert h.handle({"op": "advertise", "slot": 4, "adv": "zz", "interval_ms": 20})["error"] == "bad_request"


def _device_found(eir: bytes) -> bytes:
    return bytes(6) + bytes([1]) + b"\xc0" + bytes(4) + len(eir).to_bytes(2, "little") + eir


def test_p2_frames_from_device_found():
    eir = bytes.fromhex("020106") + bytes([27, 0x03]) + FRAME + bytes.fromhex("03031234")
    assert p2_frames_from_device_found(_device_found(eir)) == [FRAME]
    assert p2_frames_from_device_found(_device_found(bytes.fromhex("0303aabb"))) == []


def test_capture_emits_unique_frames_and_restarts_discovery():
    now = [0.0]
    m = FakeMgmt()
    emitted = []
    h = Helper(m, clock=lambda: now[0], emit=emitted.append)
    eir = bytes.fromhex("020106") + bytes([27, 0x03]) + FRAME

    def tick(timeout):
        now[0] += 0.5
        return m.events.pop(0) if m.events else None

    m.read_event = tick
    m.events = [(DEVICE_FOUND, 0, _device_found(eir)), (DEVICE_FOUND, 0, _device_found(eir)),
                (DISCOVERING, 0, bytes((0x06, 0)))]
    assert h.handle({"op": "capture", "seconds": 3}) == {"ok": True}
    assert emitted == [{"event": "frame", "frame": FRAME.hex()}]
    assert m.ops().count(START_DISCOVERY) == 2 and m.ops()[-1] == STOP_DISCOVERY


def test_socket_refusal_becomes_a_mgmt_error_so_retry_and_cleanup_run():
    from screenlight.beacon.helper import MgmtSocket

    class RefusingSocket:
        def sendall(self, data):
            raise OSError(12, "Cannot allocate memory")   # how kernel 7.0 refuses a management command

    m = MgmtSocket.__new__(MgmtSocket)
    m.sock, m.pending = RefusingSocket(), []
    try:
        m.command(ADD_EXT_ADV_DATA, b"")
    except MgmtError as e:
        assert e.op == ADD_EXT_ADV_DATA
    else:
        raise AssertionError("expected MgmtError")


def test_refused_data_after_params_is_cleaned_up():
    m = FakeMgmt()
    m.fail_next(ADD_EXT_ADV_DATA, status=0xFE, times=2)   # 0xFE: refused at the socket
    assert advertise(Helper(m))["error"] == "adapter_stuck"
    assert m.ops()[-1] == REMOVE_ADV                     # nothing half-created is left behind
