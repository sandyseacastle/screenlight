"""Developer tool: tune the colour correction for a Tuya Bluetooth beacon bulb by eye.

Usage:
  uv run python tools/beacon_calibrate.py --local-key KEY --beacon-key HEX --node N

Part 1 (hue): the window shows a target colour; the bulb shows what is sent. Use Left/Right (Shift for big
steps) until the bulb matches the screen, then Enter. Twelve targets, 0-330 degrees.
Part 2 (saturation): Up/Down raises/lowers the saturation sent to the bulb from near white. Press Enter at the
lowest value where the bulb clearly looks coloured rather than white.
At the end it prints BEACON_HUE_POINTS and BEACON_MIN_SATURATION for src/screenlight/colour.py.
"""

import argparse
import colorsys
import sys
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from screenlight.beacon.protocol import dp_colour  # noqa: E402
from screenlight.beacon.radio import BeaconRadio  # noqa: E402

TARGETS = list(range(0, 360, 30))
SAT_HUE = 30  # judge saturation on orange, the colour that drifted most


def hex_colour(h_deg: float, s: float = 1.0) -> str:
    r, g, b = colorsys.hsv_to_rgb((h_deg % 360) / 360, s, 1.0)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


class Calibrator(tk.Tk):
    def __init__(self, radio, bulb):
        super().__init__()
        self.title("Beacon bulb calibration")
        self.radio, self.bulb = radio, bulb
        self.points: list[tuple[int, int]] = []
        self.index = 0
        self.sent = float(TARGETS[0])
        self.sat = None
        self.swatch = tk.Canvas(self, width=520, height=320, highlightthickness=0)
        self.swatch.pack()
        self.info = tk.Label(self, font=("TkDefaultFont", 13), justify="left")
        self.info.pack(padx=12, pady=10)
        for key, step in [("<Left>", -2), ("<Right>", 2), ("<Shift-Left>", -10), ("<Shift-Right>", 10)]:
            self.bind(key, lambda e, d=step: self.nudge_hue(d))
        self.bind("<Up>", lambda e: self.nudge_sat(0.05))
        self.bind("<Down>", lambda e: self.nudge_sat(-0.05))
        self.bind("<Return>", lambda e: self.accept())
        self.show()

    def send(self, h_deg: float, s: float) -> None:
        self.radio.send(self.bulb, dp_colour((h_deg % 360) / 360, s, 1.0))

    def show(self) -> None:
        if self.sat is None:
            target = TARGETS[self.index]
            self.swatch.configure(bg=hex_colour(target))
            self.info.configure(text=f"Hue {self.index + 1}/12 — screen {target}°, bulb gets {self.sent % 360:.0f}°\n"
                                     "←/→ adjust (Shift = bigger steps) until the bulb matches, then Enter")
            self.send(self.sent, 1.0)
        else:
            self.swatch.configure(bg=hex_colour(SAT_HUE, self.sat))
            self.info.configure(text=f"Saturation — bulb gets {self.sat:.2f}\n"
                                     "↑/↓ adjust; Enter at the LOWEST value where the bulb clearly looks orange")
            self.send(self.corrected(SAT_HUE), self.sat)

    def corrected(self, deg: float) -> float:
        pts = self.curve()
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= deg <= x1:
                return y0 + (y1 - y0) * (deg - x0) / (x1 - x0)
        return deg

    def curve(self) -> list[tuple[int, int]]:
        return self.points + [(360, self.points[0][1] + 360)]

    def nudge_hue(self, delta: int) -> None:
        if self.sat is None:
            self.sent += delta
            self.show()

    def nudge_sat(self, delta: float) -> None:
        if self.sat is not None:
            self.sat = min(1.0, max(0.0, round(self.sat + delta, 2)))
            self.show()

    def accept(self) -> None:
        if self.sat is None:
            self.points.append((TARGETS[self.index], round(self.sent)))
            self.index += 1
            if self.index < len(TARGETS):
                # Start the next target with the same offset as this one: neighbours usually drift alike.
                self.sent = TARGETS[self.index] + (self.sent - TARGETS[self.index - 1])
            else:
                self.sat = 0.1
            self.show()
        else:
            print("\n# Paste into src/screenlight/colour.py")
            print(f"BEACON_HUE_POINTS: tuple[tuple[float, float], ...] = {tuple(self.curve())}")
            print(f"BEACON_MIN_SATURATION = {self.sat}")
            self.destroy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-key", required=True)
    ap.add_argument("--beacon-key", required=True)
    ap.add_argument("--node", type=lambda s: int(s, 0), required=True)
    args = ap.parse_args()
    bulb = SimpleNamespace(id="calibrate", key=args.local_key, node_id=args.node, beacon_key=args.beacon_key)
    radio = BeaconRadio(Path("/tmp/screenlight-calibrate-state.json"), ready_timeout=600, idle_stop=3600)
    try:
        print("Starting the Bluetooth helper (password dialog)...", flush=True)
        radio.start()
        Calibrator(radio, bulb).mainloop()
    finally:
        radio.close()


if __name__ == "__main__":
    main()
