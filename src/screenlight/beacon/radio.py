"""User-side Bluetooth radio for beacon bulbs: owns the root helper, advertising slots and sequence numbers.

The helper is started lazily (that is when the password dialog appears) and only ever receives finished
adverts. One radio serves the whole app session; the sync engine borrows it.
"""

import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from screenlight.beacon.protocol import advert, build_frame

HELPER_PATH = Path(__file__).resolve().with_name("helper.py")
SYSTEM_PYTHON = "/usr/bin/python3"
INTERVAL_MS = 20
MAX_BULBS = 3
SEQ_BLOCK = 1000
SEQ_ROTATE_AT = 0xFF00
SOURCE_RANGE = (0x4000, 0x7EFF)
REPLY_TIMEOUT = 45.0  # above the helper's worst case: 7 management commands x 5 s each
HEARD_AFTER = 0.3  # how long an advert must stay up before it can be removed and still be heard
_DENIED_EXIT_CODES = (126, 127)  # pkexec: dialog dismissed / not authorised
_HELPER_ERRORS = {"adapter_off": "adapter_off", "slot_busy": "no_slot"}


class RadioError(Exception):
    """code: denied | unsupported | adapter_off | adapter_stuck | helper_stopped | no_slot"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def beacon_supported() -> bool:
    return sys.platform.startswith("linux") and shutil.which("pkexec") is not None \
        and Path(SYSTEM_PYTHON).exists()


def default_launcher() -> subprocess.Popen:
    return subprocess.Popen(["pkexec", SYSTEM_PYTHON, "-I", str(HELPER_PATH)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, bufsize=1)


class SeqState:
    """Our source ID and per-source sequence numbers, reserved in blocks and saved before use."""

    def __init__(self, path: Path, rng: random.Random | None = None):
        self.path = Path(path)
        self.rng = rng or random.Random()
        try:
            data = json.loads(self.path.read_text())
            self.source_id = int(data["source_id"])
            self.reserved = int(data["reserved"])
            if not SOURCE_RANGE[0] <= self.source_id <= SOURCE_RANGE[1]:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            self.source_id, self.reserved = self._new_source(None), 0
        self.next_seq = max(1, self.reserved)

    def _new_source(self, avoid: int | None) -> int:
        while True:
            source = self.rng.randint(*SOURCE_RANGE)
            if source != avoid:
                return source

    def next(self) -> tuple[int, int]:
        if self.next_seq >= SEQ_ROTATE_AT:
            self.source_id, self.next_seq, self.reserved = self._new_source(self.source_id), 1, 0
        if self.next_seq >= self.reserved:
            self.reserved = self.next_seq + SEQ_BLOCK
            self._save()
        seq = self.next_seq
        self.next_seq += 1
        return self.source_id, seq

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"source_id": self.source_id, "reserved": self.reserved}, f)
        os.replace(tmp, self.path)


class BeaconRadio:
    def __init__(self, state_path: Path, launcher: Callable[[], subprocess.Popen] = default_launcher,
                 ready_timeout: float = 120.0, idle_stop: float = 1.0,
                 supported: Callable[[], bool] = beacon_supported):
        self._state_path = Path(state_path)
        self._launcher = launcher
        self._ready_timeout = ready_timeout
        self._idle_stop = idle_stop
        self._supported = supported
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue | None = None
        self._launched = 0
        self._denied = False
        self._dead = False  # gave up after the one allowed restart
        self._instances = 0
        self._active: list[int] = []
        self._slots: dict[str, int] = {}
        self._live: dict[int, float] = {}  # slot -> last advertise time
        self._seq: SeqState | None = None
        self._closed = threading.Event()
        self._idle_thread: threading.Thread | None = None

    # ---- helper lifecycle ---------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._closed.is_set():
                raise RadioError("helper_stopped", "the Bluetooth radio was closed")
            if self._proc is not None and self._proc.poll() is None:
                return
            if not self._supported():
                raise RadioError("unsupported", "Bluetooth bulbs need Linux with pkexec")
            if self._denied:
                raise RadioError("denied", "password prompt was cancelled")
            if self._dead or self._launched >= 2:
                self._dead = True
                raise RadioError("helper_stopped", "the Bluetooth helper stopped")
            self._launch()

    def _launch(self) -> None:
        self._launched += 1
        self._slots.clear()
        self._live.clear()
        proc = self._launcher()
        replies: queue.Queue = queue.Queue()
        threading.Thread(target=_read_lines, args=(proc, replies), name="beacon-helper-out", daemon=True).start()
        self._proc, self._replies = proc, replies
        try:
            message = replies.get(timeout=self._ready_timeout)
        except queue.Empty:
            message = None
        if not (isinstance(message, dict) and message.get("event") == "ready"):
            self._kill()
            code = proc.wait(timeout=5) if proc.poll() is None else proc.returncode
            if code in _DENIED_EXIT_CODES:
                self._denied = True
                self._launched -= 1  # a cancelled prompt doesn't use up the one crash restart
                raise RadioError("denied", "password prompt was cancelled")
            raise RadioError("helper_stopped", f"Bluetooth helper failed to start (exit {code})")
        hello = self._request({"op": "hello"})
        self._instances, self._active = int(hello["instances"]), list(hello["active"])
        if self._idle_thread is None:
            self._idle_thread = threading.Thread(target=self._idle_loop, name="beacon-idle", daemon=True)
            self._idle_thread.start()

    def _request(self, request: dict, on_event: Callable[[dict], None] | None = None,
                 timeout: float = REPLY_TIMEOUT) -> dict:
        proc, replies = self._proc, self._replies
        try:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    message = replies.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    if self._closed.is_set():
                        raise OSError("radio closed") from None
                    if time.monotonic() >= deadline:
                        raise
                    continue
                if message is None:
                    raise OSError("helper exited")
                if "ok" in message:
                    break
                if on_event:
                    on_event(message)
        except (OSError, ValueError, queue.Empty) as e:
            self._kill()
            raise RadioError("helper_stopped", f"the Bluetooth helper stopped: {e}") from None
        if not message["ok"]:
            code = _HELPER_ERRORS.get(message.get("error"), "adapter_stuck")
            raise RadioError(code, message.get("detail", ""))
        return message

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
            except OSError:
                pass  # the helper runs as root; it exits on its own once it sees stdin closed

    def reset_denied(self) -> None:
        # No lock: the GUI thread calls this, and start() can hold the lock for minutes during a password prompt.
        self._denied = False

    def close(self) -> None:
        """Called from the GUI thread, so it must never wait for the lock: a capture or a password prompt can
        hold it for minutes. Closing the helper's stdin makes it clean up and exit; any request in progress
        then fails with helper_stopped."""
        self._closed.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    # ---- slots and sending --------------------------------------------------

    def acquire(self, bulb_id: str) -> int:
        with self._lock:
            self.start()
            if bulb_id in self._slots:
                return self._slots[bulb_id]
            free = [s for s in range(self._instances, 0, -1) if s not in self._active]
            unused = [s for s in free if s not in self._slots.values()]
            # Leave one free instance for the system (and any other program).
            if len(self._slots) >= min(MAX_BULBS, len(free) - 1) or not unused:
                raise RadioError("no_slot", "too many Bluetooth bulbs")
            self._slots[bulb_id] = unused[0]
            return unused[0]

    def send(self, bulb, payload: bytes) -> None:
        with self._lock:
            if self._seq is None:
                self._seq = SeqState(self._state_path)
            for attempt in range(2):
                slot = self.acquire(bulb.id)
                source_id, seq = self._seq.next()
                frame = build_frame(source_id, bulb.node_id, seq, payload, bulb.key, bytes.fromhex(bulb.beacon_key))
                try:
                    self._request({"op": "advertise", "slot": slot, "adv": advert(frame).hex(),
                                   "interval_ms": INTERVAL_MS})
                    break
                except RadioError as e:
                    if e.code != "no_slot" or attempt:
                        raise
                    # Another program took this instance after we said hello: never pick it again.
                    self._active.append(slot)
                    self._slots.pop(bulb.id, None)
                    self._live.pop(slot, None)
            self._live[slot] = time.monotonic()

    def release(self, bulb_id: str) -> None:
        """Stop a bulb's advert, after letting a just-sent command (e.g. "Turn off" at Stop) be heard."""
        with self._lock:
            last = self._live.get(self._slots.get(bulb_id))
            if last is not None:
                time.sleep(max(0.0, HEARD_AFTER - (time.monotonic() - last)))
            slot = self._slots.pop(bulb_id, None)
            if slot is not None and self._live.pop(slot, None) is not None and self._proc is not None:
                try:
                    self._request({"op": "stop", "slot": slot})
                except RadioError:
                    pass

    def _idle_loop(self) -> None:
        while not self._closed.wait(max(self._idle_stop / 4, 0.01)):
            with self._lock:
                now = time.monotonic()
                for slot, last in list(self._live.items()):
                    if now - last >= self._idle_stop and self._proc is not None:
                        del self._live[slot]
                        try:
                            self._request({"op": "stop", "slot": slot})
                        except RadioError:
                            return

    def capture(self, seconds: float, on_frame: Callable[[bytes], None]) -> None:
        with self._lock:
            self.start()
            self._request({"op": "capture", "seconds": seconds},
                          on_event=lambda m: m.get("event") == "frame" and on_frame(bytes.fromhex(m["frame"])),
                          timeout=seconds + REPLY_TIMEOUT)


def _read_lines(proc: subprocess.Popen, replies: queue.Queue) -> None:
    try:
        for line in proc.stdout:
            try:
                replies.put(json.loads(line))
            except ValueError:
                continue
    except (OSError, ValueError):
        pass
    replies.put(None)
