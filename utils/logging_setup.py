"""Per-run file logging for Atlas.

Call :func:`setup_logging` ONCE at process start, *before* importing the
``agent`` package, so every logger in the process writes to a single fresh
timestamped file under ``logs/`` (plus the console).

This replaces the old scattered ``logging.basicConfig(... 'agent_logs.txt',
mode='a')`` calls that appended every run into one ever-growing file.
"""

import logging
import os
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(_PROJECT_ROOT, "logs")

_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _prune_old_logs(keep: int) -> None:
    """Keep only the ``keep`` most recent ``atlas_*.log`` files."""
    if keep <= 0:
        return
    try:
        files = sorted(
            (
                os.path.join(LOGS_DIR, f)
                for f in os.listdir(LOGS_DIR)
                if f.startswith("atlas_") and f.endswith(".log")
            ),
            key=os.path.getmtime,
        )
        for path in files[:-keep]:
            try:
                os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def setup_logging(level: int = logging.INFO, keep_runs: int = 20) -> str:
    """Route root logging to a fresh per-run file + the console.

    Returns the path of the log file for this run.
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    _prune_old_logs(keep_runs)

    log_path = os.path.join(
        LOGS_DIR, f"atlas_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(_FORMAT)

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    logging.getLogger(__name__).info("Logging to %s", log_path)
    return log_path
