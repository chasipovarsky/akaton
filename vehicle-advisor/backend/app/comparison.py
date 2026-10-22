from app.config import load_rules, load_catalogue, load_occupations
from app.vehicles import get_value_of_use
from app.net_engine import compare

TAX_YEAR = 2026


def _find_car(catalogue, car_id):
    for c in catalogue:
        if c["id"] == car_id:
            return c
    return None


def _car_label(car: dict) -> str:
    return f'{car["model"]} ({car["reg_year"]})'


def check_eligibility(emp: dict, car: dict, rules: dict) -> tuple[bool, str]:
    """emp: group, children. car: מהקטלוג."""
    if car["group"] == "משפחתי":
        fam = rules["familyTrack"]
        if emp.get("children", 0) >= fam["minChildren"] and car["id"] in fam["cars"]:
            return True, f"מסלול ברוכת ילדים ({emp['children']} ילדים)"
        return False, "מסלול ברוכת ילדים דורש 4 ילדים ומעלה עד גיל 23"
    order = rules["groupOrder"]
    if order.index(car["group"]) <= order.index(emp["group"]):
        return True, "הרכב מהקבוצה שלך או נמוכה ממנה"
    return False, "הרכב מקבוצה גבוהה מהקבוצה שלך"


def build_alternative(emp: dict, car_id: str, participation: float | None):
    """מחזיר (alt_dict, מידע) או (None, מה חסר)."""
    rules = load_rules()
    car = _find_car(load_catalogue(), car_id)
    if car is None:
        return None, {"error": "רכב לא נמצא בקטלוג"}

    ok, reason = check_eligibility(emp, car, rules)
    if not ok:
        return None, {"eligible": False, "reason": reason}

    if participation is None:
        return None, {"missing": "participation",
                      "question": "מה סכום ההשתתפות שלך ברכב החדש?"}

    rows = get_value_of_use(TAX_YEAR, car["reg_year"], 1, car["maker_code"], car["model_code"])
    if len(rows) != 1:
        return None, {"error": "שווי שימוש לא נמצא במאגר"}

    alt = dict(value_of_use=rows[0]["value_of_use"], participation=participation,
               electric=(car["drive"] == "חשמלי"),
               label=_car_label(car), cost=car["company_cost"])

    if car["group"] == "משפחתי":
        budget = rules["budgets"][emp["group"]]
        gap = max(0, car["company_cost"] - budget)
        alt["family_share"] = gap * rules["familyTrack"]["gapSplitEmployee"]

    return alt, {"eligible": True, "reason": reason, "car": car}


def build_waiver(emp: dict):
    occ = next((o for o in load_occupations() if o["name"] == emp["occupation"]), None)
    if occ is None:
        return None, {"missing": "occupation", "question": "מה העיסוק שלך?"}
    if not occ["can_waive"]:
        return None, {"eligible": False, "reason": "העיסוק אינו מאפשר ויתור על רכב"}
    return dict(value_of_use=0, participation=0, maintenance=occ["maintenance"],
                label="ויתור על רכב", cost=0), \
        {"eligible": True, "reason": "העיסוק מאפשר ויתור"}


def _today_scenario(emp: dict, catalogue: list, rules: dict):
    """היום: רכב מהקטלוג, ויתור קיים (אחזקה), או חסר."""
    if emp.get("current_maintenance"):
        m = emp["current_maintenance"]
        return dict(value_of_use=0, participation=0, maintenance=m,
                    label=f"ויתור על רכב (אחזקה {m:,.0f} ₪)", cost=0), None
    car = next((c for c in catalogue if c["id"] == emp.get("current_car_id")), None)
    if car is None:
        return None, {"missing": "current_car", "question": "מה הרכב שלך היום?"}
    if emp.get("current_participation") is None:
        return None, {"missing": "current_participation",
                      "question": "מה ההשתתפות שלך ברכב היום?"}
    rows = get_value_of_use(TAX_YEAR, car["reg_year"], 1, car["maker_code"], car["model_code"])
    if len(rows) != 1:
        return None, {"error": "שווי שימוש לא נמצא במאגר"}
    today = dict(value_of_use=rows[0]["value_of_use"],
                 participation=emp["current_participation"],
                 electric=(car["drive"] == "חשמלי"),
                 label=_car_label(car), cost=car["company_cost"])
    if car["group"] == "משפחתי":
        budget = rules["budgets"][emp["group"]]
        gap = max(0, car["company_cost"] - budget)
        today["family_share"] = gap * rules["familyTrack"]["gapSplitEmployee"]
    return today, None


def _explain(today: dict, alt: dict, electric_net: float) -> str:
    parts = []
    tv, av = today.get("value_of_use", 0), alt.get("value_of_use", 0)
    if tv != av:
        parts.append(f"שווי השימוש משתנה מ-{tv:,.0f} ₪ ל-{av:,.0f} ₪")
    tm, am = today.get("maintenance", 0), alt.get("maintenance", 0)
    if am > tm:
        parts.append(f"נוספת אחזקת רכב של {am:,.0f} ₪ (חייבת במס)")
    elif tm > am:
        parts.append(f"בוטלה אחזקת רכב של {tm:,.0f} ₪")
    if alt.get("electric") and not today.get("electric"):
        parts.append(f"נוסף החזר חשמל של {electric_net:,.0f} ₪ נטו (מגולם)")
    tp, ap = today.get("participation", 0), alt.get("participation", 0)
    if tp != ap:
        parts.append(f"ההשתתפות שלך משתנה מ-{tp:,.0f} ₪ ל-{ap:,.0f} ₪")
    if alt.get("family_share"):
        parts.append(f"חלקך בפער במסלול המשפחתי הוא {alt['family_share']:,.0f} ₪")
    return " · ".join(parts)


def _details(s: dict, electric_net: float) -> dict:
    return {
        "label": s.get("label", ""),
        "cost": s.get("cost", 0),
        "participation": s.get("participation", 0),
        "value_of_use": s.get("value_of_use", 0),
        "maintenance": s.get("maintenance", 0),
        "electric_refund": electric_net if s.get("electric") else 0,
        "family_share": s.get("family_share", 0),
    }


def run_comparison(emp: dict, target: str, participation: float | None = None) -> dict:
    """target: מזהה רכב מהקטלוג, או 'WAIVER' לוויתור."""
    rules = load_rules()
    catalogue = load_catalogue()

    today, miss = _today_scenario(emp, catalogue, rules)
    if miss:
        return {"status": "missing", **miss}

    if target == "WAIVER":
        alt, info = build_waiver(emp)
    else:
        alt, info = build_alternative(emp, target, participation)

    if alt is None:
        if "missing" in info:
            return {"status": "missing", **info}
        if info.get("eligible") is False:
            return {"status": "not_eligible", **info}
        return {"status": "error", **info}

    res = compare(emp, today, alt, rules)
    electric_net = rules["electric"]["netBonus"]
    return {
        "status": "ok",
        "reason": info.get("reason"),
        "today": {k: round(v) for k, v in res["today"].items()},
        "alt": {k: round(v) for k, v in res["alt"].items()},
        "diff": round(res["diff"]),
        "explanation": _explain(today, alt, electric_net),
        "details": {
            "group": emp.get("group"),
            "budget": rules["budgets"].get(emp.get("group")),
            "today": _details(today, electric_net),
            "alt": _details(alt, electric_net),
        },
        "inputs": {"today": today, "alt": alt},
    }


def rank_options(emp: dict) -> dict:
    """כל החלופות שהעובד זכאי להן (רכבים + ויתור), ממוינות מהנטו הגבוה לנמוך.
    ההשתתפות בכל רכב היא ברירת המחדל מהקטלוג — לא ההשתתפות בפועל."""
    rules = load_rules()
    catalogue = load_catalogue()

    today, miss = _today_scenario(emp, catalogue, rules)
    if miss:
        return {"status": "missing", **miss}

    options = []
    for car in catalogue:
        if car["id"] == emp.get("current_car_id"):
            continue
        ok, _ = check_eligibility(emp, car, rules)
        if not ok:
            continue
        res = run_comparison(emp, car["id"], car["default_participation"])
        if res["status"] == "ok":
            options.append({
                "target": car["id"],
                "label": res["details"]["alt"]["label"],
                "drive": car["drive"],
                "participation": car["default_participation"],
                "net": res["alt"]["net"],
                "diff": res["diff"],
            })

    waiver, _ = build_waiver(emp)
    if waiver is not None and not emp.get("current_maintenance"):
        res = run_comparison(emp, "WAIVER")
        if res["status"] == "ok":
            options.append({
                "target": "WAIVER",
                "label": res["details"]["alt"]["label"],
                "drive": None,
                "participation": 0,
                "net": res["alt"]["net"],
                "diff": res["diff"],
            })

    options.sort(key=lambda o: o["net"], reverse=True)
    today_net = options[0]["net"] - options[0]["diff"] if options else None
    return {"status": "ok", "today_net": today_net,
            "today_label": today.get("label", ""), "options": options}