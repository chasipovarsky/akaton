"""
החזרה אסינכרונית: POST /api/chat חוזר מיד עם pending, התור רץ ברקע, והדפדפן שואל את /state.
המודל מוחלף במודל מזויף "איטי" שמחכה ל-Event שהבדיקה משחררת — בלי Gemini ובלי sleep שרירותי.
"""
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import app.agent as agent
import app.main as main_mod
from app import store
from app.main import app
from conftest import wait_turn

client = TestClient(app)

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)


def _text(t):
    c = types.Content(role="model", parts=[types.Part(text=t)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=c)], function_calls=None, text=t)


@pytest.fixture
def slow_model(monkeypatch):
    """מודל שעונה רק אחרי release.set(). raise_with — חריגה במקום תשובה."""
    ctl = SimpleNamespace(release=threading.Event(), started=threading.Event(), raise_with=None)

    def fake_generate(contents, config, deadline=None):
        ctl.started.set()
        assert ctl.release.wait(10), "test never released the model"
        if ctl.raise_with is not None:
            raise ctl.raise_with
        return _text("תשובה מהבוט")

    monkeypatch.setattr(agent, "_generate", fake_generate)
    monkeypatch.setattr(agent, "_client", object())
    main_mod._rate_store.clear()
    yield ctl
    ctl.release.set()  # לא משאירים thread תקוע


def _session():
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    return sid


def test_post_returns_immediately_with_pending(slow_model):
    sid = _session()
    t = time.perf_counter()
    r = client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    elapsed = time.perf_counter() - t
    assert r.status_code == 200 and r.json() == {"status": "pending"}
    assert elapsed < 1.0, f"POST took {elapsed:.2f}s"

    # בזמן שהבוט "חושב": busy, וההודעה של העובדת מוצגת (שורדת רענון)
    assert slow_model.started.wait(5)
    st = client.get(f"/api/chat/{sid}/state").json()
    assert st["busy"] is True
    assert st["history"][-1] == {"role": "user", "text": "שלום"}

    # polling מגיע לתשובה
    slow_model.release.set()
    st = wait_turn(client, sid)
    assert st["busy"] is False and st["error_message"] is None
    assert st["history"][-1] == {"role": "bot", "text": "תשובה מהבוט"}
    assert st["history"][-2] == {"role": "user", "text": "שלום"}


def test_second_turn_same_session_returns_409(slow_model):
    sid = _session()
    assert client.post("/api/chat", json={"session_id": sid, "message": "ראשונה"}).status_code == 200
    r = client.post("/api/chat", json={"session_id": sid, "message": "שנייה"})
    assert r.status_code == 409
    assert r.json()["error"] == "busy" and "עדיין" in r.json()["reply"]
    # גם טעינת עובדת לדוגמה לא מחליפה פרופיל באמצע תור
    assert client.post("/api/demo/E-001", json={"session_id": sid}).status_code == 409
    slow_model.release.set()
    wait_turn(client, sid)
    # אחרי שהתור הסתיים — אפשר לשלוח שוב
    assert client.post("/api/chat", json={"session_id": sid, "message": "שלישית"}).status_code == 200
    wait_turn(client, sid)


def test_other_session_not_blocked(slow_model):
    a, b = _session(), _session()
    assert client.post("/api/chat", json={"session_id": a, "message": "א"}).status_code == 200
    assert client.post("/api/chat", json={"session_id": b, "message": "ב"}).status_code == 200
    slow_model.release.set()
    wait_turn(client, a)
    wait_turn(client, b)


def test_turn_error_returns_hebrew_error_message(slow_model):
    sid = _session()
    slow_model.raise_with = RuntimeError("INTERNAL_SECRET_TRACE")
    client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    slow_model.release.set()
    st = wait_turn(client, sid)
    assert st["busy"] is False
    assert st["error_message"] == main_mod.GENERIC_ERROR_MSG
    assert "INTERNAL_SECRET_TRACE" not in str(st)
    # ההודעה שנכשלה זמינה ל"לנסות שוב" גם אחרי רענון, אבל לא נשמרה בהיסטוריה
    assert st["pending_message"] == "שלום"
    assert all(m["text"] != "שלום" for m in st["history"])


def test_turn_timeout_returns_friendly_message(slow_model):
    sid = _session()
    slow_model.raise_with = agent.TurnTimeout()
    client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    slow_model.release.set()
    st = wait_turn(client, sid)
    assert st["error_message"] == "לא הצלחתי לסיים בזמן, אפשר לנסות שוב."


def test_error_cleared_by_next_successful_turn(slow_model):
    sid = _session()
    slow_model.raise_with = RuntimeError("x")
    client.post("/api/chat", json={"session_id": sid, "message": "1"})
    slow_model.release.set()
    assert wait_turn(client, sid)["error_message"]
    slow_model.raise_with = None
    client.post("/api/chat", json={"session_id": sid, "message": "2"})
    st = wait_turn(client, sid)
    assert st["error_message"] is None and st["pending_message"] == ""
