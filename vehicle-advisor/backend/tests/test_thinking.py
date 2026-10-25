"""
"חשיבה" מינימלית + timeout של 60 שניות — נבדק דרך הספרייה המותקנת עצמה:
client אמיתי של google-genai, ורק השכבה שמדברת עם הרשת (_api_client._request) מוחלפת
בפונקציה שמתעדת את בקשת ה-HTTP שנבנתה ומחזירה תשובה מינימלית. בלי קריאה ל-Gemini.
"""
import json
import re

import pytest
from google import genai
from google.genai import _api_client, errors, types

import app.agent as agent

CONFIG = types.GenerateContentConfig(temperature=0)
CONTENTS = [types.Content(role="user", parts=[types.Part(text="שלום")])]
OK_BODY = json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}}]})


@pytest.fixture(scope="module")
def real_client():
    # יצירת client לוקחת כמה שניות (SSL) — פעם אחת לקובץ, כמו בשרת
    return genai.Client(api_key="test-key-not-real",
                        http_options=types.HttpOptions(timeout=60_000, retry_options=None))


@pytest.fixture
def wire(monkeypatch, real_client):
    """client אמיתי; כל בקשה שהספרייה בונה נרשמת ב-sent (url, גוף, timeout, כותרות)."""
    client = real_client
    sent = []
    replies = []  # אפשר לדחוף חריגה שתיזרק במקום תשובה

    def fake_request(http_request, http_options=None, stream=False):
        sent.append(http_request)
        if replies:
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
        return _api_client.HttpResponse(headers={}, response_stream=[OK_BODY])

    monkeypatch.setattr(client._api_client, "_request", fake_request)
    monkeypatch.setattr(agent, "_client", client)
    monkeypatch.setattr(agent, "_THINKING_REJECTED", set())
    return sent, replies


def _thinking_sent(req) -> dict | None:
    """thinkingConfig מתוך ה-JSON שיוצא לרשת בפועל (הספרייה שולחת json.dumps(req.data)).
    בגרסה 2.31 השדות הפנימיים יוצאים בשמות snake_case — מנרמלים ל-camelCase להשוואה."""
    wire = json.loads(json.dumps(req.data))
    tc = wire.get("generationConfig", {}).get("thinkingConfig")
    if tc is None:
        return None
    return {re.sub(r"_(\w)", lambda m: m.group(1).upper(), k): v for k, v in tc.items()}


# ── ההגדרה לכל משפחת מודלים ──────────────────────────────────────────────────

@pytest.mark.parametrize("model, expected", [
    ("gemini-2.5-flash", {"thinkingBudget": 0}),        # .env.example — ראשי
    ("gemini-2.5-flash-lite", {"thinkingBudget": 0}),   # .env.example — גיבוי
    ("gemini-2.5-pro", {"thinkingBudget": 128}),        # Pro: אי אפשר לכבות — המינימום
    ("gemini-3.8-flash", {"thinkingLevel": "LOW"}),     # ברירת המחדל בקוד
    ("gemini-3.5-flash", {"thinkingLevel": "LOW"}),
    ("gemini-3-pro-preview", {"thinkingLevel": "LOW"}),
])
def test_thinking_config_reaches_built_request(wire, monkeypatch, model, expected):
    sent, _ = wire
    monkeypatch.setattr(agent, "MODELS", [model])
    resp = agent._generate(CONTENTS, CONFIG, None)
    assert resp.text == "ok"
    assert len(sent) == 1
    assert model in sent[0].url
    assert _thinking_sent(sent[0]) == expected
    # במודלי 3.x לא נשלח thinking_budget (לפי הספרייה הוא שגיאה מ-3.5)
    if "thinkingLevel" in expected:
        assert "thinkingBudget" not in _thinking_sent(sent[0])


@pytest.mark.parametrize("model", ["gemini-2.0-flash", "gemini-1.5-flash", "some-other-model"])
def test_unknown_model_gets_no_thinking_config(wire, monkeypatch, model):
    sent, _ = wire
    monkeypatch.setattr(agent, "MODELS", [model])
    agent._generate(CONTENTS, CONFIG, None)
    assert _thinking_sent(sent[0]) is None


def test_main_and_backup_each_get_their_own_setting(wire, monkeypatch):
    """הראשי נכשל ב-503 → הגיבוי מקבל את ההגדרה שמתאימה *לו*."""
    sent, replies = wire
    monkeypatch.setattr(agent, "MODELS", ["gemini-3.8-flash", "gemini-2.5-flash-lite"])
    replies.append(errors.ServerError(503, {"error": {"code": 503, "message": "overloaded",
                                                     "status": "UNAVAILABLE"}}))
    agent._generate(CONTENTS, CONFIG, None)
    assert [("3.8" in r.url, _thinking_sent(r)) for r in sent] == [
        (True, {"thinkingLevel": "LOW"}),
        (False, {"thinkingBudget": 0}),
    ]


def test_rejected_thinking_config_is_dropped_not_fatal(wire, monkeypatch):
    """מודל שמחזיר 400 על הגדרת החשיבה — שולחים שוב פעם אחת בלעדיה, וזוכרים לפעם הבאה."""
    sent, replies = wire
    monkeypatch.setattr(agent, "MODELS", ["gemini-3.9-flash"])
    replies.append(errors.ClientError(400, {"error": {
        "code": 400, "message": "Thinking level is not supported for this model.",
        "status": "INVALID_ARGUMENT"}}))
    assert agent._generate(CONTENTS, CONFIG, None).text == "ok"
    assert _thinking_sent(sent[0]) == {"thinkingLevel": "LOW"}
    assert _thinking_sent(sent[1]) is None
    # בקריאה הבאה כבר לא שולחים את ההגדרה
    agent._generate(CONTENTS, CONFIG, None)
    assert _thinking_sent(sent[2]) is None


def test_other_400_is_not_swallowed(wire, monkeypatch):
    sent, replies = wire
    monkeypatch.setattr(agent, "MODELS", ["gemini-2.5-flash"])
    replies.append(errors.ClientError(400, {"error": {"code": 400, "message": "bad contents",
                                                     "status": "INVALID_ARGUMENT"}}))
    with pytest.raises(errors.ClientError):
        agent._generate(CONTENTS, CONFIG, None)
    assert len(sent) == 1


# ── timeout אפקטיבי של 60 שניות ──────────────────────────────────────────────

def test_effective_timeout_is_60_seconds(wire, monkeypatch):
    import time
    sent, _ = wire
    monkeypatch.setattr(agent, "MODELS", ["gemini-2.5-flash"])
    agent._generate(CONTENTS, CONFIG, time.monotonic() + agent.TURN_TIMEOUT_S)
    assert agent.CALL_TIMEOUT_S == 60 and agent.TURN_TIMEOUT_S == 90
    assert sent[0].timeout == 60.0
    assert sent[0].headers["X-Server-Timeout"] == "60"
    # 20 שניות לפני סוף התור — הקריאה מקוצרת לזמן שנשאר
    agent._generate(CONTENTS, CONFIG, time.monotonic() + 20)
    assert 19.0 <= sent[1].timeout <= 20.0
    assert sent[1].headers["X-Server-Timeout"] == "20"
