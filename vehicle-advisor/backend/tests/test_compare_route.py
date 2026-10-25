"""
מסלול השוואה בלי Gemini: GET /api/cars ו-POST /api/compare על הפרופיל השמור בשיחה.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import app.agent as agent
import app.main as main_mod
from app import store
from app.comparison import check_eligibility, run_comparison
from app.config import load_catalogue, load_demo_employees, load_rules
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """אם משהו כאן ינסה לקרוא למודל — הבדיקה תיכשל."""
    def boom(*a, **k):
        raise AssertionError("Gemini must not be called")
    monkeypatch.setattr(agent, "_generate", boom)
    main_mod._rate_store.clear()


def _demo_session(emp_id="E-001"):
    sid = str(uuid.uuid4())
    assert client.post(f"/api/demo/{emp_id}", json={"session_id": sid}).status_code == 200
    profile = next(e for e in load_demo_employees() if e["id"] == emp_id)["profile"]
    return sid, profile


# ── GET /api/cars ───────────────────────────────────────────────────────────

def test_cars_invalid_session_id():
    assert client.get("/api/cars", params={"session_id": "bad id!"}).status_code == 400


def test_cars_without_profile():
    body = client.get("/api/cars", params={"session_id": str(uuid.uuid4())}).json()
    assert body["status"] == "missing_profile" and body["cars"] == []


@pytest.mark.parametrize("emp_id", ["E-001", "E-002", "E-003", "E-006"])
def test_cars_are_exactly_the_eligible_ones(emp_id):
    sid, profile = _demo_session(emp_id)
    body = client.get("/api/cars", params={"session_id": sid}).json()
    assert body["status"] == "ok"
    ids = {c["id"] for c in body["cars"]} - {"WAIVER"}
    rules = load_rules()
    expected = {c["id"] for c in load_catalogue()
                if check_eligibility(profile, c, rules)[0] and c["id"] != profile.get("current_car_id")}
    assert ids == expected
    car = next(c for c in body["cars"] if c["id"] != "WAIVER")
    assert {"label", "company_cost", "default_participation"} <= set(car)


def test_cars_group_c_excludes_group_d():
    sid, _ = _demo_session("E-001")  # קבוצה ג
    groups = {c["group"] for c in client.get("/api/cars", params={"session_id": sid}).json()["cars"]}
    assert "ד" not in groups


def test_waiver_listed_only_when_allowed():
    sid, _ = _demo_session("E-005")  # מנהל/ת פיתוח — מותר לוותר
    assert any(c["id"] == "WAIVER" for c in client.get("/api/cars", params={"session_id": sid}).json()["cars"])
    sid, _ = _demo_session("E-003")  # כבר ויתרה (אחזקה) — אין "ויתור" נוסף
    assert all(c["id"] != "WAIVER" for c in client.get("/api/cars", params={"session_id": sid}).json()["cars"])


# ── POST /api/compare ───────────────────────────────────────────────────────

def test_compare_invalid_session_id():
    r = client.post("/api/compare", json={"session_id": "bad id!", "car_id": "CAR-10"})
    assert r.status_code == 400


def test_compare_missing_profile_lists_what_is_missing():
    r = client.post("/api/compare", json={"session_id": str(uuid.uuid4()), "car_id": "CAR-10"})
    body = r.json()
    assert body["status"] == "missing_profile"
    assert "base" in body["missing"] and "group" in body["missing"] and "current_car" in body["missing"]


def test_compare_matches_run_comparison_with_catalogue_participation():
    sid, profile = _demo_session("E-001")
    body = client.post("/api/compare", json={"session_id": sid, "car_id": "CAR-10"}).json()
    ref = run_comparison(profile, "CAR-10", 300)  # ההשתתפות בקטלוג ל-ATTO 3
    assert body["status"] == "ok"
    assert body["today"] == ref["today"] and body["alt"] == ref["alt"] and body["diff"] == ref["diff"]
    assert body["participation_source"] == "catalogue_default"
    # נשמר בשיחה — אחרי רענון ההשוואה מוצגת
    st = client.get(f"/api/chat/{sid}/state").json()
    assert st["comparison"]["alt"]["net"] == ref["alt"]["net"]


def test_compare_with_explicit_participation():
    sid, profile = _demo_session("E-001")
    body = client.post("/api/compare", json={"session_id": sid, "car_id": "CAR-10",
                                             "participation": 350}).json()
    assert body["alt"]["net"] == run_comparison(profile, "CAR-10", 350)["alt"]["net"]
    assert body["participation_source"] == "employee"


def test_compare_waiver():
    sid, profile = _demo_session("E-005")
    body = client.post("/api/compare", json={"session_id": sid, "car_id": "WAIVER"}).json()
    assert body["status"] == "ok"
    assert body["diff"] == run_comparison(profile, "WAIVER")["diff"]


def test_compare_not_eligible_car_is_refused_and_not_saved():
    sid, _ = _demo_session("E-001")  # קבוצה ג
    body = client.post("/api/compare", json={"session_id": sid, "car_id": "CAR-17"}).json()
    assert body["status"] == "not_eligible"
    assert client.get(f"/api/chat/{sid}/state").json()["comparison"] is None


def test_compare_unknown_car():
    sid, _ = _demo_session("E-001")
    r = client.post("/api/compare", json={"session_id": sid, "car_id": "CAR-99"})
    assert r.status_code == 404


def test_compare_rate_limited(monkeypatch):
    sid, _ = _demo_session("E-001")
    monkeypatch.setattr(main_mod, "_check_rate", lambda ip: False)
    r = client.post("/api/compare", json={"session_id": sid, "car_id": "CAR-10"})
    assert r.status_code == 429


def test_compare_refused_while_bot_turn_running(monkeypatch):
    sid, _ = _demo_session("E-001")
    monkeypatch.setattr(main_mod.turns, "is_busy", lambda s: True)
    r = client.post("/api/compare", json={"session_id": sid, "car_id": "CAR-10"})
    assert r.status_code == 409
