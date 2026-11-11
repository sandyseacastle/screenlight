# Bluetooth Beacon Bulbs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screen Light drives Tuya Bluetooth beacon (P2) bulbs alongside its Wi-Fi bulbs, on Linux, through a pkexec root helper.

**Architecture:**
- A pure P2 codec (`beacon/protocol.py`), a standard-library root helper that only advertises finished adverts or
  captures them (`beacon/helper.py`), and a user-side `BeaconRadio` that owns the helper, the slots and the sequence
  numbers (`beacon/radio.py`).
- A `BeaconDevice` plugs into the existing `BulbWorker` through its device factory.
- The GUI gains a guided OFF/ON capture to set a bulb up.

**Tech Stack:** Python ≥3.10 (app, uv-managed), system `/usr/bin/python3` (helper, standard library only), Linux
Bluetooth Management API over an `AF_BLUETOOTH` raw socket, tkinter, pytest.

**Spec:** `docs/superpowers/specs/2026-11-10-bluetooth-beacon-bulbs-design.md`

## Deviations from the spec (approved by the user before execution)

1. **Sequence state lives in `beacon_state.json`** next to `config.json` (mode 0600), not in `Settings`. The radio
   must persist a reservation synchronously from worker threads before using it. A separate file avoids racing the
   GUI's debounced settings saves.
2. **One `BeaconRadio` per app session, not per engine run.** The app restarts the engine on every bulb-list edit, and
   a per-run radio would ask for the password each time. The engine *borrows* the app's radio and never closes it.
   "No re-prompt after cancel" is reset when the user presses Start (`radio.reset_denied()`).

## Global Constraints

- Advert, 31 bytes: `02 01 06 1b 03 <26-byte frame>`, with Flags first. The bulb ignores any other order.
- Frame: type `0x0b`, source (2 bytes, BE), node (2 bytes, BE), sequence (2 bytes, BE), subcommand `0x05`, CRC-8,
  16-byte XXTEA⊕beacon key, outer CRC.
- Datapoints:
  - DP 1 power: `01 11 00|01`
  - DP 11 colour: `0b 04 HH HL SS VV`, with H 0–359, S 0–100, V 1–100
  - A frame carries at most 16 plaintext bytes.
- Advertising interval **20 ms**. Slot data is updated **in place**; a slot is only re-created on failure or re-arm.
- Kernel advert timeout **15 s**, re-armed when a slot is over **10 s** old. Idle stop after **1 s** with no new
  data.
- Slots: highest instance first, skipping active ones. Capacity is `min(3, instances - 1)`.
- Source IDs are random in **0x4000–0x7EFF**. Sequences are reserved in blocks of **1000**. Rotate to a new source
  before the sequence exceeds **0xFF00**.
- The helper uses the **standard library only**, runs on `/usr/bin/python3 -I`, and never receives keys.
- The helper is launched with `pkexec /usr/bin/python3 -I <abs path>/helper.py`. pkexec exit codes **126/127** mean
  denied.
- Bluetooth bulbs are Linux only. On other platforms the UI hides the setup and the bulbs show
  `Bluetooth bulbs need Linux`.
- Status texts, verbatim:
  - `sending`
  - `no permission`
  - `Bluetooth bulbs need Linux`
  - `Bluetooth off`
  - `Bluetooth adapter needs a reset: toggle Bluetooth off/on`
  - `helper stopped`
  - `too many Bluetooth bulbs`
  - `run Bluetooth setup again`
- No real keys in the repo. Tests use dummy keys only.

## Review Focus

1. **Bulb re-paired in Smart Life:** the local key changes and the old beacon key goes stale, so the bulb silently
   ignores commands. Expect the status `run Bluetooth setup again`, not `sending` (Task 5 and Task 6 tests).
2. **Another program already using advertising slots (or a second Screen Light):** expect the extra bulbs to show
   `too many Bluetooth bulbs` and the other program's adverts to be left alone (Task 2 and Task 3 tests).
3. **Bluetooth toggled or laptop suspended mid-sync:** in-place updates start failing. Expect recovery without a new
   password prompt (Task 2 and Task 3 tests).
4. **Editing the bulb list while syncing:** this restarts the engine. Expect no new password prompt (Task 6 test).
5. **Black or very dark screen regions:** V would round to 0, and H = 1.0 would encode as 360. Expect V clamped to 1
   and H wrapped to 0 (Task 1 and Task 6 tests).

---

### Task 1: P2 codec (`beacon/protocol.py`)

**Files:**
- Create: `src/screenlight/beacon/__init__.py` (empty), `src/screenlight/beacon/protocol.py`
- Test: `tests/test_beacon_protocol.py`

**Interfaces:**
- Produces:
  - `xxtea_encrypt(block: bytes, key: bytes) -> bytes`, `xxtea_decrypt(block: bytes, key: bytes) -> bytes`. Both
    take a 16-byte block and key, use the Tuya big-endian variant with 19 rounds and delta `0x9E3779B9`, and raise
    `ValueError` on bad lengths.
  - `crc8(data: bytes) -> int`: poly 0x07, init 0.
  - `outer_crc(frame25: bytes) -> int`: poly 0x8380 over `(b0 & 0xFC) + b[1:25]`, returning the high byte.
  - `build_frame(source_id: int, node_id: int, seq: int, payload: bytes, local_key: str, beacon_key: bytes) -> bytes`,
    which returns 26 bytes.
  - `parse_frame(frame: bytes, local_key: str, beacon_key: bytes) -> Frame | None`. `Frame` is a frozen dataclass
    `(source_id: int, node_id: int, seq: int, datapoints: dict[int, bytes])`.
  - `advert(frame: bytes) -> bytes`, which returns 31 bytes with Flags first.
  - `dp_power(on: bool) -> bytes`
  - `dp_colour(h: float, s: float, v: float) -> bytes`. It takes HSV floats 0–1: H is `round(h*360) % 360`, S is
    `round(s*100)`, V is `max(1, round(v*100))`.
  - `derive_from_off_on(frames: Iterable[bytes], local_key: str) -> tuple[bytes, int] | None`, which returns
    `(beacon_key, node_id)`.
  - `local_key` is the 16-character ASCII string from the Tuya cloud. It is encoded with ASCII inside the module.

- [ ] **Step 1: Write the failing tests** (dummy keys `LK = "0123456789abcdef"`, `BK = bytes(range(16))`; the
  vectors come from the spike implementation that controlled the real bulb)

```python
def test_xxtea_known_answer_and_round_trip():
    assert xxtea_encrypt(bytes(16), LK.encode()).hex() == "e1939f01f4c06036ae8fb9dcc7883b7f"
    assert xxtea_decrypt(xxtea_encrypt(bytes(range(16)), LK.encode()), LK.encode()) == bytes(range(16))

def test_crc8_check_value():
    assert crc8(b"123456789") == 0xF4

@pytest.mark.parametrize("seq,payload,expected", [
    (0x100, dp_power(False), "0b5c110002010005d6efa00f81f8516528a4a9a2904da64bd2ab"),
    (0x101, dp_power(True),  "0b5c11000201010533eb14c7a272416d6b2aa86361a7449d20a9"),
    (0x102, bytes((0x0B, 0x04, 0, 240, 100, 100)), "0b5c110002010205c0f4f27e72e123d03a51e1fbe6037f120194"),
])
def test_build_frame_known_answers(seq, payload, expected):
    assert build_frame(0x5C11, 0x0002, seq, payload, LK, BK).hex() == expected

def test_parse_round_trip_and_rejects_corruption():
    f = build_frame(0x4001, 7, 42, dp_colour(2 / 3, 1, 1), LK, BK)
    assert parse_frame(f, LK, BK) == Frame(0x4001, 7, 42, {11: bytes((0, 240, 100, 100))})
    for i in (8, 12, 25):
        bad = bytearray(f); bad[i] ^= 1
        assert parse_frame(bytes(bad), LK, BK) is None
    assert parse_frame(f, "fedcba9876543210", BK) is None

def test_advert_layout():
    a = advert(bytes(26))
    assert len(a) == 31 and a[:5] == bytes.fromhex("0201061b03")

def test_dp_colour_clamps_dark_and_wraps_hue():          # Review Focus 5
    assert dp_colour(1.0, 1.0, 0.0) == bytes((0x0B, 0x04, 0, 0, 100, 1))
    assert dp_colour(0.5, 0.5, 0.5) == bytes((0x0B, 0x04, 0, 180, 50, 50))

def test_derive_from_off_on_ignores_decoys():
    other = "fedcba9876543210"
    frames = [build_frame(0x7A09, 2, 9, dp_power(False), other, BK),          # decoy: other bulb's key
              build_frame(0x7A09, 2, 10, dp_colour(0, 1, 1), LK, BK),
              build_frame(0x7A09, 2, 11, dp_power(False), LK, BK),
              build_frame(0x7A09, 2, 12, dp_power(True), LK, BK)]
    assert derive_from_off_on(frames, LK) == (BK, 2)
    assert derive_from_off_on(frames[:3], LK) is None                          # OFF without ON
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_beacon_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: screenlight.beacon`

- [ ] **Step 3: Implement `protocol.py`.** It follows the spec's wire-format table. In `derive_from_off_on`, the
  candidate key for a DP frame (type `0x0b`, subcommand `0x05`) whose byte 8 equals `crc8` of the padded OFF
  plaintext is `frame[9:25] XOR xxtea_encrypt(padded OFF, local_key)`. Keep the candidate only if the outer CRC
  validates. Then require a different frame with the same source and node that `parse_frame` decodes as
  `{1: b"\x01"}`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_beacon_protocol.py -v`
Expected: all PASS

- [ ] **Step 5: Commit** with `git commit -m "feat(beacon): P2 beacon codec"`

---

### Task 2: Root helper (`beacon/helper.py`)

**Files:**
- Create: `src/screenlight/beacon/helper.py`
- Test: `tests/test_beacon_helper.py`

**Interfaces:**
- Consumes: nothing from the app. The helper must import only the standard library. It is run as a script by the
  system Python and must not import `screenlight`.
- Produces (pure, testable):
  - `encode_command(op: int, index: int, params: bytes) -> bytes`
  - `parse_event(packet: bytes) -> tuple[int, int, bytes] | None`, returning `(event code, index, payload)`
  - `ext_adv_params(instance: int, interval_ms: int, timeout_s: int) -> bytes`, the 18-byte struct `<BIHHIIb` with
    flags `CONNECTABLE | PARAM_TIMEOUT | PARAM_INTERVAL` and tx power 0
  - `ext_adv_data(instance: int, adv: bytes) -> bytes`
  - `p2_frames_from_device_found(params: bytes) -> list[bytes]`: the payload of each AD `0x03` with length 26 and
    first byte `0x0b`, taken from a Device Found (`0x0012`) event
  - `class Helper(mgmt, clock=time.monotonic)` with `handle(request: dict) -> dict`, plus `cleanup()`, which removes
    every slot it created
  - `mgmt` is any object with `command(op: int, params: bytes) -> bytes` that raises `MgmtError(op, status)`. The
    real one is `MgmtSocket` (ctypes `sockaddr_hci` bind to `(0xFFFF, channel 3)`).
- JSON-lines protocol on stdin and stdout (the radio relies on these exact shapes):
  - On startup, the helper prints `{"event": "ready"}`.
  - `{"op": "hello"}` → `{"ok": true, "instances": int, "active": [int], "max_len": int}`
  - `{"op": "advertise", "slot": int, "adv": "<62 hex>", "interval_ms": 20}` → `{"ok": true}`
  - `{"op": "stop", "slot": int}` → `{"ok": true}`
  - `{"op": "capture", "seconds": float}` → zero or more `{"event": "frame", "frame": "<52 hex>"}`, then
    `{"ok": true}`
  - Errors come back as `{"ok": false, "error": code, "detail": str}`, where code is one of `adapter_off` (mgmt status
    0x0f), `adapter_stuck`, `slot_busy`, `bad_request` or `mgmt_error`.
  - On stdin EOF or SIGTERM, the helper calls `cleanup()` and exits 0.

- [ ] **Step 1: Write the failing tests.** Use a `FakeMgmt` that records `(op, params)` and can be scripted to raise
  `MgmtError` for given ops.

```python
def test_ext_adv_params_exact_bytes():
    assert ext_adv_params(4, 20, 15).hex() == "040160000000000f00200000002000000000"

def test_first_advertise_creates_then_updates_in_place():
    m = FakeMgmt(active=[]); h = Helper(m)
    assert h.handle({"op": "advertise", "slot": 4, "adv": ADV.hex(), "interval_ms": 20}) == {"ok": True}
    assert h.handle({"op": "advertise", "slot": 4, "adv": ADV2.hex(), "interval_ms": 20}) == {"ok": True}
    assert [op for op, _ in m.calls] == [READ_ADV_FEATURES, ADD_EXT_ADV_PARAMS, ADD_EXT_ADV_DATA, ADD_EXT_ADV_DATA]

def test_refuses_slot_active_elsewhere():                       # Review Focus 2
    h = Helper(FakeMgmt(active=[4]))
    assert h.handle({"op": "advertise", "slot": 4, "adv": ADV.hex(), "interval_ms": 20})["error"] == "slot_busy"

def test_failed_update_recreates_once_then_reports_stuck():    # Review Focus 3
    m = FakeMgmt(active=[]); h = Helper(m)
    h.handle({"op": "advertise", "slot": 4, "adv": ADV.hex(), "interval_ms": 20})
    m.fail_next(ADD_EXT_ADV_DATA, status=0x03)                  # recreate succeeds
    assert h.handle({"op": "advertise", "slot": 4, "adv": ADV2.hex(), "interval_ms": 20}) == {"ok": True}
    m.fail_next(ADD_EXT_ADV_DATA, status=0x03, times=2)
    assert h.handle({"op": "advertise", "slot": 4, "adv": ADV.hex(), "interval_ms": 20})["error"] == "adapter_stuck"

def test_not_powered_maps_to_adapter_off(): ...                # status 0x0f on ADD_EXT_ADV_PARAMS → "adapter_off"

def test_rearms_after_10_seconds():                            # clock injected
    # create at t=0, advertise at t=11 → REMOVE_ADV, ADD_EXT_ADV_PARAMS, ADD_EXT_ADV_DATA (not an in-place update)

def test_cleanup_removes_only_own_slots(): ...                 # created 4 and 3 → REMOVE_ADV for 4 and 3 only

def test_p2_frames_from_device_found():
    eir = bytes.fromhex("020106") + bytes([27, 0x03]) + FRAME + bytes.fromhex("03031234")
    params = bytes(6) + bytes([1, 0xC0]) + bytes(4) + len(eir).to_bytes(2, "little") + eir
    assert p2_frames_from_device_found(params) == [FRAME]
```

- [ ] **Step 2: Run them and confirm they fail.** Run `uv run pytest tests/test_beacon_helper.py -v`; expected FAIL
  (import error).

- [ ] **Step 3: Implement `helper.py`.**
  - Opcodes: READ_ADV_FEATURES 0x003D, REMOVE_ADV 0x003F, ADD_EXT_ADV_PARAMS 0x0054, ADD_EXT_ADV_DATA 0x0055,
    START_DISCOVERY 0x0023 and STOP_DISCOVERY 0x0024 (address type `0x06`, LE only).
  - Events: CMD_COMPLETE 0x0001, CMD_STATUS 0x0002 and DEVICE_FOUND 0x0012.
  - `capture` starts discovery, collects frames until the deadline (deduplicating identical frames), then stops
    discovery.
  - The `__main__` block: install a SIGTERM handler, print `ready`, run a stdin loop that writes one JSON line per
    reply and flushes, and call `cleanup()` in `finally`.

- [ ] **Step 4: Run the tests and confirm they pass.** Run `uv run pytest tests/test_beacon_helper.py -v`.

- [ ] **Step 5: Manual smoke test (root).** Run `printf '{"op":"hello"}\n' | sudo /usr/bin/python3 -I src/screenlight/beacon/helper.py`.
  Expected: a `ready` line, then `{"ok": true, "instances": 4, ...}`.

- [ ] **Step 6: Commit** with `git commit -m "feat(beacon): stdlib root helper for advertising and capture"`

---

### Task 3: Sequence state and `BeaconRadio` (`beacon/radio.py`)

**Files:**
- Create: `src/screenlight/beacon/radio.py`, `tests/fake_helper.py` (a script that speaks the helper protocol, with
  behaviour set by environment variables)
- Test: `tests/test_beacon_radio.py`

**Interfaces:**
- Consumes: `protocol.build_frame`, `protocol.advert`, and `config.Bulb` (`id`, `key`, `node_id`, `beacon_key` from
  Task 5). The radio reads these through `getattr`, so Task 5 may come later.
- Produces:
  - `class RadioError(Exception)` with `.code`, one of `denied`, `unsupported`, `adapter_off`, `adapter_stuck`,
    `helper_stopped` or `no_slot`
  - `class SeqState(path: Path, rng=random.Random())` with `next() -> tuple[int, int]`, which returns
    `(source_id, seq)`
  - `beacon_supported() -> bool`, true when Linux, `pkexec` is available and `/usr/bin/python3` exists
  - `default_launcher() -> subprocess.Popen` (the pkexec command line from Global Constraints; text mode,
    line-buffered)
  - `class BeaconRadio(state_path: Path, launcher=default_launcher, ready_timeout=120.0, idle_stop=1.0)` with these
    methods:
    - `start() -> None`, which is idempotent and raises `RadioError`
    - `acquire(bulb_id: str) -> int`
    - `send(bulb, payload: bytes) -> None`
    - `release(bulb_id: str) -> None`
    - `capture(seconds: float, on_frame: Callable[[bytes], None]) -> None`
    - `reset_denied() -> None`
    - `close() -> None`
  - The radio is thread-safe, with one lock around request/reply. Idle stop runs on a daemon thread.
- Helper error codes map to `RadioError` codes as follows: `adapter_off` → `adapter_off`, `slot_busy` → `no_slot`,
  and anything else → `adapter_stuck`.
- Launch outcomes:
  - The process exits before `ready` with code 126 or 127 → `denied`, remembered until `reset_denied()`.
  - Any other early exit, or a lost pipe later → `helper_stopped`. The radio restarts the helper at most once per
    session, after which it stays stopped.
  - `start()` on an unsupported platform → `unsupported`.

- [ ] **Step 1: Write the failing tests**

```python
def test_seq_state_reserves_before_use_and_survives_restart(tmp_path):
    p = tmp_path / "beacon_state.json"
    s = SeqState(p); src, first = s.next()
    assert 0x4000 <= src <= 0x7EFF
    assert json.loads(p.read_text())["reserved"] >= first + 1      # persisted before returned
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    s2 = SeqState(p); assert s2.next()[1] > first                   # never reuses after a "crash"

def test_seq_state_rotates_source_before_0xff00(tmp_path):
    # seed the file with {"source_id": 0x4abc, "reserved": 0xFF00} → next() returns a new source and seq 1

def test_denied_is_remembered_until_reset(fake_launcher):         # fake exits 126 before "ready"
    r = BeaconRadio(tmp, launcher=fake_launcher("exit126"))
    with pytest.raises(RadioError) as e: r.start()
    assert e.value.code == "denied" and fake_launcher.launches == 1
    with pytest.raises(RadioError): r.start()
    assert fake_launcher.launches == 1                              # no second prompt
    r.reset_denied(); # next start() launches again

def test_slots_highest_first_skip_active_capacity(fake_launcher):  # Review Focus 2
    # fake hello: instances=4, active=[4] → acquire(a)=3, acquire(b)=2, acquire(c) raises RadioError("no_slot")

def test_send_builds_advert_with_next_sequence(fake_launcher): ...  # fake records adverts; parse_frame decodes them
def test_idle_stop_after_one_second(fake_launcher): ...             # idle_stop=0.05 → fake receives {"op":"stop"}
def test_helper_crash_restarts_once_then_stays_stopped(fake_launcher): ...
def test_adapter_off_error_propagates_without_relaunch(fake_launcher): ...   # Review Focus 3: launches stays 1
```

- [ ] **Step 2: Run them and confirm they fail.** Run `uv run pytest tests/test_beacon_radio.py -v`.

- [ ] **Step 3: Implement `radio.py` and `tests/fake_helper.py`.**
  - The `fake_launcher` fixture runs `sys.executable tests/fake_helper.py` with a mode environment variable and
    counts launches.

- [ ] **Step 4: Run the tests and confirm they pass**, then run the full suite with `uv run pytest`.

- [ ] **Step 5: Commit** with `git commit -m "feat(beacon): BeaconRadio with helper lifecycle, slots and sequence state"`

---

### Task 4: Hardware verification gate (manual, on the user's bulb)

**Files:**
- Create: `tools/beacon_probe.py`, a developer CLI built on `BeaconRadio`. Keys come from `--local-key`,
  `--beacon-key` and `--node` arguments, so no config is needed.
- Modify: the spec, adding a `## Verification results` section.

**Interfaces:**
- Consumes: `BeaconRadio`, `protocol.*`
- Produces: the recorded results that decide Tasks 6–7 behaviour. There is no runtime code.

- [ ] **Step 1: Write `tools/beacon_probe.py`** with these subcommands:
  - `colour H S V` sends one colour.
  - `seqtest` sends red at seq N, green at N−5, blue at N+1000, then red at 1. It prints each step and asks the user
    what the bulb showed.
  - `sourcetest` sends 6 colours, each from a fresh random source ID. Afterwards the user checks Smart Life still
    controls the bulb.
  - `whitetest` sends DP 2 enum 0 (`02 41 00`), then DP 3 brightness 1000 (`03 24 000003e8`), then DP 4 temperature 0
    (`04 24 00000000`), each on its own.
  - The probe uses a fixed test source of its own, so these tests can step the sequence backwards without touching
    the app's source.
- [ ] **Step 2: Run each subcommand with the user** (the executor supplies the keys from the session) and record what
  they see.
- [ ] **Step 3: Record the results in the spec and decide:**
  - **Old or wrapped sequence numbers accepted?** If yes, keep source rotation as a harmless safety net.
  - **New sources break phone control?** If yes, **stop and consult the user** before Task 6.
  - **Does `whitetest` give warm white?** If yes, `BeaconDevice.set_white_percentage` uses those DPs. If not, it uses
    the fallback colour `(30/360, 0.45, brightness)`.
- [ ] **Step 4: Commit** with `git commit -m "chore(beacon): probe tool and hardware verification results"`

---

### Task 5: Config fields and discovery merge

**Files:**
- Modify: `src/screenlight/config.py` (`Bulb`), `src/screenlight/discovery.py` (`merge_into`)
- Test: `tests/test_config.py`, `tests/test_discovery.py`

**Interfaces:**
- Produces:
  - `Bulb.kind: str = "wifi"`, either `"wifi"` or `"beacon"`
  - `Bulb.node_id: int = 0`
  - `Bulb.beacon_key: str = ""` (32 hex characters)
  - `Bulb.is_beacon` property
  - `config.beacon_state_path() -> Path`, which is `config_path().parent / "beacon_state.json"`
- `merge_into` keeps `kind`, `node_id` and `beacon_key`. If a beacon bulb's `key` changes, it clears `beacon_key`.

- [ ] **Step 1: Write the failing tests**

```python
def test_old_config_loads_bulbs_as_wifi(tmp_path):    # file written without the new fields
    assert load(path).bulbs[0].kind == "wifi" and not load(path).bulbs[0].is_beacon

def test_beacon_bulb_round_trip(tmp_path):
    b = Bulb(name="BT", id="x", ip="Bluetooth", key="0123456789abcdef", kind="beacon", node_id=2, beacon_key="00" * 16)
    save(Settings(bulbs=(b,)), path); assert load(path).bulbs == (b,)

def test_refresh_keeps_beacon_fields_and_clears_stale_beacon_key():   # Review Focus 1
    old = Bulb(name="BT", id="x", ip="Bluetooth", key="aaaaaaaaaaaaaaaa", kind="beacon", node_id=2, beacon_key="11" * 16)
    same, _, _ = merge_into((old,), [Bulb(name="BT", id="x", key="aaaaaaaaaaaaaaaa")])
    assert same[0].beacon_key == "11" * 16 and same[0].kind == "beacon" and same[0].ip == "Bluetooth"
    changed, _, _ = merge_into((old,), [Bulb(name="BT", id="x", key="bbbbbbbbbbbbbbbb")])
    assert changed[0].beacon_key == "" and changed[0].key == "bbbbbbbbbbbbbbbb"
```

- [ ] **Step 2: Run them and confirm they fail.** Run `uv run pytest tests/test_config.py tests/test_discovery.py -v`.
- [ ] **Step 3: Implement.** For beacon bulbs, `merge_into` never takes `ip` or `version` from the found entry.
- [ ] **Step 4: Run the tests and confirm they pass** with `uv run pytest`.
- [ ] **Step 5: Commit** with `git commit -m "feat(beacon): beacon fields in bulb config"`

---

### Task 6: `BeaconDevice`, worker status hook and engine wiring

**Files:**
- Modify: `src/screenlight/bulbs.py`, `src/screenlight/engine.py`
- Test: `tests/test_bulbs.py`, `tests/test_engine.py`, new `tests/test_beacon_device.py`

**Interfaces:**
- Consumes:
  - `BeaconRadio` / `RadioError` (Task 3)
  - `dp_power`, `dp_colour` (Task 1)
  - `Bulb.is_beacon` (Task 5)
  - Task 4's warm-white decision
  - `colour.correct_for_beacon` from Task 7. Until then, use an identity stub: define it in Task 7's file now,
    returning its input.
- Produces:
  - `class BeaconDevice(bulb: Bulb, radio: BeaconRadio)` with the `BulbWorker` device surface, plus
    `online_label = "sending"`
  - `make_device(bulb: Bulb, radio: BeaconRadio | None) -> device`, which dispatches on `bulb.kind`
  - `BEACON_STATUS: dict[str, str]`:
    - `denied` → `no permission`
    - `unsupported` → `Bluetooth bulbs need Linux`
    - `adapter_off` → `Bluetooth off`
    - `adapter_stuck` → `Bluetooth adapter needs a reset: toggle Bluetooth off/on`
    - `helper_stopped` → `helper stopped`
    - `no_slot` → `too many Bluetooth bulbs`
  - `SyncEngine(..., radio: BeaconRadio | None = None)`. The engine borrows the radio and never closes it.
- `BulbWorker._connect` sets `self.status = getattr(self._dev, "online_label", "online")`.

- [ ] **Step 1: Write the failing tests** (with a `FakeRadio` recording `send(bulb, payload)` calls and scriptable
  to raise `RadioError`)

```python
def test_set_hsv_sends_corrected_colour_and_returns_immediately(): ...  # payload == dp_colour(*correct_for_beacon(hsv))
def test_colour_after_off_prefixes_power_on():
    d.turn_off(); d.set_hsv(0, 1, 1)
    assert radio.payloads[-1] == dp_power(True) + dp_colour(0, 1, 1)
def test_status_maps_radio_errors(): ...         # each RadioError code → BEACON_STATUS text in the BulbError message
def test_missing_beacon_key_says_run_setup_again():                     # Review Focus 1
    with pytest.raises(BulbError, match="run Bluetooth setup again"): BeaconDevice(bulb_without_key, radio).status()
def test_worker_shows_sending_for_beacon_devices(): ...                 # worker.status == "sending"
def test_engine_borrows_radio_and_never_closes_it():                    # Review Focus 4
    e = SyncEngine(settings_with_beacon, radio=radio, grabber_factory=lambda monitor: FakeGrabber()); e.start(); e.stop()
    assert not radio.closed
```

- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
  - `status()` checks `beacon_key` first, then calls `radio.start()` and `radio.acquire(bulb.id)`.
  - `close()` calls `radio.release(bulb.id)`.
  - `identify_bulb` keeps its signature. Callers pass `device_factory=lambda b: make_device(b, radio)`.
- [ ] **Step 4: Run the tests and confirm they pass** with `uv run pytest`.
- [ ] **Step 5: Commit** with `git commit -m "feat(beacon): BeaconDevice plugged into BulbWorker and engine"`

---

### Task 7: Colour correction (`correct_for_beacon` + calibration session)

**Files:**
- Modify: `src/screenlight/colour.py`
- Create: `tools/beacon_calibrate.py`
- Test: `tests/test_colour.py`

**Interfaces:**
- Produces:
  - `BEACON_HUE_POINTS: tuple[tuple[float, float], ...]`, pairs of (screen hue°, sent hue°) covering 0 and 360
  - `BEACON_MIN_SATURATION: float`
  - `correct_for_beacon(hsv: HSV) -> HSV`:
    - Hue goes through piecewise-linear interpolation over the points.
    - If `s < 0.1`, the colour is treated as white and left unchanged.
    - Otherwise `s' = floor + (1 - floor) * s`.
    - V is unchanged.

- [ ] **Step 1: Write the failing tests:**
  - identity behaviour with the initial points `((0, 0), (360, 360))` and floor 0
  - interpolation, using monkeypatched points `((0, 0), (30, 15), (360, 360))`: a screen hue of 30° sends 15°
  - near-white colours stay unchanged when the floor is 0.4

- [ ] **Step 2: Implement it** with the initial identity constants. Run `uv run pytest tests/test_colour.py -v`;
  expect PASS.
- [ ] **Step 3: Write `tools/beacon_calibrate.py`:**
  - It opens a tkinter window with a swatch and sends the same colour to the bulb through `BeaconRadio`, reading keys
    from the user's config.
  - The targets are 12 hues, 0–330° in steps of 30°.
  - ←/→ nudges the sent hue by 2°. Enter records the pair and moves on.
  - A saturation step finds the lowest S that still reads as colour.
  - At the end it prints the Python literals for the two constants.
- [ ] **Step 4: Run the session with the user** and paste the printed constants into `colour.py`. Re-run
  `uv run pytest`.
- [ ] **Step 5: Commit** with `git commit -m "feat(beacon): colour correction tuned on the user's bulb"`

---

### Task 8: GUI: Bluetooth setup from Find bulbs

**Files:**
- Create: `src/screenlight/gui/beacon_dialog.py`
- Modify: `src/screenlight/gui/setup_dialog.py`, `src/screenlight/gui/app.py`

**Interfaces:**
- Consumes:
  - `BeaconRadio.capture`
  - `derive_from_off_on`
  - `beacon_supported`
  - the app's radio (see Task 9: `App.radio()`)
  - `Candidate` (an existing type)
- Produces:
  - `BeaconDialog.ask(parent, call_soon, radio, candidate: Candidate) -> Bulb | None`, which returns a beacon bulb
    with `ip="Bluetooth"`
  - `SetupDialog` gains a "Set up as Bluetooth..." button. It is enabled when the selected row `is_light and not
    on_lan` and `beacon_supported()`, and hidden otherwise.

- [ ] **Step 1: Implement the dialog.** Copy text, verbatim:
  - Instructions: "Press Listen, then in Smart Life turn *{name}* OFF, then ON. Keep your phone near this computer."
  - Success: "Found {name} (node {node:04X})."
  - Timeout: "Didn't catch it. Check your phone's Bluetooth is on and it's near this computer, then try again."

  Capture runs on a worker thread for 60 s, feeding frames to `derive_from_off_on`. It stops early on success and
  routes UI updates through `call_soon`, as `SetupDialog._safe` does.
- [ ] **Step 2: Add a GUI smoke test.** Run `uv run screenlight`, open Find bulbs, check the button state for a
  non-LAN light, and open and cancel the dialog. Check that cancelling the password prompt shows `no permission`.
- [ ] **Step 3: Manual test with the user:** a real setup of their bulb, producing a saved beacon bulb in
  `config.json`.
- [ ] **Step 4: Commit** with `git commit -m "feat(beacon): guided Bluetooth bulb setup"`

---

### Task 9: GUI: app-session radio, list, Flash, Start

**Files:**
- Modify: `src/screenlight/gui/app.py`
- Test: `tests/test_engine.py` (radio reuse across restarts)

**Interfaces:**
- Consumes: `BeaconRadio`, `beacon_supported`, `make_device`, `SyncEngine(radio=...)`
- Produces:
  - `App.radio() -> BeaconRadio`, created lazily with `config.beacon_state_path()` and closed in `_on_close`
  - The IP column shows `Bluetooth` for beacon bulbs.
  - `_start_engine` calls `radio.reset_denied()` only for a user-initiated Start, not for `_restart_if_running`.
  - On non-Linux platforms, beacon bulbs show `Bluetooth bulbs need Linux` and are never sent anything.
  - `_flash` uses `make_device(b, self.radio())` for beacon bulbs when stopped.

- [ ] **Step 1: Write the failing test.** Two engine runs with the same `FakeRadio` (simulating a list edit) should
  give `radio.starts == 1`. This is Review Focus 4 at engine level.
- [ ] **Step 2: Implement it**, then run `uv run pytest` and expect PASS.
- [ ] **Step 3: Manual checklist with the user, recording each result in the commit message:**
  - [ ] sync for 10 minutes at 8/s, with the bulb following its region
  - [ ] a mixed Wi-Fi + Bluetooth list (or Bluetooth only, if no Wi-Fi bulb is on hand)
  - [ ] "When stopped" set to keep, warm white and off
  - [ ] cancelling the password prompt: Bluetooth shows `no permission` while the rest keeps going, and the next Start
    asks again
  - [ ] Bluetooth turned off mid-sync shows `Bluetooth off`, then recovers when it's turned back on, with no new
    prompt
  - [ ] Flash while stopped and while running
  - [ ] editing the list while syncing doesn't prompt again
  - [ ] closing the app stops the advert (verify with `sudo btmon` that no adverts continue)
- [ ] **Step 4: Commit** with `git commit -m "feat(beacon): Bluetooth bulbs in the main window"`

---

### Task 10: README and final review

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Bluetooth bulbs (Linux)" section.** It should cover:
  - which bulbs work (Tuya beacon "Smart BT" lights)
  - the password prompt and why it's needed
  - the setup steps
  - limits: 3 bulbs, no status feedback, Linux only
  - troubleshooting, using the status texts from Global Constraints
  - the security note from spec risk 4
  - updates to the Development module list
- [ ] **Step 2: Run the full suite.** Run `uv run pytest`; expect all PASS with no warnings from the new tests.
- [ ] **Step 3: Commit** with `git commit -m "docs: Bluetooth bulb support"`
