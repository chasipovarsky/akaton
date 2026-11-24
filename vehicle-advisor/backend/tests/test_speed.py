"""
האצת התור של הבוט: כלי משולב (שם רכב במקום סבב list_cars), הקשר בהודעה, פלט כלים קומפקטי,
קיצור היסטוריה, prefix יציב, שלב התור (stage) ו-client אחד. בלי Gemini — מודל מתוסרט.
"""
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import app.agent as agent
import app.main as main_mod
from app import store, turns
from app.comparison import run_comparison
from app.main import app
from conftest import wait_turn

client = TestClient(app)

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)


def _calls(*calls):
    fcs = [SimpleNamespace(name=n, args=a) for n, a in calls]
    content = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name=n, args=a)) for n, a in calls])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], function_calls=fcs, text=None)


def _text(t):
    content = types.Content(role="model", parts=[types.Part(text=t)])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], function_calls=None, text=t)


@pytest.fixture
def fake_model(monkeypatch):
    queue, requests = [], []

    def fake_generate(contents, config, deadline=None):
        requests.append((list(contents), config))
        return queue.pop(0)

    monkeypatch.setattr(agent, "_generate", fake_generate)
    monkeypatch.setattr(agent, "_client", object())
    main_mod._rate_store.clear()
    return SimpleNamespace(queue=queue, requests=requests)


def _session(profile=None, history=None):
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001 if profile is None else profile), history or [])
    return sid


def _result(contents_item, i=0):
    return contents_item.parts[i].function_response.response["result"]


# ── כלי משולב: שם רכב → מזהה, בלי סבב list_cars ─────────────────────────────

@pytest.mark.parametrize("name", ["ATTO 3", "atto 3", "CAR-10", "car-10"])
def test_compare_accepts_car_name_or_id(name):
    profile = dict(E001)
    res = agent._run_tool("x" * 8, profile, "compare_alternative", {"target": name, "participation": 300})
    ref = run_comparison(E001, "CAR-10", 300)
    assert res["status"] == "ok"
    assert (res["today"]["net"], res["alt"]["net"], res["diff"]) == \
        (ref["today"]["net"], ref["alt"]["net"], ref["diff"])
    assert res["explanation"] == ref["explanation"]


def test_compare_ambiguous_name_returns_options_without_guessing():
    profile = dict(E001)
    res = agent._run_tool("x" * 8, profile, "compare_alternative", {"target": "EV", "participation": 300})
    assert res["status"] == "unknown_car"
    assert {o["id"] for o in res["options"]} >= {"CAR-08", "CAR-11"}
    assert "_last_comparison" not in profile  # לא חושב כלום


def test_compare_unknown_name_is_not_guessed():
    profile = dict(E001)
    res = agent._run_tool("x" * 8, profile, "compare_alternative", {"target": "פרארי", "participation": 300})
    assert res == {"status": "unknown_car", "options": []}
    assert "_last_comparison" not in profile


def test_exact_model_name_wins_over_partial_match():
    # COROLLA מופיע גם בתוך COROLLA CROSS — שם מדויק בוחר רכב אחד
    assert agent._resolve_car("COROLLA") == ("CAR-07", [])
    assert agent._resolve_car("COROLLA CROSS") == ("CAR-09", [])
    assert agent._resolve_car("WAIVER") == ("WAIVER", [])


def test_update_profile_resolves_current_car_name():
    profile = {k: v for k, v in E001.items() if k != "current_car_id"}
    res = agent._run_tool("x" * 8, profile, "update_profile", {"current_car_id": "COROLLA CROSS"})
    assert profile["current_car_id"] == "CAR-09"
    assert res == {"saved": ["current_car_id"], "missing": []}
    res = agent._run_tool("x" * 8, profile, "update_profile", {"current_car_id": "פרארי"})
    assert res["status"] == "unknown_car" and profile["current_car_id"] == "CAR-09"


def test_one_round_with_car_name_full_turn(fake_model):
    """'אני רוצה ATTO 3, השתתפות 300' — סבב כלים אחד + תשובה (בלי list_cars)."""
    sid = _session()
    fake_model.queue += [
        _calls(("compare_alternative", {"target": "ATTO 3", "participation": 300})),
        _text("הנה ההשוואה."),
    ]
    client.post("/api/chat", json={"session_id": sid, "message": "אני רוצה ATTO 3, השתתפות 300"})
    st = wait_turn(client, sid)
    assert len(fake_model.requests) == 2
    ref = run_comparison(E001, "CAR-10", 300)
    assert st["comparison"]["status"] == "ok" and st["comparison"]["diff"] == ref["diff"]
    # הפאנל בממשק מקבל את ההשוואה המלאה (details) — הקיצור הוא רק במה שנשלח למודל
    assert "details" in st["comparison"]


def test_parallel_update_and_compare_in_one_round(fake_model):
    sid = _session({k: v for k, v in E001.items() if k != "current_participation"})
    fake_model.queue += [
        _calls(("update_profile", {"current_participation": 300}),
               ("compare_alternative", {"target": "CAR-10", "participation": 300})),
        _text("הנה ההשוואה."),
    ]
    client.post("/api/chat", json={"session_id": sid, "message": "השתתפות היום 300, ורוצה ATTO 3 ב-300"})
    st = wait_turn(client, sid)
    assert len(fake_model.requests) == 2
    tool_msg = fake_model.requests[1][0][-1]
    assert _result(tool_msg, 0) == {"saved": ["current_participation"], "missing": []}
    assert _result(tool_msg, 1)["status"] == "ok"
    assert st["comparison"]["diff"] == run_comparison(E001, "CAR-10", 300)["diff"]


# ── פלט קומפקטי וקיצור היסטוריה ─────────────────────────────────────────────

def test_compare_output_is_compact():
    profile = dict(E001)
    res = agent._run_tool("x" * 8, profile, "compare_alternative", {"target": "CAR-10", "participation": 300})
    assert set(res) == {"status", "reason", "today", "alt", "diff", "explanation"}
    assert "details" in profile["_last_comparison"] and "inputs" in profile["_last_comparison"]


def _old_turn_history():
    """היסטוריה בפורמט הישן: update_profile עם פרופיל מלא, list_cars עם כל השדות, השוואה מלאה."""
    full = run_comparison(E001, "CAR-10", 300)
    old_list = {"cars": [{"id": "CAR-10", "group": "ג", "maker": "בי ווי די", "model": "ATTO 3", "drive": "חשמלי"}]}
    fr = lambda n, r: types.Part.from_function_response(name=n, response={"result": r})  # noqa: E731
    return [
        types.Content(role="user", parts=[types.Part(text="אני רוצה ATTO 3")]),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="list_cars", args={}))]),
        types.Content(role="user", parts=[fr("update_profile", {"saved": ["base"], "profile": dict(E001)}),
                                          fr("list_cars", old_list), fr("compare_alternative", full)]),
        types.Content(role="model", parts=[types.Part(text="הנה ההשוואה.")]),
    ], full


def test_compact_history_shrinks_old_tool_results_and_keeps_text():
    history, full = _old_turn_history()
    out = agent._compact_history(history)
    upd, cars, cmp = (p.function_response.response["result"] for p in out[2].parts)
    assert upd == {"saved": ["base"], "missing": []}
    assert cars == {"cars": [{"id": "CAR-10", "model": "ATTO 3", "drive": "חשמלי"}]}
    assert set(cmp) == {"status", "reason", "today", "alt", "diff", "explanation"}
    assert cmp["diff"] == full["diff"] and cmp["explanation"] == full["explanation"]
    # הטקסט של השיחה וקריאות הכלים — בלי שינוי
    assert [c.model_dump() for i, c in enumerate(out) if i != 2] == \
        [c.model_dump() for i, c in enumerate(history) if i != 2]
    # אידמפוטנטי: קיצור שני לא משנה כלום
    assert [c.model_dump() for c in agent._compact_history(out)] == [c.model_dump() for c in out]
    size = lambda c: len(c.model_dump_json(exclude_none=True))  # noqa: E731
    assert size(out[2]) < size(history[2]) * 0.6  # בפועל: 1431 → 658 תווים


def test_old_session_is_compacted_when_saved(fake_model):
    history, _ = _old_turn_history()
    sid = _session(history=history)
    fake_model.queue += [_text("בשמחה.")]
    client.post("/api/chat", json={"session_id": sid, "message": "תודה"})
    wait_turn(client, sid)
    sent = fake_model.requests[0][0]
    assert "profile" not in _result(sent[2], 0)          # המודל קיבל את הגרסה המקוצרת
    _, saved = store.load_session(sid)
    assert "details" not in _result(saved[2], 2)         # ונשמרה מקוצרת
    assert store.display_history(sid)[-2:] == [{"role": "user", "text": "תודה"},
                                               {"role": "bot", "text": "בשמחה."}]


# ── הקשר בהודעה ו-prefix יציב ───────────────────────────────────────────────

def test_context_sent_with_message_but_not_saved_or_shown(fake_model):
    sid = _session({k: v for k, v in E001.items() if k != "occupation"})
    fake_model.queue += [_text("מה העיסוק שלך?")]
    client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    wait_turn(client, sid)

    last = fake_model.requests[0][0][-1]
    assert last.parts[0].text == "שלום"
    ctx = last.parts[1].text
    assert ctx.startswith("[הקשר מהמערכת")
    assert "base=18500" in ctx and "חסרים: occupation" in ctx
    assert "רכב נוכחי: CAR-09 COROLLA CROSS" in ctx
    assert "CAR-10 ATTO 3 (2025)" in ctx and "CAR-09 COROLLA CROSS (" not in ctx.split("רכבים שאפשר")[1]

    _, saved = store.load_session(sid)
    assert len(saved[0].parts) == 1 and saved[0].parts[0].text == "שלום"
    assert "הקשר" not in str(store.display_history(sid))


def test_cars_route_without_occupation_does_not_fail():
    """היה KeyError ב-build_waiver כשיש קבוצה ואין עיסוק — עכשיו רשימה בלי ויתור."""
    sid = _session({"group": "ג", "children": 0})
    r = client.get("/api/cars", params={"session_id": sid})
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["cars"]]
    assert "CAR-10" in ids and "WAIVER" not in ids


def test_context_without_group_has_no_car_list():
    ctx = agent._context_text({})
    assert "פרופיל שמור: ריק" in ctx and "group" in ctx and "רכבים" not in ctx


def test_prefix_is_stable_between_turns(fake_model):
    """פרומפט + הגדרות כלים זהים בכל קריאה, וההיסטוריה של תור קודם נשלחת כפי שנשמרה."""
    sid = _session()
    fake_model.queue += [_text("א"), _text("ב")]
    client.post("/api/chat", json={"session_id": sid, "message": "1"})
    wait_turn(client, sid)
    client.post("/api/chat", json={"session_id": sid, "message": "2"})
    wait_turn(client, sid)
    (c1, cfg1), (c2, cfg2) = fake_model.requests
    assert cfg1.system_instruction == cfg2.system_instruction == agent.SYSTEM_PROMPT
    assert [t.model_dump() for t in cfg1.tools] == [t.model_dump() for t in cfg2.tools]
    assert cfg1.max_output_tokens == agent.MAX_OUTPUT_TOKENS
    # בתור השני: ההיסטוריה מתחילה בהודעה של התור הראשון בלי ההקשר, וההקשר רק בסוף
    assert c2[0].parts[0].text == "1" and len(c2[0].parts) == 1
    assert len(c2[-1].parts) == 2


# ── שלב התור (stage) ─────────────────────────────────────────────────────────

def test_stage_sequence(fake_model, monkeypatch):
    seen = []
    monkeypatch.setattr(turns, "set_stage", lambda sid, s: seen.append(s))
    sid = _session()
    fake_model.queue += [_calls(("compare_alternative", {"target": "CAR-10", "participation": 300})),
                         _text("הנה.")]
    agent.chat(sid, "ATTO 3 ב-300")
    assert seen == ["checking", "computing", "writing"]


def test_stage_in_state_while_busy(monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    def fake_generate(contents, config, deadline=None):
        entered.set()
        assert gate.wait(10)
        return _text("שלום")

    monkeypatch.setattr(agent, "_generate", fake_generate)
    monkeypatch.setattr(agent, "_client", object())
    main_mod._rate_store.clear()
    sid = _session()
    client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    try:
        assert entered.wait(5)
        st = client.get(f"/api/chat/{sid}/state").json()
        assert st["busy"] is True and st["stage"] == "checking"
    finally:
        gate.set()
    st = wait_turn(client, sid)
    assert st["busy"] is False and st["stage"] is None


def test_set_stage_ignored_when_no_turn():
    sid = str(uuid.uuid4())
    turns.set_stage(sid, "writing")
    assert turns.status(sid)["stage"] is None


# ── client אחד ───────────────────────────────────────────────────────────────

def test_warm_client_creates_one_client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(agent, "_client", None)
    agent.warm_client()
    first = agent._client
    agent.warm_client()
    assert agent._client is first
    assert first._api_client._http_options.timeout == agent.CALL_TIMEOUT_S * 1000
