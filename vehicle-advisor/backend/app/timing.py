"""לוג זמנים קצר (perf_counter) — כדי לראות איפה הזמן הולך בתור של הבוט."""
import logging
import time
from contextlib import contextmanager

log = logging.getLogger("vehicle_advisor.timing")


@contextmanager
def timed(label: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log.info("%s: %.0f ms", label, (time.perf_counter() - t0) * 1000)
