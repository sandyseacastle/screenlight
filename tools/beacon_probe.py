"""Developer probe for a Tuya Bluetooth beacon bulb: answers hardware questions the app's design depends on.

Usage:
  uv run python tools/beacon_probe.py --local-key KEY --beacon-key HEX --node N  <command>
Commands:
  colour H S V    one colour (H 0-359, S/V 0-100)
  seqtest         does the bulb accept lower, repeated and wrapped sequence numbers?
  whitetest       do the white-mode datapoints give warm white?
  sourcetest      six colours, each from a brand-new source ID (then check Smart Life still works)
  all             seqtest, whitetest, sourcetest in one session (one password prompt)

Uses its own random source IDs (never the app's or the phone's), so stepping sequences backwards is harmless.
"""

import argparse
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screenlight.beacon.protocol import advert, build_frame, dp_colour  # noqa: E402
from screenlight.beacon.radio import INTERVAL_MS, BeaconRadio, RadioError  # noqa: E402

PROBE_SOURCES = (0x7F00, 0x7FFF)  # outside the app's 0x4000-0x7EFF range
HOLD = 3.0


class ProbeRadio(BeaconRadio):
    def send_raw(self, bulb, source_id: int, seq: int, payload: bytes) -> None:
        with self._lock:
            slot = self.acquire(bulb.id)
            frame = build_frame(source_id, bulb.node_id, seq, payload, bulb.key, bytes.fromhex(bulb.beacon_key))
            self._request({"op": "advertise", "slot": slot, "adv": advert(frame).hex(), "interval_ms": INTERVAL_MS})


def hsv(h, s=100, v=100):
    return dp_colour(h / 360, s / 100, v / 100)


WHITE = hsv(0, 0, 100)
RED, GREEN, BLUE = hsv(0), hsv(120), hsv(240)


def steps(radio, bulb, source, plan, t0):
    for label, seq, payload in plan:
        radio.send_raw(bulb, source, seq, payload)
        print(f"[{time.monotonic() - t0:5.1f}s] {label}", flush=True)
        time.sleep(HOLD)


def seqtest(radio, bulb, t0):
    source = random.randint(*PROBE_SOURCES)
    print(f"--- seqtest (source {source:#06x}) ---")
    steps(radio, bulb, source, [
        ("S1 WHITE  seq 1000  (baseline)", 1000, WHITE),
        ("S2 RED    seq 1001  (normal next number: should turn red)", 1001, RED),
        ("S3 GREEN  seq 996   (LOWER than before)", 996, GREEN),
        ("S4 BLUE   seq 1001  (REPEATED number, new colour)", 1001, BLUE),
        ("S5 RED    seq 65534 (jump up: should turn red)", 0xFFFE, RED),
        ("S6 GREEN  seq 1     (WRAPPED around)", 1, GREEN),
        ("S7 BLUE   seq 2     (after the wrap)", 2, BLUE),
    ], t0)


def whitetest(radio, bulb, t0):
    source = random.randint(*PROBE_SOURCES)
    print(f"--- whitetest (source {source:#06x}) ---")
    steps(radio, bulb, source, [
        ("W1 RED    (baseline)", 1, RED),
        ("W2 white mode only (DP2=white)", 2, bytes.fromhex("024100")),
        ("W3 RED    (baseline)", 3, RED),
        ("W4 white mode + brightness 100% + warmest temperature", 4,
         bytes.fromhex("024100") + bytes.fromhex("0324000003e8") + bytes.fromhex("042400000000")),
        ("W5 white mode + brightness 100% + coolest temperature", 5,
         bytes.fromhex("024100") + bytes.fromhex("0324000003e8") + bytes.fromhex("0424000003e8")),
        ("W6 RED    (back to colour mode by colour alone?)", 6, RED),
    ], t0)


def sourcetest(radio, bulb, t0):
    print("--- sourcetest (each colour from a new source ID) ---")
    for i, (name, payload) in enumerate([("RED", RED), ("GREEN", GREEN), ("BLUE", BLUE), ("YELLOW", hsv(60)),
                                          ("CYAN", hsv(180)), ("MAGENTA", hsv(300))], start=1):
        source = random.randint(*PROBE_SOURCES)
        steps(radio, bulb, source, [(f"N{i} {name:<8} from new source {source:#06x}", 1, payload)], t0)
    print("Now check Smart Life on your phone still controls the bulb.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-key", required=True)
    ap.add_argument("--beacon-key", required=True)
    ap.add_argument("--node", type=lambda s: int(s, 0), required=True)
    ap.add_argument("command", choices=["colour", "seqtest", "whitetest", "sourcetest", "all"])
    ap.add_argument("values", nargs="*", type=int)
    args = ap.parse_args()
    bulb = SimpleNamespace(id="probe", key=args.local_key, node_id=args.node, beacon_key=args.beacon_key)
    radio = ProbeRadio(Path("/tmp/screenlight-probe-state.json"), idle_stop=3600, ready_timeout=600)
    t0 = time.monotonic()
    try:
        print("Starting the Bluetooth helper (password dialog)...", flush=True)
        radio.start()
        if args.command == "colour":
            radio.send_raw(bulb, random.randint(*PROBE_SOURCES), 1, hsv(*args.values))
            time.sleep(HOLD)
        for name, fn in [("seqtest", seqtest), ("whitetest", whitetest), ("sourcetest", sourcetest)]:
            if args.command in (name, "all"):
                fn(radio, bulb, t0)
    except RadioError as e:
        sys.exit(f"Radio error [{e.code}]: {e}")
    finally:
        radio.close()


if __name__ == "__main__":
    main()
