"""
נקודות הקצה של הפולינג (/state, resume בלי תשובה שמחכה, תיבת הנציג, בורר הרכבים) לא כפופות
ל-rate limit — גם כשהמכסה של /api/chat כבר נוצלה. ו-GEMINI_CALL_TIMEOUT ממשתנה סביבה.
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app.agent as agent
import app.main as main_mod
from app import store
from app.main import app

client = TestClient(app)

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)


@pytest.fixture
def exhausted_rate_limit():
    """המכסה של כתובת הבדיקה (testclient) מלאה: עוד POST /api/chat יחזיר 429."""
    main_mod._rate_store.clear()
    main_mod._rate_store["testclient"] = [time.time()] * main_mod._RATE_LIMIT
    yield
    main_mod._rate_store.clear()


def test_60_polls_in_a_minute_never_429(exhausted_rate_limit):
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    polls = [
        lambda: client.get(f"/api/chat/{sid}/state"),
        lambda: client.post("/api/chat/resume", json={"session_id": sid}),  # אין תשובת נציג שמחכה
        lambda: client.get("/api/agent/questions", params={"open_only": True}),
        lambda: client.get("/api/cars", params={"session_id": sid}),
    ]
    t0 = time.monotonic()
    for poll in polls:
        codes = [poll().status_code for _ in range(60)]
        assert codes == [200] * 60
    assert time.monotonic() - t0 < 60  # 60 קריאות רצופות לכל נקודה, בתוך דקה
    # הפולינג גם לא "צורך" מהמכסה — היא נשארה בדיוק כמו שהייתה
    assert len(main_mod._rate_store["testclient"]) == main_mod._RATE_LIMIT
    # ביקורת: אותה כתובת באמת חסומה ב-/api/chat
    assert client.post("/api/chat", json={"session_id": sid, "message": "שלום"}).status_code == 429


@pytest.mark.parametrize("raw, expected", [
    (None, 40), ("", 40), ("15", 15), ("22.5", 22.5), ("abc", 40), ("0", 40), ("-3", 40),
])
def test_call_timeout_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("GEMINI_CALL_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("GEMINI_CALL_TIMEOUT", raw)
    assert agent._call_timeout_from_env() == expected


def test_tests_run_with_default_timeout():
    assert agent.CALL_TIMEOUT_S == agent.DEFAULT_CALL_TIMEOUT_S == 40
