"""
בדיקות "עובדת לדוגמה" — טעינת פרופיל מוכן לשיחה, בלי קריאה ל-Gemini.
"""
import uuid

from fastapi.testclient import TestClient

from app import store
from app.agent import REQUIRED
from app.comparison import rank_options
from app.config import load_demo_employees
from app.main import app

client = TestClient(app)


def test_demo_list():
    r = client.get("/api/demo/employees")
    assert r.status_code == 200
    rows = r.json()
    assert [e["id"] for e in rows] == ["E-001", "E-002", "E-003", "E-004", "E-005", "E-006"]
    assert rows[1]["title"] == "קבוצה ב', 9.8 אלף ₪"
    # רק מזהה וכותרת — בלי שם, ת"ז או נתוני שכר מלאים
    assert all(set(e) == {"id", "title"} for e in rows)


def test_demo_profiles_complete():
    for e in load_demo_employees():
        p = e["profile"]
        assert not [k for k in REQUIRED if k not in p], e["id"]
        assert "current_car_id" in p or "current_maintenance" in p, e["id"]


def test_demo_invalid_session_id():
    r = client.post("/api/demo/E-001", json={"session_id": "bad id!"})
    assert r.status_code == 400


def test_demo_unknown_employee():
    r = client.post("/api/demo/E-999", json={"session_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_demo_load_then_rank():
    sid = str(uuid.uuid4())
    r = client.post("/api/demo/E-002", json={"session_id": sid})
    assert r.status_code == 200
    history = r.json()["history"]
    assert history[-1]["role"] == "bot"
    assert "מה הכי משתלם לי?" in history[-1]["text"]

    profile, _ = store.load_session(sid)
    expected = next(e for e in load_demo_employees() if e["id"] == "E-002")["profile"]
    assert profile == expected

    body = client.post("/api/rank", json={"session_id": sid}).json()
    assert body["status"] == "ok"
    assert body["options"] == rank_options(expected)["options"]


def test_demo_load_replaces_previous_profile():
    sid = str(uuid.uuid4())
    client.post("/api/demo/E-001", json={"session_id": sid})
    client.post("/api/demo/E-003", json={"session_id": sid})
    profile, _ = store.load_session(sid)
    assert profile["current_maintenance"] == 1500
    assert "current_car_id" not in profile
    assert "_last_comparison" not in profile
    # שתי הטעינות נשמרות בהיסטוריה
    assert len(store.display_history(sid)) == 4
