"""
זמני קצבה ל-Gemini: timeout לקריאה, תקרה לתור, מגבלת סבבי כלים, ומעבר למודל גיבוי רק בשגיאת מכסה/עומס.
בלי רשת — ה-client מוחלף במזויף.
"""
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types

import app.agent as agent
from app import store

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)

CONFIG = types.GenerateContentConfig(temperature=0)


class APIErr(Exception):
    def __init__(self, code):
        super().__init__(f"{code} error")
        self.code = code


def _fake_client(monkeypatch, behaviour):
    """behaviour(model, config) -> תשובה או חריגה. מחזיר רשימת הקריאות."""
    calls = []

    def generate_content(model, contents, config):
        calls.append((model, config))
        return behaviour(model, config)

    monkeypatch.setattr(agent, "_client", SimpleNamespace(models=SimpleNamespace(
        generate_content=generate_content)))
    monkeypatch.setattr(agent, "MODELS", ["main-model", "backup-model"])
    return calls


def _raise(e):
    raise e


def test_timeout_raises_turn_timeout_without_backup(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: _raise(httpx.ReadTimeout("slow")))
    with pytest.raises(agent.TurnTimeout):
        agent._generate([], CONFIG, time.monotonic() + 90)
    assert [m for m, _ in calls] == ["main-model"]  # לא עוברים לגיבוי על timeout


@pytest.mark.parametrize("code", [429, 503, 404])
def test_backup_model_on_quota_or_overload(monkeypatch, code):
    calls = _fake_client(monkeypatch, lambda m, c: _raise(APIErr(code)) if m == "main-model" else "ok")
    assert agent._generate([], CONFIG, time.monotonic() + 90) == "ok"
    assert [m for m, _ in calls] == ["main-model", "backup-model"]


def test_no_backup_on_other_errors(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: _raise(APIErr(400)))
    with pytest.raises(APIErr):
        agent._generate([], CONFIG, time.monotonic() + 90)
    assert len(calls) == 1


def test_per_call_timeout_capped_by_turn_deadline(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: "ok")
    agent._generate([], CONFIG, time.monotonic() + 90)
    assert calls[-1][1].http_options.timeout == agent.CALL_TIMEOUT_S * 1000
    agent._generate([], CONFIG, time.monotonic() + 10)
    assert calls[-1][1].http_options.timeout <= 10_000


def test_expired_deadline_does_not_call_model(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: "ok")
    with pytest.raises(agent.TurnTimeout):
        agent._generate([], CONFIG, time.monotonic() - 1)
    assert calls == []


def test_tool_round_limit(monkeypatch):
    """מודל שמבקש כלים בלי סוף — עוצרים אחרי 6 סבבים, בלי לשמור חצי תור."""
    n = {"calls": 0}

    def endless(contents, config, deadline=None):
        n["calls"] += 1
        c = types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(name="list_cars", args={}))])
        return SimpleNamespace(candidates=[SimpleNamespace(content=c)],
                               function_calls=[SimpleNamespace(name="list_cars", args={})], text=None)

    monkeypatch.setattr(agent, "_generate", endless)
    monkeypatch.setattr(agent, "_client", object())
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    with pytest.raises(agent.TurnTimeout):
        agent.chat(sid, "שלום")
    assert n["calls"] == agent.MAX_TOOL_ROUNDS + 1
    _, history = store.load_session(sid)
    assert history == []  # לא נשמר חצי תור


def test_turn_deadline(monkeypatch):
    """תור שעובר את תקרת הזמן הכוללת נעצר עם TurnTimeout."""
    monkeypatch.setattr(agent, "TURN_TIMEOUT_S", 0.5)

    def slow_tools(contents, config, deadline=None):
        if deadline is not None and time.monotonic() > deadline:
            raise agent.TurnTimeout()
        time.sleep(0.3)
        c = types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(name="list_cars", args={}))])
        return SimpleNamespace(candidates=[SimpleNamespace(content=c)],
                               function_calls=[SimpleNamespace(name="list_cars", args={})], text=None)

    monkeypatch.setattr(agent, "_generate", slow_tools)
    monkeypatch.setattr(agent, "_client", object())
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    t = time.monotonic()
    with pytest.raises(agent.TurnTimeout):
        agent.chat(sid, "שלום")
    assert time.monotonic() - t < 2


# ── הערך האפקטיבי של ה-timeout (דרך הספרייה עצמה, לא דרך מודל מזויף) ─────────

from google.genai import errors  # noqa: E402


def _built_request(client, http_options):
    """בונה את בקשת ה-HTTP כמו שהספרייה המותקנת בונה אותה — בלי לשלוח."""
    return client._api_client._build_request(
        "post", "models/x:generateContent", {}, http_options=http_options)


def test_client_created_with_timeout_in_milliseconds(monkeypatch):
    """chat() יוצר client עם 60000 ms — והספרייה מתרגמת ל-60 שניות ול-X-Server-Timeout: 60."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(agent, "_client", None)
    monkeypatch.setattr(agent, "_generate", lambda contents, config, deadline=None: SimpleNamespace(
        candidates=[SimpleNamespace(content=types.Content(role="model", parts=[types.Part(text="ok")]))],
        function_calls=None, text="ok"))
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    agent.chat(sid, "שלום")

    opts = agent._client._api_client._http_options
    assert opts.timeout == 60_000            # אלפיות שנייה — לא 60
    assert opts.retry_options is None        # בלי retries של הספרייה
    req = _built_request(agent._client, None)
    assert req.timeout == 60.0               # httpx מקבל שניות
    assert req.headers["X-Server-Timeout"] == "60"


def test_per_call_timeout_effective_and_not_sticky(monkeypatch):
    """ה-http_options ש-_generate מעביר בכל קריאה — מה שהספרייה באמת שולחת."""
    from google import genai
    real = genai.Client(api_key="test-key-not-real",
                        http_options=types.HttpOptions(timeout=60_000, retry_options=None))
    calls = _fake_client(monkeypatch, lambda m, c: "ok")

    agent._generate([], CONFIG, time.monotonic() + 90)
    req = _built_request(real, calls[-1][1].http_options)
    assert calls[-1][1].http_options.timeout == 60_000
    assert req.timeout == 60.0 and req.headers["X-Server-Timeout"] == "60"

    # קרוב לסוף התור: קריאה קצרה יותר — והכותרת מתעדכנת (לא נשארת מהקריאה הקודמת)
    agent._generate([], CONFIG, time.monotonic() + 10)
    req = _built_request(real, calls[-1][1].http_options)
    assert 9.0 <= req.timeout <= 10.0
    assert req.headers["X-Server-Timeout"] == "10"
    # ושוב קריאה מלאה — חוזר ל-60
    agent._generate([], CONFIG, time.monotonic() + 90)
    assert _built_request(real, calls[-1][1].http_options).headers["X-Server-Timeout"] == "60"


# ── 504 DEADLINE_EXCEEDED ───────────────────────────────────────────────────

def _deadline_exceeded():
    return errors.ServerError(504, {"error": {
        "code": 504, "message": "Deadline expired before operation could complete.",
        "status": "DEADLINE_EXCEEDED"}})


def test_504_falls_back_to_backup_model(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: _raise(_deadline_exceeded()) if m == "main-model" else "ok")
    assert agent._generate([], CONFIG, time.monotonic() + 90) == "ok"
    assert [m for m, _ in calls] == ["main-model", "backup-model"]


def test_504_on_all_models_becomes_turn_timeout(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: _raise(_deadline_exceeded()))
    with pytest.raises(agent.TurnTimeout):
        agent._generate([], CONFIG, time.monotonic() + 90)
    assert [m for m, _ in calls] == ["main-model", "backup-model"]


def test_504_then_other_error_is_not_hidden(monkeypatch):
    calls = _fake_client(monkeypatch, lambda m, c: _raise(_deadline_exceeded()) if m == "main-model"
                         else _raise(APIErr(400)))
    with pytest.raises(APIErr):
        agent._generate([], CONFIG, time.monotonic() + 90)
    assert len(calls) == 2


def test_504_on_all_models_shows_friendly_message(monkeypatch):
    """מקצה לקצה: POST /api/chat → תור ברקע → 504 בשני המודלים → הודעת timeout ידידותית."""
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from conftest import wait_turn

    _fake_client(monkeypatch, lambda m, c: _raise(_deadline_exceeded()))
    main_mod._rate_store.clear()
    client = TestClient(main_mod.app)
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    assert client.post("/api/chat", json={"session_id": sid, "message": "שלום"}).status_code == 200
    st = wait_turn(client, sid)
    assert st["error_message"] == "לא הצלחתי לסיים בזמן, אפשר לנסות שוב."
    assert "DEADLINE" not in str(st) and "504" not in str(st)
