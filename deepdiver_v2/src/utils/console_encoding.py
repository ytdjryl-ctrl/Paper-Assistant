import os
import sys


def force_utf8_console() -> None:
    """Prefer UTF-8 for Windows console output and Python stdio streams."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
