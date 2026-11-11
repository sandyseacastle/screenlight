"""Small widgets shared by the main window and dialogs."""

import tkinter as tk


def checkbox_images() -> dict[bool, tk.PhotoImage]:
    """Drawn rather than using ☑/☐ glyphs, which many default fonts render badly."""
    off, on = tk.PhotoImage(width=14, height=14), tk.PhotoImage(width=14, height=14)
    off.put("#8a8a8a", to=(0, 0, 14, 14))
    off.put("#ffffff", to=(1, 1, 13, 13))
    on.put("#2563eb", to=(0, 0, 14, 14))
    for x, y in [(3, 6), (4, 7), (5, 8), (6, 7), (7, 6), (8, 5), (9, 4), (10, 3)]:
        on.put("#ffffff", to=(x, y, x + 2, y + 2))
    return {True: on, False: off}
