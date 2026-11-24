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


class TurnStats:
    """סיכום תור אחד של הבוט: סבבי Gemini, זמן כל קריאה וכל כלי, וטוקנים (usage_metadata)."""

    def __init__(self):
        self.t0 = time.perf_counter()
        self.calls: list[dict] = []   # {"model", "ms", "prompt", "cached", "output", "thoughts"}
        self.tools: list[tuple[str, float]] = []

    def add_call(self, model: str, ms: float, resp) -> None:
        u = getattr(resp, "usage_metadata", None)
        tok = lambda name: (getattr(u, name, None) or 0) if u is not None else 0  # noqa: E731
        self.calls.append({"model": model, "ms": ms,
                           "prompt": tok("prompt_token_count"),
                           "cached": tok("cached_content_token_count"),
                           "output": tok("candidates_token_count"),
                           "thoughts": tok("thoughts_token_count")})

    def add_tool(self, name: str, ms: float) -> None:
        self.tools.append((name, ms))

    def summary(self) -> dict:
        s = lambda k: sum(c[k] for c in self.calls)  # noqa: E731
        return {"rounds": len(self.calls),
                "total_ms": round((time.perf_counter() - self.t0) * 1000),
                "gemini_ms": round(s("ms")), "tools_ms": round(sum(ms for _, ms in self.tools)),
                "calls_ms": [round(c["ms"]) for c in self.calls],
                "tools": [f"{n}={ms:.0f}" for n, ms in self.tools],
                "prompt_tokens": s("prompt"), "cached_tokens": s("cached"),
                "output_tokens": s("output"), "thought_tokens": s("thoughts")}

    def log(self, outcome: str) -> None:
        log.info("turn %s: %s", outcome, self.summary())
