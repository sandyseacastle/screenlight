import sys


def main() -> None:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        sys.exit(
            "tkinter is missing from this Python.\n"
            "Run the app with uv (`uv run screenlight`), which uses a Python that includes it,\n"
            "or install your distribution's python3-tk package."
        )
    from screenlight.gui.app import run

    run()
