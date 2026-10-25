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
