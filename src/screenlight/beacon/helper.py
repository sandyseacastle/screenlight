"""Root helper for Bluetooth beacon bulbs. Standard library only; must not import screenlight.

Run by the app as: pkexec /usr/bin/python3 -I helper.py
It speaks JSON lines on stdin/stdout:
  -> {"event": "ready"}                                      once, on start
  {"op": "hello"}                                            -> {"ok": true, "instances", "active", "max_len"}
  {"op": "advertise", "slot", "adv": hex, "interval_ms"}     -> {"ok": true}   (create, or update in place)
  {"op": "stop", "slot"}                                     -> {"ok": true}
  {"op": "capture", "seconds"}                               -> {"event": "frame", "frame": hex}* then {"ok": true}
Errors: {"ok": false, "error": adapter_off|adapter_stuck|slot_busy|bad_request|mgmt_error, "detail"}.

It only ever handles finished advertisements, never keys. On stdin EOF or SIGTERM it removes every
advertising instance it created and exits. It never touches instances it did not create or global settings.
"""

import collections
import json
import signal
import socket
import struct
import sys
import time

INDEX = 0  # hci0
START_DISCOVERY, STOP_DISCOVERY = 0x0023, 0x0024
READ_ADV_FEATURES, REMOVE_ADV = 0x003D, 0x003F
ADD_EXT_ADV_PARAMS, ADD_EXT_ADV_DATA = 0x0054, 0x0055
CMD_COMPLETE, CMD_STATUS, DEVICE_FOUND, DISCOVERING = 0x0001, 0x0002, 0x0012, 0x0013
_FLAG_CONNECTABLE, _PARAM_TIMEOUT, _PARAM_INTERVAL = 1 << 0, 1 << 13, 1 << 14
_STATUS_REJECTED, _STATUS_NOT_POWERED = 0x0B, 0x0F
_STATUS_REFUSED = 0xFE  # not a kernel status: the send itself was refused
_LE_ONLY = 0x06  # discovery address types: LE public + LE random

ADV_TIMEOUT_S = 15  # kernel removes a forgotten advert after this
COMMAND_TIMEOUT = 5  # seconds to wait for one management command's reply
REARM_AFTER_S = 10  # re-create an advert this old so the kernel timeout never fires mid-use
FRAME_LEN = 26


class MgmtError(Exception):
    def __init__(self, op: int, status: int):
        super().__init__(f"mgmt op {op:#06x} failed with status {status:#04x}")
        self.op = op
        self.status = status


def encode_command(op: int, index: int, params: bytes) -> bytes:
    return struct.pack("<HHH", op, index, len(params)) + params


def parse_event(packet: bytes) -> tuple[int, int, bytes] | None:
    if len(packet) < 6:
        return None
    code, index, length = struct.unpack_from("<HHH", packet)
    payload = packet[6:6 + length]
    return (code, index, payload) if len(payload) == length else None


def ext_adv_params(instance: int, interval_ms: int, timeout_s: int) -> bytes:
    """struct mgmt_cp_add_ext_adv_params (18 bytes); a legacy connectable PDU at a fixed interval."""
    slots = round(interval_ms / 0.625)
    flags = _FLAG_CONNECTABLE | _PARAM_TIMEOUT | _PARAM_INTERVAL
    return struct.pack("<BIHHIIb", instance, flags, 0, timeout_s, slots, slots, 0)


def ext_adv_data(instance: int, adv: bytes) -> bytes:
    return bytes((instance, len(adv), 0)) + adv


def p2_frames_from_device_found(params: bytes) -> list[bytes]:
    """P2 beacon frames (AD type 0x03, 26 bytes, type byte 0x0b) in a Device Found event."""
    if len(params) < 14:
        return []
    eir_len = struct.unpack_from("<H", params, 12)[0]
    eir, frames, i = params[14:14 + eir_len], [], 0
    while i < len(eir) and eir[i]:
        length = eir[i]
        ad_type, value = eir[i + 1:i + 2], eir[i + 2:i + 1 + length]
        if ad_type == b"\x03" and len(value) == FRAME_LEN and value[0] == 0x0B:
            frames.append(bytes(value))
        i += 1 + length
    return frames


class MgmtSocket:
    """Kernel Bluetooth Management API over the HCI control channel (needs root)."""

    def __init__(self):
        import ctypes
        import ctypes.util
        import os

        self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
        # CPython < 3.13 can't express HCI channels in bind(), so build struct sockaddr_hci ourselves.
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        addr = struct.pack("<HHH", socket.AF_BLUETOOTH, 0xFFFF, 3)  # family, HCI_DEV_NONE, HCI_CHANNEL_CONTROL
        if libc.bind(self.sock.fileno(), ctypes.create_string_buffer(addr, len(addr)), len(addr)):
            err = ctypes.get_errno()
            raise OSError(err, "bind HCI control channel: " + os.strerror(err))
        self.pending = collections.deque(maxlen=1000)

    def _recv(self, timeout: float):
        self.sock.settimeout(max(timeout, 0.001))
        try:
            return parse_event(self.sock.recv(4096))
        except socket.timeout:
            return None

    def command(self, op: int, params: bytes = b"") -> bytes:
        try:
            self.sock.sendall(encode_command(op, INDEX, params))
        except OSError as e:
            # Kernel 7.0 refuses some management commands with ENOMEM on send. Treat that like a failed
            # status so the retry and cleanup paths run.
            raise MgmtError(op, _STATUS_REFUSED) from e
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while time.monotonic() < deadline:
            event = self._recv(deadline - time.monotonic())
            if event is None:
                continue
            code, index, payload = event
            if code in (CMD_COMPLETE, CMD_STATUS) and index == INDEX and len(payload) >= 3 \
                    and struct.unpack_from("<H", payload)[0] == op:
                if payload[2]:
                    raise MgmtError(op, payload[2])
                return payload[3:]
            self.pending.append(event)
        raise MgmtError(op, 0xFF)

    def read_event(self, timeout: float):
        return self.pending.popleft() if self.pending else self._recv(timeout)


class Helper:
    def __init__(self, mgmt, clock=time.monotonic, emit=None):
        self.mgmt = mgmt
        self.clock = clock
        self.emit = emit or (lambda message: None)
        self.slots: dict[int, float] = {}  # instance -> created at

    # ---- requests ----------------------------------------------------------

    def handle(self, request: dict) -> dict:
        try:
            op = request.get("op")
            if op == "hello":
                return self._hello()
            if op == "advertise":
                return self._advertise(int(request["slot"]), bytes.fromhex(request["adv"]),
                                       int(request["interval_ms"]))
            if op == "stop":
                self._remove(int(request["slot"]))
                return {"ok": True}
            if op == "capture":
                return self._capture(float(request["seconds"]))
            return _error("bad_request", f"unknown op {op!r}")
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            return _error("bad_request", str(e))
        except MgmtError as e:
            return _error("adapter_off" if e.status == _STATUS_NOT_POWERED else "mgmt_error", str(e))
        except OSError as e:  # kernel 7.0 reports refused management commands as ENOMEM on send
            return _error("mgmt_error", str(e))

    def _features(self):
        data = self.mgmt.command(READ_ADV_FEATURES)
        _flags, max_len, _scan, instances, count = struct.unpack_from("<IBBBB", data)
        return max_len, instances, list(data[8:8 + count])

    def _hello(self) -> dict:
        max_len, instances, active = self._features()
        return {"ok": True, "instances": instances, "active": active, "max_len": max_len}

    def _advertise(self, slot: int, adv: bytes, interval_ms: int) -> dict:
        if not 1 <= len(adv) <= 31 or not 1 <= slot <= 0xFF:
            return _error("bad_request", "bad slot or advert length")
        if slot not in self.slots:
            if slot in self._features()[2]:
                return _error("slot_busy", f"advertising instance {slot} is in use by another program")
        elif self.clock() - self.slots[slot] < REARM_AFTER_S:
            try:
                self.mgmt.command(ADD_EXT_ADV_DATA, ext_adv_data(slot, adv))
                return {"ok": True}
            except MgmtError:
                # e.g. Bluetooth toggled or resumed from suspend: re-create once, then give up.
                return self._create_with_retry(slot, adv, interval_ms, attempts=1)
        return self._create_with_retry(slot, adv, interval_ms, attempts=2)

    def _create_with_retry(self, slot: int, adv: bytes, interval_ms: int, attempts: int) -> dict:
        error = None
        for attempt in range(attempts):
            # A failed attempt can leave a half-created instance behind, so always clear it before retrying.
            self._remove(slot, force=attempt > 0)
            try:
                self.mgmt.command(ADD_EXT_ADV_PARAMS, ext_adv_params(slot, interval_ms, ADV_TIMEOUT_S))
                self.mgmt.command(ADD_EXT_ADV_DATA, ext_adv_data(slot, adv))
                self.slots[slot] = self.clock()
                return {"ok": True}
            except MgmtError as e:
                error = e
        self._remove(slot, force=True)
        # Add Ext Adv Params with a timeout is REJECTED (rather than NOT_POWERED) while the adapter is off.
        if error.status == _STATUS_NOT_POWERED or (error.op == ADD_EXT_ADV_PARAMS and error.status == _STATUS_REJECTED):
            return _error("adapter_off", str(error))
        return _error("adapter_stuck", str(error))

    def _remove(self, slot: int, force: bool = False) -> None:
        """Remove an instance this helper created (or, with force, one a failed create may have left)."""
        if slot in self.slots or force:
            try:
                self.mgmt.command(REMOVE_ADV, bytes((slot,)))
            except MgmtError:
                pass
        self.slots.pop(slot, None)

    def _capture(self, seconds: float) -> dict:
        seen = set()
        self.mgmt.command(START_DISCOVERY, bytes((_LE_ONLY,)))
        deadline = self.clock() + seconds
        try:
            while self.clock() < deadline:
                event = self.mgmt.read_event(deadline - self.clock())
                if event is None:
                    continue
                code, _index, payload = event
                if code == DEVICE_FOUND:
                    for frame in p2_frames_from_device_found(payload):
                        if frame not in seen:
                            seen.add(frame)
                            self.emit({"event": "frame", "frame": frame.hex()})
                elif code == DISCOVERING and len(payload) >= 2 and payload[1] == 0:
                    try:  # the kernel ends LE discovery after ~10 s; keep listening
                        self.mgmt.command(START_DISCOVERY, bytes((_LE_ONLY,)))
                    except MgmtError:
                        pass
        finally:
            try:
                self.mgmt.command(STOP_DISCOVERY, bytes((_LE_ONLY,)))
            except MgmtError:
                pass
        return {"ok": True}

    def cleanup(self) -> None:
        for slot in list(self.slots):
            self._remove(slot)


def _error(code: str, detail: str) -> dict:
    return {"ok": False, "error": code, "detail": detail}


def _print(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    helper = Helper(MgmtSocket(), emit=_print)
    try:
        _print({"event": "ready"})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError as e:
                _print(_error("bad_request", str(e)))
                continue
            _print(helper.handle(request) if isinstance(request, dict) else _error("bad_request", "not an object"))
    finally:
        helper.cleanup()


if __name__ == "__main__":
    main()
