"""
תורים של הבוט ברקע: POST /api/chat חוזר מיד, והדפדפן שואל את /state עד שהתור מסתיים.
המצב (busy / שגיאה אחרונה / ההודעה שבטיפול) נשמר בזיכרון, מאחורי נעילה.
מתאים לשרת עם תהליך אחד (כמו ב-Dockerfile); אחרי הפעלה מחדש תורים שרצו פשוט נעלמים.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

log = logging.getLogger("vehicle_advisor")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bot-turn")
_lock = threading.Lock()
# sid → {"busy": bool, "error": str | None, "message": str, "started": float}
_state: dict[str, dict] = {}


def start(sid: str, message: str, fn: Callable[[], None],
          on_error: Callable[[Exception], str]) -> bool:
    """מפעיל תור ברקע. False אם כבר רץ תור לאותה שיחה (בדיקה והפעלה באותה נעילה)."""
    with _lock:
        if _state.get(sid, {}).get("busy"):
            return False
        _state[sid] = {"busy": True, "error": None, "message": message,
                       "started": time.monotonic()}
    _executor.submit(_run, sid, fn, on_error)
    return True


def _run(sid: str, fn: Callable[[], None], on_error: Callable[[Exception], str]) -> None:
    error = None
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — כל כשל הופך להודעה ידידותית
        try:
            error = on_error(e)
        except Exception:  # noqa: BLE001
            log.exception("turn error handler failed")
            error = "אירעה תקלה זמנית. אפשר לנסות שוב בעוד רגע."
    finally:
        with _lock:
            if error is None:
                _state.pop(sid, None)  # הצליח — אין מה לזכור
            else:
                st = _state.setdefault(sid, {})
                st["busy"] = False
                st["error"] = error


def status(sid: str) -> dict:
    """busy, error_message (רק כשהתור נכשל), ו-pending_message (ההודעה שבטיפול / שנכשלה)."""
    with _lock:
        st = dict(_state.get(sid, {}))
    busy = bool(st.get("busy"))
    return {
        "busy": busy,
        "error_message": None if busy else st.get("error"),
        "pending_message": st.get("message", "") if (busy or st.get("error")) else "",
    }


def is_busy(sid: str) -> bool:
    with _lock:
        return bool(_state.get(sid, {}).get("busy"))


def shutdown() -> None:
    _executor.shutdown(wait=False, cancel_futures=True)
