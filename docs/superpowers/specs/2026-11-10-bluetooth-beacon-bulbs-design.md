# Bluetooth beacon bulb support: design

Date: 2026-11-10 · Status: draft for review · Branch: `bluetooth-beacon-bulbs`

## Summary

Let Screen Light drive Tuya / Smart Life **Bluetooth beacon** bulbs alongside the existing Wi-Fi bulbs, from the
same list and the same Start button, with no extra hardware. On Linux, the laptop sends the bulb's own one-way
"P2 beacon" commands as Bluetooth LE advertisements through a small root helper.

## Background (from the 2026-11-10 feasibility spike)

- The target bulb ("Smart BT Lights", Tuya category `dj`) is a **Tuya Beacon Mesh / P2** device. It never accepts
  connections, it never acknowledges commands, and the Tuya cloud always reports it `online: false`. The connectable
  Tuya BLE protocol (`ha_tuya_ble`) does not apply.
- The phone controls it by advertising a 26-byte P2 frame in AD type `0x03` (a "complete list of 16-bit service
  UUIDs"), preceded by Flags `02 01 06`.
- The codec matches the MIT-licensed reference [FinnGrndl/SunsetLampHATuyaBridge](https://github.com/FinnGrndl/SunsetLampHATuyaBridge):
  XXTEA (Tuya big-endian variant, 19 rounds) under the device **local key**, XOR with a 16-byte **beacon key**, an
  inner CRC-8 and an outer P2 CRC.
- Verified on the user's bulb:
  - The beacon key was recovered from a capture of phone commands, and all 9 captured frames validated.
  - The laptop's own frames (with its own source ID) control the bulb.
  - Red/blue alternation at **3, 5, 8 and 12 updates/s** looked even.
  - Pure R/G/B look right. Intermediate hues drift (25° shows as yellow-green), and low saturation looks bluish
    white.
- Transmit constraints found:
  - The bulb **ignores adverts whose Flags come after the frame**. BlueZ's D-Bus advertising API always orders them
    that way, so it cannot be used.
  - The kernel Management API (Add Extended Advertising Params/Data) works but **needs root**.
  - A 1.28 s advertising interval works but lags. 20–100 ms is needed.
  - Removing and re-adding an advert at about 10/s left the controller refusing adds (status `0x03`) until the
    adapter was power-cycled. **Updating the advert data in place** works at 12/s.
- Platform quirks:
  - System Python 3.12 can't bind HCI channels, so the bind goes through `ctypes` with `sockaddr_hci`.
  - uv's Python has no `AF_BLUETOOTH`, so the helper runs on the system `/usr/bin/python3`.
  - Kernel 7.0 reports Management rejections as `ENOMEM` on send.

### Wire format

Advert, 31 bytes: `02 01 06 | 1b 03 | <26-byte P2 frame>`

| Offset | Len | Field |
|---:|---:|---|
| 0 | 1 | frame type, `0x0b` = DP request |
| 1 | 2 | source ID (big-endian) |
| 3 | 2 | destination node ID (big-endian) |
| 5 | 2 | per-source sequence number (big-endian) |
| 7 | 1 | subcommand, `0x05` = DP write |
| 8 | 1 | CRC-8 (poly 0x07) of the 16-byte padded plaintext |
| 9 | 16 | XXTEA(plaintext, local key) XOR beacon key |
| 25 | 1 | outer P2 CRC over the unmasked frame |

Compact datapoints: `dp_id, (type << 4 | len), value…`. DP 1 power: `01 11 00/01`. DP 11 colour:
`0b 04 HH HL SS VV` (H 0–360, S and V 0–100).

## Decisions (confirmed with the user)

1. **Root access:** a password prompt (`pkexec`) when Start or Flash first needs a Bluetooth bulb. One helper per
   session, nothing installed system-wide.
2. **Setup:** guided in-app. Find bulbs → "Set up as Bluetooth..." → Listen → the user turns the bulb off and then on
   in Smart Life → the app derives the beacon key and node ID.
3. **Colour accuracy:** tuned once together during implementation and built in as the default correction for beacon
   bulbs. No in-app calibration.
4. **Integration approach:** a new device type inside the existing `BulbWorker` (approach A). There is no separate
   worker class and no engine split.

Accepted assumptions:
- Bluetooth bulbs are Linux-only. Windows keeps Wi-Fi support.
- Wi-Fi and Bluetooth bulbs mix in one list.

## Goals / non-goals

**Goals:**
- Bluetooth beacon bulbs follow their screen region at the configured rate (default 8/s, up to 12/s verified).
- Setup with no command line.
- A Bluetooth failure never affects Wi-Fi bulbs.
- No keys ever enter the root process.

**Non-goals:**
- Bluetooth on Windows or macOS
- connectable (non-beacon) Tuya BLE bulbs
- Tuya mesh groups
- reading bulb state back
- in-app colour calibration
- more than 3 simultaneous Bluetooth bulbs

## Architecture

```
src/screenlight/beacon/
  __init__.py
  protocol.py   pure codec, no I/O
  helper.py     root helper, standard library only, run as: pkexec /usr/bin/python3 -I <path>/helper.py
  radio.py      user-side BeaconRadio: helper lifecycle, slots, frame building, sequence numbers
bulbs.py        + BeaconDevice; device_factory dispatches on bulb.kind
config.py       + Bulb.kind/node_id/beacon_key, Settings.beacon_source_id/beacon_seq_reserved
colour.py       + correct_for_beacon(hsv)
gui/setup_dialog.py   + "Set up as Bluetooth..." for lights not seen on the LAN
gui/beacon_dialog.py  new: Listen / capture / confirm dialog
tools/beacon_calibrate.py   developer tool for the colour-correction session (not part of the app)
```

### `beacon/protocol.py`

Pure functions:
- `xxtea_encrypt` / `xxtea_decrypt` (16-byte block, 16-byte key)
- `crc8`
- `outer_crc`
- `build_frame(source_id, node_id, seq, payload, local_key, beacon_key) -> bytes`, which returns the 26-byte frame
- `parse_frame(frame, local_key, beacon_key) -> Frame | None`, which validates both CRCs
- `advert(frame) -> bytes`, which returns the 31-byte advert with Flags first
- datapoint encoders: `dp_power(on)`, `dp_colour(h, s, v)`, plus warm-white encoders only if they are verified
  (see Risks)
- `derive_from_off_on(frames, local_key) -> (beacon_key, node_id) | None`. It finds a DP frame whose CRC-8 matches the
  OFF plaintext, derives the key, and requires another frame from the same source and node to validate and decode as
  ON.

### `beacon/helper.py` (root)

- Standard library only, so it runs on the system Python.
- Binds the HCI control channel with a `ctypes` `sockaddr_hci` bind.
- Protocol: JSON lines on stdin/stdout. Every request gets one reply, `{"ok": true}` or
  `{"ok": false, "error": "<code>", "detail": "..."}`.
  - `hello` → adapter info (max adv length, instances, active instances)
  - `advertise {slot, adv_hex, interval_ms}`:
    - The first call for a slot creates the instance: Add Ext Adv Params with flags
      `CONNECTABLE | PARAM_TIMEOUT | PARAM_INTERVAL` and an 18-byte params struct.
    - Later calls update the data in place (Add Ext Adv Data).
    - If an in-place update fails, the helper removes and re-creates the instance once.
    - If that also fails, it returns `adapter_stuck`.
  - `stop {slot}` → removes the instance
  - `capture {seconds}` → starts LE discovery through the Management API and streams
    `{"event": "frame", "adv_hex": ...}` for every advert carrying a 26-byte AD `0x03` with type byte `0x0b`. It ends
    with `{"event": "capture_done"}`.
- Uses only the instances it was told to use. It refuses any instance that is already active, and never touches
  global adapter settings.
- Re-arms each instance's kernel timeout (15 s) about every 10 s while it is in use.
- On stdin EOF or SIGTERM, it removes all of its instances and exits.
- It only ever receives finished adverts. It never sees keys.

### `beacon/radio.py` (user)

- `BeaconRadio`, one per engine run (plus a short-lived one for Flash and setup). It is thread-safe.
- It starts the helper lazily on first use through an injectable launcher. The default launcher is
  `pkexec /usr/bin/python3 -I helper.py`.
- `acquire(bulb) -> slot`:
  - Slots are taken from the highest instance downwards, skipping ones that are already active.
  - Capacity is (supported instances − 1), up to 3. One is left for the system.
- `send(bulb, payload)` → next sequence number, `build_frame`, `advert`, then the helper's `advertise`.
- `release(bulb)` → helper `stop`.
- Idle stop: if a bulb's advert hasn't changed for 1 s, the slot's advert stops. The next send re-creates it.
- Remembers launch outcomes for the session:
  - `denied` (pkexec exit 126/127) means no re-prompting.
  - `unsupported` means not Linux, or no pkexec or `/usr/bin/python3`.
  - A helper that crashes is restarted at most once.
- Sequence numbers:
  - The radio reserves blocks of 1000. `beacon_seq_reserved` is persisted through a settings callback **before** a
    block is used, so a crash never reuses a number.
  - On approaching `0xFFFF`, it switches to a new random `beacon_source_id` and resets the counter. The exact rule
    depends on the wrap-behaviour test in Risks.

### `bulbs.py`: `BeaconDevice`

- Implements the device surface `BulbWorker` uses: `status`, `bulb_has_capability`, `set_hsv`, `turn_off`,
  `set_white_percentage` and `close`.
- `status()` makes sure the radio is started and the slot acquired. It raises `BulbError` with a user-facing message
  on failure, so the worker's existing back-off loop applies.
- `set_hsv`:
  - applies `correct_for_beacon`, then sends DP 11
  - returns immediately, because there is no acknowledgement
  - `BulbWorker`'s rate cap does the pacing
- `turn_off` sends DP 1 false.
- `set_white_percentage` uses verified warm-white datapoints if they exist. Otherwise it sends a fixed warm-white
  colour.
- The worker shows `sending` instead of `online` for Bluetooth bulbs. This is done with a small hook (a status label
  provided by the device), not by special-casing in the GUI.

### Config

- `Bulb` gains:
  - `kind: str = "wifi"`, either `"wifi"` or `"beacon"`
  - `node_id: int = 0`
  - `beacon_key: str = ""` (32 hex characters)
- `Settings` gains:
  - `beacon_source_id: int = 0`. Zero means it hasn't been chosen yet. On first use it is generated at random in
    `0x4000–0x7EFF` and saved.
  - `beacon_seq_reserved: int = 0`
- Existing files load unchanged and their bulbs are Wi-Fi.
- The file stays owner-readable only (mode 0600), as today.
- `discovery.merge_into` keeps `kind`, `node_id` and `beacon_key` when it refreshes a bulb. If a beacon bulb's local
  key changes, it is flagged so the GUI can ask for the Bluetooth setup to be run again.

### GUI

- **Find bulbs:** a light not seen on the LAN gets a "Set up as Bluetooth..." button (Linux only). It opens the beacon
  dialog for that cloud device.
- **Beacon dialog:**
  1. Instructions to press Listen, then turn *name* off and then on in Smart Life.
  2. Listen starts a short-lived radio and the helper's `capture` (60 s), which brings up the password prompt.
  3. Frames feed `derive_from_off_on` with this device's local key.
  4. On success it shows "Found *name* (node 0002)", and Add saves the bulb with `kind="beacon"`.
  5. On timeout it shows hints and offers Retry.
- **Bulbs list:** the IP column shows `Bluetooth` for beacon bulbs. Flash works through a short-lived radio when
  sync is stopped.

## Data flow (sync)

1. **Start:** the engine creates workers. For a beacon bulb, `device_factory` returns a `BeaconDevice` bound to the
   run's shared `BeaconRadio`. Its first `status()` launches the helper with the password prompt; this blocks only
   that worker's thread.
2. **Each frame:** colour → `BulbWorker.submit` (newest wins) → `BeaconDevice.set_hsv` → correction → DP 11 → radio
   → in-place advert update at a 20 ms interval.
3. **Stop:** the workers apply the "When stopped" action, the radio stops all slots, and closing the helper's stdin
   ends it.

## Error handling

| Situation | Bulb status | Behaviour |
|---|---|---|
| Password dialog cancelled | `no permission` | No re-prompt until the next Start. Wi-Fi bulbs continue. |
| Not Linux, or no pkexec or `/usr/bin/python3` | `Bluetooth bulbs need Linux` | Setup option hidden |
| Adapter off or missing | `Bluetooth off` | Worker back-off. The same helper is reused, so there are no extra prompts. |
| Controller refuses adds (`0x03`) after the helper's retry | `Bluetooth adapter needs a reset: toggle Bluetooth off/on` | Worker back-off |
| Helper crashes | `helper stopped` | One restart per session (one more prompt), then stays stopped |
| More than 3 beacon bulbs | `too many Bluetooth bulbs` | Only the extras are affected |
| App crashes or is killed | — | The helper sees EOF and cleans up. A 15 s kernel timeout is the backstop. |
| Setup capture finds nothing | Dialog hints | Retry |

## Colour correction

Implementation includes a session with the user using `tools/beacon_calibrate.py`:
- The tool shows a swatch on screen and sends the same colour to the bulb.
- For about 12 target hues, the user adjusts the sent hue until the bulb matches the screen.
- The user then finds the lowest saturation that still reads as colour.

The results become constants in `colour.py`: a piecewise-linear hue curve and a saturation floor. They are applied by
`correct_for_beacon` to beacon bulbs only, after the existing colour pipeline.

## Testing

- **Unit (no root, no hardware):**
  - `protocol`: round-trips, CRC rejection, `derive_from_off_on` on synthetic captures (including decoys signed with
    other keys), and fixed regression vectors generated with dummy keys. No real keys go in the repo.
  - `helper`: its pure functions (Management packet encoding, parsing device-found adverts, the request dispatcher)
    against byte fixtures.
  - `radio`: tested against a fake helper process that speaks the JSON protocol through the injectable launcher. This
    covers slot allocation, idle stop, sequence reservation and persistence, the `denied` and `unsupported` paths, and
    the helper-crash restart limit.
  - `BeaconDevice` + `BulbWorker` with a fake radio. Engine and config tests are extended, including loading old
    configs.
- **Manual on the user's bulb (checklist in the plan):**
  - sequence-wrap test
  - guided setup
  - 10 minutes of syncing at 8/s
  - each "When stopped" option
  - cancelling the password prompt
  - Bluetooth off mid-sync
  - Flash
  - mixed Wi-Fi + Bluetooth list
- **Docs:** README section on Bluetooth bulbs (Linux only, password prompt, setup steps, troubleshooting).

## Risks and verification items (first implementation tasks)

1. **Sequence-number behaviour.** Before relying on the source-switch plan, test:
   - whether the bulb accepts a lower or wrapped sequence number from a source it has already seen
   - whether introducing new source IDs ever disturbs control from the phone

   If the bulb only tracks a few sources, stop and revisit with the user.
2. **Warm-white datapoints.** These are unverified in compact P2 form. Test DP 2 (mode) and DP 3/4 (brightness and
   temperature) frames. Fall back to a warm-white colour if they don't work.
3. **Helper on the system Python.** It depends on `/usr/bin/python3` having `AF_BLUETOOTH`, which is true on the
   user's Mint system. Detect this and report `unsupported` otherwise.
4. **Security trade-off.** `pkexec` runs `helper.py` from the user-writable project directory. A process running as
   the user could change it before the user types their password. This is accepted for a personal tool and documented
   in the README. The helper takes no keys and only advertises or captures.

## Verification results (2026-11-11, `tools/beacon_probe.py all` on the user's bulb)

The real launch path worked: pkexec dialog → root helper on `/usr/bin/python3` → mgmt adverts. All 19 steps were
sent without errors. The user reports every step showed its labelled colour, and that Smart Life still controls the
bulb afterwards.

- **Sequence numbers:** the bulb accepted a lower number (S3), a repeated number (S4) and a wrap back to 1 (S6, S7).
  It applies no replay protection, so a sequence wrap is harmless. Source rotation stays in as a safety net.
- **New source IDs:** six fresh source IDs were each obeyed, and phone control was unaffected. No change to the design.
- **White mode:** DP 2 = white plus DP 3 brightness plus DP 4 temperature switches to white (W4/W5), and a colour alone
  switches back to colour mode (W6). `BeaconDevice.set_white_percentage` uses these datapoints. The Task 9 checklist
  re-checks that the result looks warm.
- **Found during the run:**
  - When Bluetooth is soft-blocked, the kernel rejects Add Ext Adv Params with REJECTED (0x0b). The helper now
    reports `adapter_off`.
  - A 120 s wait for the password was too short in practice. The app should allow longer (see the Task 9 ruling).
