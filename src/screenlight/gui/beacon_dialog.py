"""Set up a Tuya Bluetooth beacon bulb: catch the phone's OFF/ON commands and derive the bulb's beacon key."""

import threading
import tkinter as tk
from tkinter import ttk

from screenlight.beacon.protocol import derive_from_off_on
from screenlight.beacon.radio import RadioError
from screenlight.bulbs import BEACON_STATUS
from screenlight.config import Bulb

LISTEN_SECONDS = 60
CHUNK_SECONDS = 5


def listen_for_off_on(radio, local_key: str, seconds: float = LISTEN_SECONDS, chunk: float = CHUNK_SECONDS,
                      cancelled=lambda: False) -> tuple[bytes, int] | None:
    """Capture in short chunks until the bulb's OFF then ON commands are seen; (beacon key, node ID) or None."""
    radio.reset_denied()  # Listen is a user action: ask for the password again even if it was cancelled before
    frames: list[bytes] = []
    found = None

    def on_frame(frame: bytes) -> None:
        nonlocal found
        frames.append(frame)
        found = found or derive_from_off_on(frames, local_key)

    listened = 0.0
    while listened < seconds and found is None and not cancelled():
        radio.capture(chunk, on_frame)
        listened += chunk
    return found


class BeaconDialog(tk.Toplevel):
    @classmethod
    def ask(cls, parent, call_soon, radio, candidate) -> Bulb | None:
        dialog = cls(parent, call_soon, radio, candidate)
        parent.wait_window(dialog)
        return dialog.result

    def __init__(self, parent, call_soon, radio, candidate):
        super().__init__(parent)
        self.title("Set up Bluetooth bulb")
        self.transient(parent)
        self.resizable(False, False)
        self.result: Bulb | None = None
        self._call_soon = call_soon
        self._radio = radio
        self._candidate = candidate
        self._found: Bulb | None = None
        self._closed = False
        name = candidate.bulb.name

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        ttk.Label(body, wraplength=380, justify="left", text=(
            f"Press Listen, then in Smart Life turn {name} OFF, then ON. "
            "Keep your phone near this computer.")).grid(row=0, column=0, columnspan=2, sticky="w")
        self._status = tk.StringVar()
        ttk.Label(body, textvariable=self._status, style="Hint.TLabel", wraplength=380).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._progress = ttk.Progressbar(body, mode="indeterminate", length=380)
        self._progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._progress.grid_remove()

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._close).pack(side="right")
        self._add_btn = ttk.Button(buttons, text="Add", command=self._add, state="disabled")
        self._add_btn.pack(side="right", padx=(0, 6))
        self._listen_btn = ttk.Button(buttons, text="Listen", command=self._listen)
        self._listen_btn.pack(side="right", padx=(0, 6))

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())
        self.wait_visibility()
        self.grab_set()

    def _safe(self, fn) -> None:
        def call():
            if not self._closed and self.winfo_exists():
                fn()
        self._call_soon(call)

    def _listen(self) -> None:
        self._listen_btn.config(state="disabled")
        self._status.set("Listening... (enter your password if asked)")
        self._progress.grid()
        self._progress.start(12)
        bulb = self._candidate.bulb

        def work():
            try:
                found = listen_for_off_on(self._radio, bulb.key, cancelled=lambda: self._closed)
                self._safe(lambda: self._done(found))
            except RadioError as e:
                message = BEACON_STATUS.get(e.code, str(e))
                self._safe(lambda: self._failed(message))

        threading.Thread(target=work, name="beacon-listen", daemon=True).start()

    def _stop_progress(self) -> None:
        self._progress.stop()
        self._progress.grid_remove()
        self._listen_btn.config(state="normal", text="Try again")

    def _done(self, found) -> None:
        self._stop_progress()
        if found is None:
            self._status.set("Didn't catch it. Check your phone's Bluetooth is on and it's near this computer, "
                             "then try again.")
            return
        beacon_key, node_id = found
        bulb = self._candidate.bulb
        self._found = Bulb(name=bulb.name, id=bulb.id, ip="Bluetooth", key=bulb.key, kind="beacon",
                           node_id=node_id, beacon_key=beacon_key.hex())
        self._status.set(f"Found {bulb.name} (node {node_id:04X}).")
        self._listen_btn.config(state="disabled")
        self._add_btn.config(state="normal")

    def _failed(self, message: str) -> None:
        self._stop_progress()
        self._status.set(f"Couldn't listen: {message}.")

    def _add(self) -> None:
        self.result = self._found
        self._close()

    def _close(self) -> None:
        self._closed = True
        self.destroy()
