"""The "Find bulbs..." wizard: fetch local keys from the Tuya cloud once, then find the bulbs on the LAN."""

import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from screenlight.config import Bulb
from screenlight.beacon.radio import beacon_supported
from screenlight.discovery import REGIONS, Candidate, fetch_cloud_devices, merge_devices, scan_lan
from screenlight.gui.beacon_dialog import BeaconDialog
from screenlight.gui.common import checkbox_images

GUIDE_URL = "https://github.com/jasonacox/tinytuya#setup-wizard---getting-local-keys"

INSTRUCTIONS = (
    "Tuya bulbs need a per-device local key before they can be controlled over Wi-Fi. "
    "You only have to do this once (and again if you re-pair a bulb):\n\n"
    "1. Pair the bulbs in the Smart Life or Tuya Smart app as normal.\n"
    "2. Create a free account at iot.tuya.com, then Cloud → Development → Create Cloud Project "
    "(Smart Home, and the same data centre as your app account).\n"
    "3. In the project open Devices → Link App Account and scan the QR code with the app.\n"
    "4. Copy the Access ID and Access Secret from the project overview into the fields below.\n\n"
    "The cloud keys are only used now and are not saved. Keep this computer on the same network as the bulbs."
)


class SetupDialog(tk.Toplevel):
    @classmethod
    def ask(cls, parent, call_soon, existing_ids, radio_provider=None) -> list[Bulb]:
        """radio_provider returns the app's BeaconRadio; without it, Bluetooth setup isn't offered."""
        dialog = cls(parent, call_soon, set(existing_ids), radio_provider)
        parent.wait_window(dialog)
        return dialog.result

    def __init__(self, parent, call_soon, existing_ids: set[str], radio_provider=None):
        super().__init__(parent)
        self.title("Find bulbs")
        self.transient(parent)
        self.minsize(560, 0)
        self.result: list[Bulb] = []
        self._call_soon = call_soon
        self._existing = existing_ids
        self._candidates: list[Candidate] = []
        self._checked: list[bool] = []
        self._radio_provider = radio_provider if beacon_supported() else None
        self._beacon: dict[int, Bulb] = {}  # row -> Bluetooth setup result
        self._current: int | None = None

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(8, weight=1)

        ttk.Label(body, text=INSTRUCTIONS, wraplength=540, justify="left").grid(row=0, column=0, columnspan=3,
                                                                                 sticky="w")
        link = ttk.Label(body, text="Step-by-step guide with screenshots ↗", style="Link.TLabel", cursor="hand2")
        link.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))
        link.bind("<Button-1>", lambda e: webbrowser.open(GUIDE_URL))

        self._access_id = tk.StringVar()
        self._secret = tk.StringVar()
        self._device_id = tk.StringVar()
        self._region = tk.StringVar(value=self._region_label("eu"))
        rows = [("Access ID", ttk.Entry(body, textvariable=self._access_id)),
                ("Access Secret", ttk.Entry(body, textvariable=self._secret, show="•")),
                ("Data centre", ttk.Combobox(body, textvariable=self._region, state="readonly",
                                             values=[self._region_label(r) for r in REGIONS])),
                ("Any device ID (optional)", ttk.Entry(body, textvariable=self._device_id))]
        for i, (label, widget) in enumerate(rows, start=2):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
            widget.grid(row=i, column=1, columnspan=2, sticky="ew", pady=3)
        rows[0][1].focus_set()

        action = ttk.Frame(body)
        action.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        action.columnconfigure(1, weight=1)
        self._fetch_btn = ttk.Button(action, text="Fetch & scan", command=self._fetch)
        self._fetch_btn.grid(row=0, column=0)
        self._progress = ttk.Progressbar(action, mode="indeterminate", length=120)
        self._status = tk.StringVar()
        ttk.Label(action, textvariable=self._status, style="Hint.TLabel", wraplength=380).grid(
            row=0, column=1, sticky="w", padx=10)
        self._progress_cell = (0, 2)

        ttk.Label(body, text="Devices in your account (click a row to tick or untick it)").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(6, 2))
        self._checkbox = checkbox_images()
        cols = ("name", "type", "ip", "version")
        self._tree = ttk.Treeview(body, columns=cols, show="tree headings", height=7, selectmode="none")
        self._tree.column("#0", width=34, minwidth=34, stretch=False)
        for col, text, width, anchor in [("name", "Name", 170, "w"), ("type", "Type", 140, "w"),
                                         ("ip", "IP address", 130, "w"), ("version", "Ver", 50, "center")]:
            self._tree.heading(col, text=text, anchor=anchor)
            self._tree.column(col, width=width, anchor=anchor, stretch=col == "name")
        self._tree.grid(row=8, column=0, columnspan=3, sticky="nsew")
        self._tree.bind("<Button-1>", self._on_click)

        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        self._add_btn = ttk.Button(buttons, text="Add selected", command=self._add, state="disabled")
        self._add_btn.pack(side="right", padx=(0, 6))
        self._bt_btn = ttk.Button(buttons, text="Set up as Bluetooth...", command=self._setup_bluetooth,
                                  state="disabled")
        if self._radio_provider is not None:
            self._bt_btn.pack(side="left")

        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_visibility()
        self.grab_set()

    @staticmethod
    def _region_label(code: str) -> str:
        return f"{code} — {REGIONS[code]}"

    def _safe(self, fn):
        """Run fn on the Tk thread, unless the dialog was closed while the worker ran."""
        def call():
            if self.winfo_exists():
                fn()
        self._call_soon(call)

    def _busy(self, busy: bool, message: str) -> None:
        self._status.set(message)
        self._fetch_btn.config(state="disabled" if busy else "normal")
        if busy:
            self._progress.grid(row=self._progress_cell[0], column=self._progress_cell[1])
            self._progress.start(12)
        else:
            self._progress.stop()
            self._progress.grid_remove()

    def _fetch(self) -> None:
        access_id, secret = self._access_id.get().strip(), self._secret.get().strip()
        if not access_id or not secret:
            self._status.set("Enter the Access ID and Access Secret first.")
            return
        region = self._region.get().split(" — ")[0]
        device_id = self._device_id.get().strip() or None
        self._busy(True, "Contacting the Tuya cloud...")

        def work():
            try:
                cloud = fetch_cloud_devices(region, access_id, secret, device_id)
                self._safe(lambda: self._status.set(
                    f"Found {len(cloud)} device(s) in your account. Scanning your network (about 20 s)..."))
                candidates = merge_devices(cloud, scan_lan(cloud))
                self._safe(lambda: self._show(candidates))
            except Exception as e:
                message = f"Failed: {e}"
                self._safe(lambda: self._busy(False, message))

        threading.Thread(target=work, name="find-bulbs", daemon=True).start()

    def _show(self, candidates: list[Candidate]) -> None:
        self._candidates = candidates
        self._checked = [c.is_light for c in candidates]
        self._beacon, self._current = {}, None
        self._bt_btn.config(state="disabled")
        self._tree.delete(*self._tree.get_children())
        for i, c in enumerate(candidates):
            name = c.bulb.name + (" (update)" if c.bulb.id in self._existing else "")
            ip = c.bulb.ip if c.on_lan else "not seen (Auto)"
            self._tree.insert("", "end", iid=str(i), image=self._checkbox[self._checked[i]],
                              values=(name, c.product or c.category, ip, c.bulb.version))
        lights = sum(self._checked)
        if not candidates:
            message = "No devices found. Check the data centre and that the app account is linked."
        else:
            message = f"{lights} light(s) ticked. Bulbs marked (update) get their new key and IP."
        self._busy(False, message)
        self._add_btn.config(state="normal" if candidates else "disabled")

    def _on_click(self, event) -> None:
        row = self._tree.identify_row(event.y)
        if row:
            i = int(row)
            self._checked[i] = not self._checked[i]
            self._tree.item(row, image=self._checkbox[self._checked[i]])
            self._current = i
            c = self._candidates[i]
            can_bluetooth = c.is_light and not c.on_lan and i not in self._beacon
            self._bt_btn.config(state="normal" if can_bluetooth else "disabled")

    def _setup_bluetooth(self) -> None:
        i = self._current
        if i is None or self._radio_provider is None:
            return
        bulb = BeaconDialog.ask(self, self._call_soon, self._radio_provider(), self._candidates[i])
        if self.winfo_exists():
            self.grab_set()
        if bulb is None:
            return
        self._beacon[i] = bulb
        self._checked[i] = True
        self._tree.item(str(i), image=self._checkbox[True])
        self._tree.set(str(i), "ip", "Bluetooth ✓")
        self._tree.set(str(i), "version", "BT")
        self._bt_btn.config(state="disabled")

    def _add(self) -> None:
        self.result = [self._beacon.get(i, c.bulb)
                       for i, (c, ticked) in enumerate(zip(self._candidates, self._checked)) if ticked]
        self.destroy()
