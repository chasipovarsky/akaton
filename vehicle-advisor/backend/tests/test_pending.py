"""
"בינתיים: מצב היום" והמשך אוטומטי אחרי תשובת נציג.
המודל מוחלף במודל מדומה עם תסריט קבוע — בלי קריאה ל-Gemini. הכלים, החישוב והשמירה אמיתיים.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import app.agent as agent
from app import store
from app.comparison import run_comparison, today_snapshot
from app.main import app

client = TestClient(app)

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)


def _call(name, **args):
    fc = SimpleNamespace(name=name, args=args)
    content = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name=name, args=args))])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)],
                           function_calls=[fc], text=None)


def _text(t):
    content = types.Content(role="model", parts=[types.Part(text=t)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)],
                           function_calls=None, text=t)


@pytest.fixture
def fake_model(monkeypatch):
    """תור תשובות מתוסרט למודל. מחזיר את התור כדי שהבדיקה תמלא אותו."""
    queue = []
    seen = []

    def fake_generate(contents, config):
        seen.append(contents[-1])
        return queue.pop(0)

    monkeypatch.setattr(agent, "_generate", fake_generate)
    monkeypatch.setattr(agent, "_client", object())
    import app.main as main_mod
    main_mod._rate_store.clear()
    return SimpleNamespace(queue=queue, seen=seen)


def _new_session():
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    return sid


# ── שלב 1: בינתיים, מצב היום ────────────────────────────────────────────────

def test_today_snapshot_has_no_alternative_numbers():
    snap = today_snapshot(E001, "CAR-10")
    assert snap["status"] == "pending"
    assert snap["today"]["net"] == run_comparison(E001, "CAR-10", 300)["today"]["net"]
    assert snap["alt_label"] == "ATTO 3 (2025)"
    # אין שום מספר של החלופה — לא נטו, לא השתתפות, לא הפרש
    assert "alt" not in snap and "diff" not in snap
    assert set(snap["details"]) == {"group", "budget", "today"}


def test_missing_participation_shows_today_only(fake_model):
    sid = _new_session()
    fake_model.queue += [
        _call("compare_alternative", target="CAR-10"),              # בלי השתתפות
        _call("ask_agent", question="מה ההשתתפות ב-ATTO 3 בקבוצה ג?"),
        _text("חסר לי סכום ההשתתפות, שאלתי את הנציג."),
    ]
    r = client.post("/api/chat", json={"session_id": sid, "message": "אני רוצה ATTO 3"})
    body = r.json()
    c = body["comparison"]
    assert c["status"] == "pending"
    assert c["today"]["net"] == run_comparison(E001, "CAR-10", 300)["today"]["net"]
    assert "alt" not in c and "diff" not in c
    assert body["open_questions"] == 1 and body["answers_ready"] == 0

    # תוצאת הכלי compare_alternative אמרה למודל שחסרה השתתפות — לא הומצא ערך
    tool_resp = fake_model.seen[1].parts[0].function_response.response["result"]
    assert tool_resp["status"] == "missing" and tool_resp["missing"] == "participation"


def test_compare_without_ask_agent_does_not_show_pending(fake_model):
    """אם הבוט שואל את העובדת (ולא את הנציג) — לא מציגים 'אחרי שהנציג יענה'."""
    sid = _new_session()
    fake_model.queue += [
        _call("compare_alternative", target="CAR-10"),
        _text("מה ההשתתפות שלך ברכב החדש?"),
    ]
    body = client.post("/api/chat", json={"session_id": sid, "message": "ATTO 3"}).json()
    assert body["comparison"] is None
    assert body["open_questions"] == 0


# ── שלב 2: המשך אוטומטי אחרי תשובת נציג ─────────────────────────────────────

def _ask_rep(fake_model, sid):
    fake_model.queue += [
        _call("compare_alternative", target="CAR-10"),
        _call("ask_agent", question="מה ההשתתפות ב-ATTO 3 בקבוצה ג?"),
        _text("חסר לי סכום ההשתתפות, שאלתי את הנציג."),
    ]
    client.post("/api/chat", json={"session_id": sid, "message": "אני רוצה ATTO 3"})
    return next(q for q in store.list_questions() if q["session_id"] == sid)["id"]


def test_comparison_appears_after_rep_answer_without_new_message(fake_model):
    sid = _new_session()
    qid = _ask_rep(fake_model, sid)

    # לפני התשובה: יש שאלה פתוחה, ו-resume לא קורא למודל
    st = client.get(f"/api/chat/{sid}/state").json()
    assert st["open_questions"] == 1 and st["answers_ready"] == 0
    calls_before = len(fake_model.seen)
    r = client.post("/api/chat/resume", json={"session_id": sid}).json()
    assert r["resumed"] is False and len(fake_model.seen) == calls_before

    # הנציגה עונה במסך שלה
    assert store.answer_question(qid, "השתתפות 350 ₪")
    st = client.get(f"/api/chat/{sid}/state").json()
    assert st["open_questions"] == 0 and st["answers_ready"] == 1

    # המסך של העובדת קורא ל-resume לבד — בלי הודעה מהעובדת
    fake_model.queue += [
        _call("compare_alternative", target="CAR-10", participation=350),
        _text("קיבלתי תשובה מהנציג. הנה ההשוואה."),
    ]
    r = client.post("/api/chat/resume", json={"session_id": sid}).json()
    assert r["resumed"] is True
    ref = run_comparison(E001, "CAR-10", 350)
    assert r["comparison"]["status"] == "ok"
    assert r["comparison"]["alt"]["net"] == ref["alt"]["net"]
    assert r["comparison"]["diff"] == ref["diff"]
    assert r["open_questions"] == 0 and r["answers_ready"] == 0

    # תשובת הנציג הגיעה למודל, וההודעה של העובדת עצמה ריקה
    resume_msg = fake_model.seen[calls_before].parts[0].text
    assert "[תשובת נציג" in resume_msg and "השתתפות 350" in resume_msg

    # אחרי רענון: ההשוואה המלאה נשמרה
    st = client.get(f"/api/chat/{sid}/state").json()
    assert st["comparison"]["status"] == "ok"


def test_rep_answer_not_lost_when_model_fails(fake_model, monkeypatch):
    sid = _new_session()
    qid = _ask_rep(fake_model, sid)
    store.answer_question(qid, "350")

    def boom(contents, config):
        raise RuntimeError("model down")

    monkeypatch.setattr(agent, "_generate", boom)
    r = client.post("/api/chat/resume", json={"session_id": sid}).json()
    assert "error" not in r
    # התשובה חזרה למצב "מחכה" — הבדיקה הבאה תנסה שוב
    st = client.get(f"/api/chat/{sid}/state").json()
    assert st["answers_ready"] == 1


def test_resume_invalid_session_id():
    r = client.post("/api/chat/resume", json={"session_id": "bad id!"})
    assert r.status_code == 400
