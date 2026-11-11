"""Main window: pick a display, manage bulbs, tune the colour, start/stop syncing."""

import colorsys
import dataclasses
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from screenlight import config
from screenlight.bulbs import identify_bulb, make_device
from screenlight.capture import Grabber, list_monitors, scale_frame, thumbnail_ppm
from screenlight.config import Settings
from screenlight.discovery import merge_into
from screenlight.engine import SyncEngine, manual_colours, region_colours
from screenlight.gui.bulb_dialog import BulbDialog
from screenlight.gui.common import checkbox_images
from screenlight.gui.setup_dialog import SetupDialog
from screenlight.presets import GRADIENTS, apply_gradient
from screenlight.regions import region_label

PREVIEW_WIDTH = 460
PUMP_MS = 50
IDLE_PREVIEW_S = 1.0
RADIO_READY_TIMEOUT = 300.0  # time allowed to type the password; 120 s proved too short on real use

_HEALTHY = ("", "connecting", "online", "sending", "stopped", "disabled")
SOURCES = {"Follow screen": "screen", "Manual colours": "manual"}
MODES = {"Vibrant": "vibrant", "Plain average": "plain"}
ON_STOP = {"Keep last colour": "keep", "Warm white": "white", "Turn off": "off"}


def short_status(status: str) -> str:
    return status.replace("Network Error: ", "")


def _label_for(mapping: dict, value: str) -> str:
    return next((k for k, v in mapping.items() if v == value), next(iter(mapping)))


def hsv_hex(hsv) -> str:
    r, g, b = colorsys.hsv_to_rgb(*hsv)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


class App(tk.Tk):
    def __init__(self, settings: Settings):
        super().__init__()
        self.title("Screen Light")
        self.settings = settings
        self.engine: SyncEngine | None = None
        self.monitors = []
        self._calls: queue.Queue = queue.Queue()
        self._latest_preview = None
        self._stop_thread: threading.Thread | None = None
        self._idle_grabber: Grabber | None = None
        self._next_idle_preview = 0.0
        self._photo = None
        self._statuses: dict[str, str] = {}
        self._save_job = None
        self._vars: dict[str, tk.Variable] = {}
        self._radio = None

        self._style()
        self._build()
        self._load_monitors()
        self._refresh_bulbs()
        if not settings.bulbs:
            self.status.set("No bulbs yet. Click “Find bulbs...” to set them up.")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(PUMP_MS, self._pump)

    # ---- threading helpers -------------------------------------------------

    def call_soon(self, fn) -> None:
        """Thread-safe: run fn on the Tk thread."""
        self._calls.put(fn)

    def _pump(self) -> None:
        try:
            while True:
                try:
                    fn = self._calls.get_nowait()
                except queue.Empty:
                    break
                fn()
            preview, self._latest_preview = self._latest_preview, None
            if self.engine is not None and preview is not None:
                if preview.frame is None:
                    self._render_tiles(preview.colours)
                else:
                    self._render(preview.frame, preview.regions, preview.colours)
                self._show_statuses(preview.statuses)
            elif self.engine is None and self._stop_thread is None and time.monotonic() >= self._next_idle_preview:
                self._next_idle_preview = time.monotonic() + IDLE_PREVIEW_S
                self._idle_preview()
        finally:
            self.after(PUMP_MS, self._pump)

    # ---- layout ------------------------------------------------------------

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Error.TLabel", foreground="#b00020")
        style.configure("Warn.TLabel", foreground="#8a4b00")
        style.configure("Link.TLabel", foreground="#1d4ed8")
        style.configure("Accent.TButton", foreground="white", background="#2563eb", padding=(14, 6))
        style.map("Accent.TButton", background=[("disabled", "#94a3b8"), ("active", "#1d4ed8")],
                  foreground=[("disabled", "#e2e8f0")])

    def _build(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        top = ttk.Frame(root)
        top.grid(row=0, column=0, sticky="ew")
        self._source = tk.StringVar(value=self.settings.source)
        for text, value in SOURCES.items():
            ttk.Radiobutton(top, text=text, value=value, variable=self._source,
                            command=self._on_source_selected).pack(side="left", padx=(0, 8))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=(0, 8))
        ttk.Label(top, text="Display").pack(side="left")
        self.monitor_box = ttk.Combobox(top, state="readonly")
        self.monitor_box.pack(side="left", fill="x", expand=True, padx=8)
        self.monitor_box.bind("<<ComboboxSelected>>", self._on_monitor_selected)
        ttk.Button(top, text="Refresh", command=self._load_monitors).pack(side="left")

        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            ttk.Label(root, style="Warn.TLabel", wraplength=PREVIEW_WIDTH, text=(
                "Wayland session detected: screen capture needs an X11 session. "
                "Log out and pick the X11/Xorg session at the login screen.")).grid(row=1, column=0, sticky="w",
                                                                                   pady=(8, 0))

        self.canvas = tk.Canvas(root, width=PREVIEW_WIDTH, height=round(PREVIEW_WIDTH * 9 / 16),
                                background="#111111", highlightthickness=0)
        self.canvas.grid(row=2, column=0, pady=10)
        self.canvas.bind("<Button-1>", self._on_canvas_click)

        self._build_bulbs(root)
        self._build_options(root)

        bottom = ttk.Frame(root)
        bottom.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(bottom, textvariable=self.status, style="Hint.TLabel", wraplength=330).pack(
            side="left", fill="x", expand=True)
        self.start_btn = ttk.Button(bottom, text="▶  Start", style="Accent.TButton", command=self._toggle)
        self.start_btn.pack(side="right")

    def _build_bulbs(self, root) -> None:
        frame = ttk.LabelFrame(root, text="Bulbs (list order sets which part of the screen each follows)",
                               padding=8)
        frame.grid(row=3, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self._checkbox = checkbox_images()
        cols = ("name", "ip", "region", "status")
        self.tree = ttk.Treeview(frame, columns=cols, show="tree headings", height=5, selectmode="browse")
        self.tree.column("#0", width=34, minwidth=34, stretch=False, anchor="center")
        for col, text, width in [("name", "Name", 110), ("ip", "IP", 100), ("region", "Region", 85),
                                 ("status", "Status", 165)]:
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=col in ("name", "status"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: self._edit_bulb())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected_status())

        side = ttk.Frame(frame)
        side.grid(row=0, column=1, sticky="n", padx=(8, 0))
        for text, cmd in [("▲ Up", lambda: self._move(-1)), ("▼ Down", lambda: self._move(1)),
                          ("Flash", self._flash), ("Colour...", self._pick_colour), ("Edit...", self._edit_bulb),
                          ("Remove", self._remove)]:
            ttk.Button(side, text=text, command=cmd, width=8).pack(fill="x", pady=1)

        actions = ttk.Frame(frame)
        actions.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(actions, text="Find bulbs...", command=self._find_bulbs).pack(side="left")
        ttk.Button(actions, text="Add manually...", command=self._add_bulb).pack(side="left", padx=6)

    def _build_options(self, root) -> None:
        frame = ttk.LabelFrame(root, text="Colour", padding=8)
        frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        frame.columnconfigure(1, weight=1)

        self._slider(frame, 0, "Saturation boost", "saturation_boost", 1.0, 2.0)
        self._slider(frame, 1, "Smoothing", "smoothing", 0.0, 0.95)
        self._slider(frame, 2, "Min brightness", "min_brightness", 0.01, 1.0)
        self._slider(frame, 3, "Max brightness", "max_brightness", 0.05, 1.0)

        right = ttk.Frame(frame)
        right.grid(row=0, column=3, rowspan=4, sticky="ne", padx=(14, 0))
        ttk.Label(right, text="Mode").grid(row=0, column=0, sticky="w")
        self._combo(right, 1, "mode", MODES)
        ttk.Label(right, text="When stopped").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._combo(right, 3, "on_stop", ON_STOP)
        ttk.Label(right, text="Bulb updates / s").grid(row=4, column=0, sticky="w", pady=(6, 0))
        rate = tk.IntVar(value=round(self.settings.max_bulb_rate))
        ttk.Spinbox(right, from_=1, to=20, textvariable=rate, width=5, state="readonly",
                    command=lambda: self._set(max_bulb_rate=float(rate.get()))).grid(row=5, column=0, sticky="w")
        ttk.Label(right, text="Apply gradient (manual)").grid(row=6, column=0, sticky="w", pady=(6, 0))
        self._gradient = ttk.Combobox(right, values=list(GRADIENTS), state="readonly", width=16)
        self._gradient.grid(row=7, column=0, sticky="w")
        self._gradient.bind("<<ComboboxSelected>>", lambda e: self._apply_gradient(self._gradient.get()))

    def _slider(self, parent, row, label, key, lo, hi) -> None:
        var = tk.DoubleVar(value=getattr(self.settings, key))
        self._vars[key] = var
        value = ttk.Label(parent, width=5, anchor="e")
        var.trace_add("write", lambda *_: value.config(text=f"{var.get():.2f}"))
        value.config(text=f"{var.get():.2f}")
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Scale(parent, from_=lo, to=hi, variable=var,
                  command=lambda _: self._set(**{key: round(var.get(), 2)})).grid(row=row, column=1, sticky="ew",
                                                                                  padx=8)
        value.grid(row=row, column=2)

    def _combo(self, parent, row, key, mapping) -> None:
        var = tk.StringVar(value=_label_for(mapping, getattr(self.settings, key)))
        box = ttk.Combobox(parent, textvariable=var, values=list(mapping), state="readonly", width=16)
        box.grid(row=row, column=0, sticky="w")
        box.bind("<<ComboboxSelected>>", lambda e: self._set(**{key: mapping[var.get()]}))

    # ---- settings ----------------------------------------------------------

    def _set(self, **changes) -> None:
        s = dataclasses.replace(self.settings, **changes)
        if s.min_brightness > s.max_brightness:
            if "min_brightness" in changes:
                s = dataclasses.replace(s, max_brightness=s.min_brightness)
                self._vars["max_brightness"].set(s.max_brightness)
            else:
                s = dataclasses.replace(s, min_brightness=s.max_brightness)
                self._vars["min_brightness"].set(s.min_brightness)
        self.settings = s
        if self.engine is not None:
            self.engine.update_settings(s)
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(500, self._save)

    def _save(self) -> None:
        self._save_job = None
        try:
            config.save(self.settings)
        except OSError as e:
            self.status.set(f"Couldn't save settings: {e}")

    # ---- display -----------------------------------------------------------

    def _load_monitors(self) -> None:
        try:
            self.monitors = list_monitors()
        except Exception as e:
            self.monitors = []
            self.status.set(f"Can't list displays: {e}")
        self.monitor_box["values"] = [m.label for m in self.monitors]
        if self.monitors:
            pos = next((i for i, m in enumerate(self.monitors) if m.index == self.settings.monitor), 0)
            self.monitor_box.current(pos)
            if self.monitors[pos].index != self.settings.monitor:
                self._set(monitor=self.monitors[pos].index)
        self._reset_idle_preview()

    def _on_monitor_selected(self, _event) -> None:
        monitor = self.monitors[self.monitor_box.current()]
        if monitor.index != self.settings.monitor:
            self._set(monitor=monitor.index)
            self._reset_idle_preview()
            self._restart_if_running()

    def _reset_idle_preview(self) -> None:
        if self._idle_grabber is not None:
            self._idle_grabber.close()
            self._idle_grabber = None
        self._next_idle_preview = 0.0

    def _idle_preview(self) -> None:
        """While stopped, show what the bulbs would do (no smoothing, nothing sent)."""
        if self.settings.source == "manual":
            self._render_tiles(manual_colours(self.settings, [b.id for b in self.settings.enabled_bulbs]))
            return
        try:
            if self._idle_grabber is None:
                self._idle_grabber = Grabber(self.settings.monitor)
            frame = self._idle_grabber.grab()
        except Exception as e:
            self._reset_idle_preview()
            self._next_idle_preview = time.monotonic() + 5
            self.status.set(f"Can't capture the screen: {e}")
            return
        regions, colours = region_colours(frame, len(self.settings.enabled_bulbs), self.settings)
        self._render(frame, regions, colours)

    def _render(self, frame, regions, colours) -> None:
        fh, fw = frame.shape[:2]
        cw, ch = PREVIEW_WIDTH, max(1, round(PREVIEW_WIDTH * fh / fw))
        if int(self.canvas["height"]) != ch:
            self.canvas.config(height=ch)
        self._photo = tk.PhotoImage(data=thumbnail_ppm(scale_frame(frame, cw, ch)), format="PPM")
        c = self.canvas
        c.delete("all")
        c.create_image(0, 0, anchor="nw", image=self._photo)
        sx, sy = cw / fw, ch / fh
        names = [b.name for b in self.settings.enabled_bulbs]
        for i, ((x0, y0, x1, y1), hsv) in enumerate(zip(regions, colours)):
            X0, Y0, X1, Y1 = x0 * sx, y0 * sy, x1 * sx - 1, y1 * sy - 1
            c.create_rectangle(X0, Y0, X1, Y1, outline="black", width=3)
            c.create_rectangle(X0, Y0, X1, Y1, outline="white", width=1)
            cx, cy = (X0 + X1) / 2, (Y0 + Y1) / 2
            c.create_rectangle(cx - 26, cy - 16, cx + 26, cy + 16, fill=hsv_hex(hsv), outline="white", width=2)
            label = names[i] if i < len(names) else "Preview"
            text = c.create_text(X0 + 8, Y0 + 6, anchor="nw", text=label, fill="white")
            bx0, by0, bx1, by1 = c.bbox(text)
            c.tag_lower(c.create_rectangle(bx0 - 4, by0 - 2, bx1 + 4, by1 + 2, fill="black", outline=""), text)

    def _render_tiles(self, colours) -> None:
        """Manual mode preview: one tile per enabled bulb, in list order. Click a tile to change its colour."""
        cw, ch = PREVIEW_WIDTH, round(PREVIEW_WIDTH * 9 / 16)
        if int(self.canvas["height"]) != ch:
            self.canvas.config(height=ch)
        c = self.canvas
        c.delete("all")
        names = [b.name for b in self.settings.enabled_bulbs]
        if not names:
            c.create_text(cw / 2, ch / 2, text="No bulbs enabled", fill="white")
            return
        width = cw / len(names)
        for i, (name, hsv) in enumerate(zip(names, colours)):
            x0 = i * width
            c.create_rectangle(x0, 0, x0 + width - 1, ch - 1, fill=hsv_hex(hsv), outline="black", width=3)
            text = c.create_text(x0 + 8, 6, anchor="nw", text=name, fill="white")
            bx0, by0, bx1, by1 = c.bbox(text)
            c.tag_lower(c.create_rectangle(bx0 - 4, by0 - 2, bx1 + 4, by1 + 2, fill="black", outline=""), text)
        c.create_text(cw / 2, ch - 10, anchor="s", text="Click a tile to change its colour", fill="white")

    # ---- manual colours ----------------------------------------------------

    def _on_source_selected(self) -> None:
        source = self._source.get()
        if source != self.settings.source:
            self._set(source=source)
            self._reset_idle_preview()
            self._restart_if_running()

    def _on_canvas_click(self, event) -> None:
        enabled = [i for i, b in enumerate(self.settings.bulbs) if b.enabled]
        if self.settings.source == "manual" and enabled:
            tile = min(int(event.x / (PREVIEW_WIDTH / len(enabled))), len(enabled) - 1)
            self._pick_colour(enabled[tile])

    def _pick_colour(self, i: int | None = None) -> None:
        from tkinter import colorchooser

        i = self._require_selection() if i is None else i
        if i is None:
            return
        bulb = self.settings.bulbs[i]
        _, chosen = colorchooser.askcolor(color=bulb.colour, parent=self, title=f"Colour for {bulb.name}")
        if not chosen:
            return
        bulbs = list(self.settings.bulbs)
        bulbs[i] = dataclasses.replace(bulb, colour=chosen.lower())
        self._set_colours(bulbs, select=i)
        if self.settings.source != "manual":
            self.status.set(f"Colour saved for {bulb.name}. Choose “Manual colours” at the top to use it.")

    def _apply_gradient(self, name: str) -> None:
        if name in GRADIENTS:
            self._set_colours(apply_gradient(self.settings.bulbs, GRADIENTS[name]))
            if self.settings.source != "manual":
                self.status.set(f"{name} applied. Choose “Manual colours” at the top to use it.")

    def _set_colours(self, bulbs, select=None) -> None:
        """Colour edits apply live: no engine restart (and so no Bluetooth password prompt)."""
        self._set(bulbs=tuple(bulbs))
        self._refresh_bulbs(select)
        self._next_idle_preview = 0.0

    # ---- bulbs -------------------------------------------------------------

    def _refresh_bulbs(self, select: int | None = None) -> None:
        if select is None:
            select = self._selected()
        self.tree.delete(*self.tree.get_children())
        n = len(self.settings.enabled_bulbs)
        k = 0
        for i, b in enumerate(self.settings.bulbs):
            if b.enabled:
                region, status = region_label(k, n), self._statuses.get(b.id, "")
                k += 1
            else:
                region, status = "—", "disabled"
            self.tree.insert("", "end", iid=str(i), image=self._checkbox[b.enabled],
                             values=(b.name, b.ip, region, short_status(status)))
        if select is not None and 0 <= select < len(self.settings.bulbs):
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def _show_statuses(self, statuses: list[str]) -> None:
        for b, status in zip(self.settings.enabled_bulbs, statuses):
            self._statuses[b.id] = status
        for i, b in enumerate(self.settings.bulbs):
            if b.enabled and self.tree.exists(str(i)):
                self.tree.set(str(i), "status", short_status(self._statuses.get(b.id, "")))

    def _show_selected_status(self) -> None:
        i = self._selected()
        if i is not None and i < len(self.settings.bulbs):
            bulb = self.settings.bulbs[i]
            if self._statuses.get(bulb.id, "") not in _HEALTHY:
                self.status.set(f"{bulb.name}: {self._statuses[bulb.id]}")

    def _selected(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _require_selection(self) -> int | None:
        i = self._selected()
        if i is None:
            self.status.set("Select a bulb in the list first.")
        return i

    def _update_bulbs(self, bulbs, select=None) -> None:
        self._set(bulbs=tuple(bulbs))
        self._refresh_bulbs(select)
        self._next_idle_preview = 0.0
        self._restart_if_running()

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "tree":
            row = self.tree.identify_row(event.y)
            if row:
                i = int(row)
                bulbs = list(self.settings.bulbs)
                bulbs[i] = dataclasses.replace(bulbs[i], enabled=not bulbs[i].enabled)
                self._update_bulbs(bulbs, select=i)
                return "break"
        return None

    def _move(self, delta: int) -> None:
        i = self._require_selection()
        if i is None or not 0 <= i + delta < len(self.settings.bulbs):
            return
        bulbs = list(self.settings.bulbs)
        bulbs[i], bulbs[i + delta] = bulbs[i + delta], bulbs[i]
        self._update_bulbs(bulbs, select=i + delta)

    def _flash(self) -> None:
        i = self._require_selection()
        if i is None:
            return
        bulb = self.settings.bulbs[i]
        self.status.set(f"Flashing {bulb.name}...")
        if self.engine is not None and self.engine.identify(bulb.id):
            return

        radio = self._beacon_radio(user_initiated=True) if bulb.is_beacon else None

        def work():
            try:
                identify_bulb(bulb, device_factory=lambda b: make_device(b, radio))
                message = f"Flashed {bulb.name}."
            except Exception as e:
                message = f"Couldn't reach {bulb.name}: {e}"
            self.call_soon(lambda: self.status.set(message))

        threading.Thread(target=work, name="flash", daemon=True).start()

    def _add_bulb(self) -> None:
        bulb = BulbDialog.ask(self)
        if bulb is None:
            return
        if any(b.id == bulb.id for b in self.settings.bulbs):
            messagebox.showerror("Screen Light", "A bulb with that device ID is already in the list.", parent=self)
            return
        self._update_bulbs([*self.settings.bulbs, bulb], select=len(self.settings.bulbs))

    def _edit_bulb(self) -> None:
        i = self._require_selection()
        if i is None:
            return
        bulb = BulbDialog.ask(self, self.settings.bulbs[i])
        if bulb is not None:
            bulbs = list(self.settings.bulbs)
            bulbs[i] = bulb
            self._update_bulbs(bulbs, select=i)

    def _remove(self) -> None:
        i = self._require_selection()
        if i is None:
            return
        bulb = self.settings.bulbs[i]
        if messagebox.askyesno("Screen Light", f"Remove {bulb.name}?", parent=self):
            bulbs = list(self.settings.bulbs)
            del bulbs[i]
            self._update_bulbs(bulbs, select=min(i, len(bulbs) - 1) if bulbs else None)

    def radio(self):
        """The session's Bluetooth radio for beacon bulbs (its helper starts, with a password prompt, on first use)."""
        if self._radio is None:
            from screenlight.beacon.radio import BeaconRadio

            self._radio = BeaconRadio(config.beacon_state_path(), ready_timeout=RADIO_READY_TIMEOUT)
        return self._radio

    def _find_bulbs(self) -> None:
        found = SetupDialog.ask(self, self.call_soon, [b.id for b in self.settings.bulbs], self.radio)
        if not found:
            return
        bulbs, added, updated = merge_into(self.settings.bulbs, found)
        self._update_bulbs(bulbs)
        self.status.set(f"Added {added} bulb(s), updated {updated}. Use Flash to check which is which.")

    # ---- start / stop ------------------------------------------------------

    def _toggle(self) -> None:
        if self._stop_thread is not None:
            return
        if self.engine is None:
            self._start_engine(user_initiated=True)
        else:
            self._stop_engine()

    def _beacon_radio(self, user_initiated: bool):
        """The session radio when Bluetooth bulbs can work here; a user action may re-ask for the password."""
        from screenlight.beacon.radio import beacon_supported

        if not beacon_supported():
            return None
        radio = self.radio()
        if user_initiated:
            radio.reset_denied()
        return radio

    def _start_engine(self, user_initiated: bool = False) -> None:
        self._reset_idle_preview()
        enabled = self.settings.enabled_bulbs
        self._statuses = {b.id: "connecting" for b in enabled}
        # Restarts after list edits reuse the radio without re-asking for a password the user just cancelled.
        radio = self._beacon_radio(user_initiated) if any(b.is_beacon for b in enabled) else None
        self.engine = SyncEngine(
            self.settings,
            on_frame=lambda preview: setattr(self, "_latest_preview", preview),
            on_error=lambda message: self.call_soon(lambda: self._engine_failed(message)),
            radio=radio,
        )
        self.engine.start()
        self.start_btn.config(text="■  Stop")
        if enabled:
            self.status.set(f"Syncing {len(enabled)} bulb(s) to display {self.settings.monitor}.")
        else:
            self.status.set("Previewing only: no bulbs are enabled.")
        self._refresh_bulbs()

    def _stop_engine(self, then=None) -> None:
        engine, self.engine = self.engine, None
        self.start_btn.config(text="Stopping...", state="disabled")

        def work():
            engine.stop()
            self.call_soon(lambda: self._stopped(then))

        self._stop_thread = threading.Thread(target=work, name="stop-engine", daemon=True)
        self._stop_thread.start()

    def _stopped(self, then) -> None:
        self._stop_thread = None
        self._statuses = {}
        self._latest_preview = None
        self._next_idle_preview = 0.0
        self.start_btn.config(text="▶  Start", state="normal")
        self.status.set("Stopped.")
        self._refresh_bulbs()
        if then:
            then()

    def _restart_if_running(self) -> None:
        if self.engine is not None:
            self._stop_engine(then=self._start_engine)

    def _engine_failed(self, message: str) -> None:
        if self.engine is not None:
            self._stop_engine(then=lambda: self.status.set(message))

    def _on_close(self) -> None:
        if self._save_job is not None:
            self.after_cancel(self._save_job)
            self._save()
        self.status.set("Stopping...")
        self.update_idletasks()
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        if self._stop_thread is not None:
            self._stop_thread.join(timeout=6)
        self._reset_idle_preview()
        if self._radio is not None:
            self._radio.close()
        self.destroy()


def run() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
        except Exception:
            pass
    App(config.load()).mainloop()
