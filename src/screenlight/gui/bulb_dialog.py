"""Add or edit a bulb by hand."""

import dataclasses

import tkinter as tk
from tkinter import ttk

from screenlight.config import Bulb

VERSIONS = ("3.1", "3.2", "3.3", "3.4", "3.5")


class BulbDialog(tk.Toplevel):
    @classmethod
    def ask(cls, parent, bulb: Bulb | None = None) -> Bulb | None:
        dialog = cls(parent, bulb)
        parent.wait_window(dialog)
        return dialog.result

    def __init__(self, parent, bulb: Bulb | None):
        super().__init__(parent)
        self.title("Edit bulb" if bulb else "Add bulb")
        self.transient(parent)
        self.resizable(False, False)
        self.result: Bulb | None = None
        self._original = bulb or Bulb(name="", id="")

        body = ttk.Frame(self, padding=14)
        body.grid(sticky="nsew")
        body.columnconfigure(1, weight=1)

        self._vars: dict[str, tk.StringVar] = {}
        fields = [("Name", "name"), ("Device ID", "id"), ("IP address", "ip"), ("Local key", "key")]
        for row, (label, key) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            var = tk.StringVar(value=getattr(self._original, key))
            self._vars[key] = var
            entry = ttk.Entry(body, textvariable=var, width=38)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            if row == 0:
                entry.focus_set()

        ttk.Label(body, text="Protocol version").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=3)
        self._version = tk.StringVar(value=str(self._original.version))
        ttk.Combobox(body, textvariable=self._version, values=VERSIONS, width=6,
                     state="readonly").grid(row=4, column=1, sticky="w", pady=3)

        ttk.Label(body, text='Use "Auto" as the IP address to find the bulb on your network automatically.',
                  style="Hint.TLabel", wraplength=340).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._error = ttk.Label(body, style="Error.TLabel")
        self._error.grid(row=6, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.wait_visibility()
        self.grab_set()

    def _save(self) -> None:
        values = {k: v.get().strip() for k, v in self._vars.items()}
        if not values["id"] or not values["key"]:
            self._error.config(text="Device ID and local key are required.")
            return
        self.result = edited_bulb(self._original, values, float(self._version.get()))
        self.destroy()


def edited_bulb(original: Bulb, values: dict, version: float) -> Bulb:
    """Apply the dialog's fields, keeping everything else (enabled, Bluetooth identity, colour...)."""
    changes = {"name": values["name"] or values["id"], "id": values["id"], "key": values["key"]}
    if original.is_beacon:
        if values["key"] != original.key:
            changes["beacon_key"] = ""  # new local key: the old beacon key is stale, Bluetooth setup must run again
    else:
        changes.update(ip=values["ip"] or "Auto", version=version)
    return dataclasses.replace(original, **changes)
