import time

import pytest

import app.agent as agent


@pytest.fixture(autouse=True)
def default_call_timeout(monkeypatch):
    """הבדיקות רצות עם ברירת המחדל של GEMINI_CALL_TIMEOUT, בלי קשר להגדרה בסביבה."""
    monkeypatch.setattr(agent, "CALL_TIMEOUT_S", agent.DEFAULT_CALL_TIMEOUT_S)
    # כל בדיקה מתחילה בלי מודלים "מושהים" מבדיקה קודמת
    monkeypatch.setattr(agent, "_cooldown", {})


def wait_turn(client, sid: str, timeout: float = 10.0) -> dict:
    """POST /api/chat חוזר מיד; מחכים (polling כמו הדפדפן) עד שהתור ברקע מסתיים."""
    end = time.monotonic() + timeout
    while True:
        st = client.get(f"/api/chat/{sid}/state").json()
        if not st["busy"]:
            return st
        if time.monotonic() > end:
            raise AssertionError(f"turn still busy after {timeout}s")
        time.sleep(0.02)


@pytest.fixture
def wait():
    return wait_turn
