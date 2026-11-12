"""Last-resort crash log: pythonw has no console, so uncaught exceptions
need somewhere to land besides a vanished stderr."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import traceback

LOG_PATH = Path(os.environ["LOCALAPPDATA"]) / "Chatterbox" / "error.log"


def log_crash(exc):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now(timezone.utc).isoformat()}] ")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=handle)
    except Exception:
        pass
