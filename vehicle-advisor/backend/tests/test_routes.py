"""
בדיקות נתיבי HTTP — ללא קריאות ל-Gemini ובלי תלות ב-build אמיתי של Angular.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unknown_api_route_returns_404():
    """נתיב /api שלא קיים — חייב להחזיר 404, לא index.html."""
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404


def test_static_file_served_with_correct_content_type(tmp_path, monkeypatch):
    """קובץ JS מתיקיית dist מוגש עם Content-Type נכון."""
    import app.main as main_mod

    browser = tmp_path / "browser"
    browser.mkdir(parents=True)
    (browser / "index.html").write_text("<html></html>")
    (browser / "main-TEST.js").write_text("console.log('ok')")

    monkeypatch.setattr(main_mod, "_DIST", browser)
    c = TestClient(main_mod.app, raise_server_exceptions=True)

    r = c.get("/main-TEST.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_spa_unknown_path_returns_index(tmp_path, monkeypatch):
    """נתיב SPA כמו /agent חוזר ל-index.html."""
    import app.main as main_mod

    browser = tmp_path / "browser"
    browser.mkdir(parents=True)
    (browser / "index.html").write_text("<html>index</html>")

    monkeypatch.setattr(main_mod, "_DIST", browser)
    c = TestClient(main_mod.app, raise_server_exceptions=True)

    r = c.get("/agent")
    assert r.status_code == 200
    assert b"index" in r.content


def test_path_traversal_blocked(tmp_path, monkeypatch):
    """
    Path traversal — קובץ מחוץ לתיקיית dist לא מוגש.
    TestClient מנרמל URLs, לכן בודקים את spa_fallback ישירות.
    """
    import app.main as main_mod

    browser = tmp_path / "browser"
    browser.mkdir(parents=True)
    (browser / "index.html").write_text("<html>index</html>")

    # קובץ סודי מחוץ לתיקיית browser
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET_CONTENT")

    monkeypatch.setattr(main_mod, "_DIST", browser)

    # וריאציות של path traversal
    traversal_paths = [
        "../secret.txt",          # נתיב רגיל
        "%2e%2e/secret.txt",      # קידוד אחד
        "..%2fsecret.txt",
        "%2e%2e/%2e%2e/secret.txt",
    ]

    for path in traversal_paths:
        # קוראים ל-spa_fallback ישירות (עוקפים URL normalization של TestClient)
        from fastapi import HTTPException
        from fastapi.responses import FileResponse

        try:
            result = main_mod.spa_fallback(path)
            # אם חזר FileResponse — חייב להיות index.html, לא secret.txt
            assert isinstance(result, FileResponse), f"path={path}: expected FileResponse"
            assert "secret" not in result.path, \
                f"path={path}: secret file was served! ({result.path})"
        except HTTPException:
            pass  # 404 גם קביל


def test_agent_questions_session_id_truncated():
    """session_id ב-agent/questions מוחזר מקוצר ל-8 תווים בלבד."""
    import uuid
    from app import store

    full_sid = str(uuid.uuid4())  # 36 תווים
    qid = store.add_question(full_sid, "שאלת בדיקה לקיצור sid")

    r = client.get("/api/agent/questions")
    assert r.status_code == 200
    questions = r.json()
    matching = [q for q in questions if q["id"] == qid]
    assert matching, "שאלת הבדיקה לא נמצאה"
    assert matching[0]["session_id"] == full_sid[:8], \
        f"session_id לא קוצר: {matching[0]['session_id']}"

    # ניקוי — ה-answer_question לא ישנה אך לפחות נוודא שה-question_id עובד
    ok = store.answer_question(qid, "תשובה לבדיקה")
    assert ok, "answer_question נכשל — question_id כנראה לא נשמר"


# ── אימות session_id ────────────────────────────────────────────────────────

@pytest.mark.parametrize("sid", ["short", "a" * 65, "abc_defgh", "../../etc", "שלוםשלוםשלום", "abcd efgh"])
def test_invalid_session_id_rejected(sid):
    import app.main as main_mod
    main_mod._rate_store.clear()
    r = client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    assert r.status_code == 400
    if "/" not in sid:  # עם '/' ה-URL מנורמל לנתיב אחר ולא מגיע ל-endpoint בכלל
        r = client.get(f"/api/chat/{sid}/state")
        assert r.status_code == 400


def test_uuid_session_id_accepted():
    """crypto.randomUUID() בממשק — 36 תווים, אותיות/ספרות/מקפים."""
    import uuid
    r = client.get(f"/api/chat/{uuid.uuid4()}/state")
    assert r.status_code == 200
    assert "history" in r.json()


# ── שגיאות בצ'אט ────────────────────────────────────────────────────────────

def test_chat_error_does_not_leak_exception(monkeypatch, caplog, wait):
    import uuid
    import app.main as main_mod

    def boom(sid, msg):
        raise RuntimeError("SECRET_INTERNAL_DETAIL")

    monkeypatch.setattr(main_mod, "agent_chat", boom)
    main_mod._rate_store.clear()
    sid = str(uuid.uuid4())
    r = client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    assert r.status_code == 200 and r.json() == {"status": "pending"}
    st = wait(client, sid)
    assert "SECRET_INTERNAL_DETAIL" not in str(st)
    assert "תקלה" in st["error_message"]
    # הפרטים נרשמו בלוג השרת
    assert "SECRET_INTERNAL_DETAIL" in caplog.text


def test_chat_quota_error_keeps_friendly_message(monkeypatch, wait):
    import uuid
    import app.main as main_mod

    def quota(sid, msg):
        raise RuntimeError("429 Resource has been exhausted")

    monkeypatch.setattr(main_mod, "agent_chat", quota)
    main_mod._rate_store.clear()
    sid = str(uuid.uuid4())
    client.post("/api/chat", json={"session_id": sid, "message": "שלום"})
    st = wait(client, sid)
    assert "מכסת" in st["error_message"]
    assert "429" not in st["error_message"]


# ── זיהוי כתובת והגבלת קצב ──────────────────────────────────────────────────

def _fake_request(xff=None, host="10.0.0.1"):
    from types import SimpleNamespace
    headers = {"X-Forwarded-For": xff} if xff else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_client_ip_ignores_xff_without_trust_proxy(monkeypatch):
    import app.main as main_mod
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert main_mod._client_ip(_fake_request("1.2.3.4, 5.6.7.8")) == "10.0.0.1"


def test_client_ip_uses_last_xff_with_trust_proxy(monkeypatch):
    import app.main as main_mod
    monkeypatch.setenv("TRUST_PROXY", "1")
    assert main_mod._client_ip(_fake_request("1.2.3.4, 5.6.7.8")) == "5.6.7.8"
    assert main_mod._client_ip(_fake_request()) == "10.0.0.1"


def test_rate_limit_blocks_after_limit():
    import app.main as main_mod
    main_mod._rate_store.clear()
    for _ in range(main_mod._RATE_LIMIT):
        assert main_mod._check_rate("9.9.9.9")
    assert not main_mod._check_rate("9.9.9.9")
    main_mod._rate_store.clear()


def test_rate_store_drops_expired_entries(monkeypatch):
    import app.main as main_mod
    main_mod._rate_store.clear()
    now = [1000.0]
    monkeypatch.setattr(main_mod.time, "time", lambda: now[0])
    main_mod._check_rate("1.1.1.1")
    now[0] += main_mod._RATE_WINDOW + 1
    main_mod._check_rate("2.2.2.2")
    assert "1.1.1.1" not in main_mod._rate_store
    assert "2.2.2.2" in main_mod._rate_store
    main_mod._rate_store.clear()


def test_rate_store_size_capped(monkeypatch):
    import app.main as main_mod
    main_mod._rate_store.clear()
    monkeypatch.setattr(main_mod, "_RATE_MAX_IPS", 5)
    for i in range(10):
        main_mod._check_rate(f"ip-{i}")
    assert len(main_mod._rate_store) == 5
    assert list(main_mod._rate_store) == [f"ip-{i}" for i in range(5, 10)]
    main_mod._rate_store.clear()
