"""
בדיקות "מה הכי משתלם לי?" — דירוג כל החלופות שהעובד זכאי להן.
"""
import uuid

from fastapi.testclient import TestClient

from app import store
from app.comparison import rank_options, run_comparison, build_waiver
from app.config import load_catalogue
from app.main import app

client = TestClient(app)

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)

E002 = dict(base=9800, commissions=2400, bonus=0, pension_pct=6, study_pct=0,
            credit_points=2.25, group="ב", children=5, occupation="נציג/ת מכירות שטח",
            current_car_id="CAR-19", current_participation=500)


def _cars():
    return {c["id"]: c for c in load_catalogue()}


def test_sorted_descending():
    r = rank_options(E001)
    assert r["status"] == "ok"
    nets = [o["net"] for o in r["options"]]
    assert nets, "אין חלופות בדירוג"
    assert nets == sorted(nets, reverse=True)


def test_not_eligible_cars_excluded():
    cars = _cars()
    ids = {o["target"] for o in rank_options(E001)["options"]}
    # עובד בקבוצה ג: אין רכבי קבוצה ד, ואין מסלול משפחתי (2 ילדים)
    assert not any(cars[i]["group"] == "ד" for i in ids if i != "WAIVER")
    assert "CAR-17" not in ids
    assert "CAR-19" not in ids and "CAR-20" not in ids
    # הרכב הנוכחי אינו "חלופה"
    assert "CAR-09" not in ids


def test_group_b_only_lower_or_equal_groups():
    cars = _cars()
    ids = {o["target"] for o in rank_options(E002)["options"]} - {"WAIVER"}
    assert all(cars[i]["group"] in ("א", "ב", "משפחתי") for i in ids)
    # 5 ילדים — זכאי למסלול המשפחתי (רכב נוכחי CAR-19 לא נכלל)
    assert "CAR-20" in ids


def test_matches_run_comparison():
    cars = _cars()
    for o in rank_options(E001)["options"]:
        if o["target"] == "WAIVER":
            ref = run_comparison(E001, "WAIVER")
        else:
            ref = run_comparison(E001, o["target"], cars[o["target"]]["default_participation"])
        assert ref["status"] == "ok"
        assert o["net"] == ref["alt"]["net"], o["target"]
        assert o["diff"] == ref["diff"], o["target"]


def test_waiver_included_only_if_allowed():
    for emp in (E001, E002):
        allowed = build_waiver(emp)[0] is not None
        has = any(o["target"] == "WAIVER" for o in rank_options(emp)["options"])
        assert has == allowed, emp["occupation"]


def test_today_net_matches_run_comparison():
    r = rank_options(E001)
    ref = run_comparison(E001, "CAR-10", participation=300)
    assert r["today_net"] == ref["today"]["net"]


# ── נתיב HTTP ───────────────────────────────────────────────────────────────

def test_rank_route_invalid_session_id():
    r = client.post("/api/rank", json={"session_id": "bad id!"})
    assert r.status_code == 400


def test_rank_route_missing_profile():
    r = client.post("/api/rank", json={"session_id": str(uuid.uuid4())})
    assert r.status_code == 200
    assert r.json()["status"] == "missing_profile"


def test_rank_route_ok():
    sid = str(uuid.uuid4())
    store.save_session(sid, dict(E001), [])
    r = client.post("/api/rank", json={"session_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["options"] == rank_options(E001)["options"]
