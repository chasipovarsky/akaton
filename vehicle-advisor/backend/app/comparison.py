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


def build_alternative(emp: dict, car_id: str, participation: float | None,
                      rules: dict | None = None, catalogue: list | None = None):
    """מחזיר (alt_dict, מידע) או (None, מה חסר).
    rules / catalogue — אופציונלי, כדי לא לקרוא את הקבצים שוב בלולאה."""
    rules = rules or load_rules()
    car = _find_car(catalogue or load_catalogue(), car_id)
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


def eligible_options(emp: dict) -> list[dict]:
    """הרכבים שהעובדת זכאית להם (בלי הרכב הנוכחי), ואם העיסוק מאפשר — גם ויתור.
    רק זכאות מהקטלוג; בלי שליפת שווי שימוש — מהיר, לבורר בממשק."""
    rules = load_rules()
    out = []
    for car in load_catalogue():
        if car["id"] == emp.get("current_car_id"):
            continue
        ok, _ = check_eligibility(emp, car, rules)
        if ok:
            out.append({
                "id": car["id"],
                "label": _car_label(car),
                "maker": car["maker"],
                "group": car["group"],
                "drive": car["drive"],
                "company_cost": car["company_cost"],
                "default_participation": car["default_participation"],
            })
    if not emp.get("current_maintenance") and build_waiver(emp)[0] is not None:
        out.append({"id": "WAIVER", "label": "ויתור על רכב", "maker": None, "group": None,
                    "drive": None, "company_cost": 0, "default_participation": 0})
    return out


def today_snapshot(emp: dict, target: str | None = None) -> dict:
    """'בינתיים: מצב היום' — כשחסר נתון לחלופה (למשל השתתפות) והשאלה נשלחה לנציג.
    מחשב רק את היום; לא ממלא ולא מנחש שום ערך של החלופה."""
    rules = load_rules()
    catalogue = load_catalogue()
    today, miss = _today_scenario(emp, catalogue, rules)
    if miss:
        return {"status": "missing", **miss}
    res = compare(emp, today, today, rules)["today"]
    electric_net = rules["electric"]["netBonus"]
    car = _find_car(catalogue, target) if target else None
    return {
        "status": "pending",
        "today": {k: round(v) for k, v in res.items()},
        "details": {
            "group": emp.get("group"),
            "budget": rules["budgets"].get(emp.get("group")),
            "today": _details(today, electric_net),
        },
        "alt_label": _car_label(car) if car else ("ויתור על רכב" if target == "WAIVER" else None),
    }


def _rank_candidates(emp: dict, rules: dict, catalogue: list):
    """בונה פעם אחת את תרחיש היום ואת כל החלופות שהעובד זכאי להן.
    זכאות, שווי שימוש והשתתפות לא תלויים בשכר — לכן אפשר לחשב נטו שוב ושוב בלי לבנות מחדש."""
    today, miss = _today_scenario(emp, catalogue, rules)
    if miss:
        return None, miss
    cands = []
    for car in catalogue:
        if car["id"] == emp.get("current_car_id"):
            continue
        ok, _ = check_eligibility(emp, car, rules)
        if not ok:
            continue
        alt, _ = build_alternative(emp, car["id"], car["default_participation"],
                                   rules=rules, catalogue=catalogue)
        if alt is not None:
            cands.append((car["id"], alt, car["drive"], car["default_participation"]))
    waiver, _ = build_waiver(emp)
    if waiver is not None and not emp.get("current_maintenance"):
        cands.append(("WAIVER", waiver, None, 0))
    return today, cands


def _rank(emp: dict, today: dict, cands: list, rules: dict) -> tuple[list, int | None]:
    """נטו לכל חלופה — אותו compare ואותו עיגול כמו run_comparison."""
    options, today_net = [], None
    for target, alt, drive, participation in cands:
        res = compare(emp, today, alt, rules)
        today_net = round(res["today"]["net"])
        options.append({
            "target": target,
            "label": alt["label"],
            "drive": drive,
            "participation": participation,
            "net": round(res["alt"]["net"]),
            "diff": round(res["diff"]),
        })
    options.sort(key=lambda o: o["net"], reverse=True)  # מיון יציב: בתיקו — סדר הקטלוג
    return options, today_net


def _with_raise(emp: dict, extra: float) -> dict:
    # הברוטו = בסיס + עמלות + בונוס; העלאה נכנסת לבסיס (ומשפיעה גם על פנסיה וקרן השתלמות)
    return {**emp, "base": emp["base"] + extra}


def _turning_point(emp: dict, today: dict, cands: list, rules: dict,
                   max_pct: float = 1.0, step_pct: float = 0.05) -> dict | None:
    """העלאת הברוטו הקטנה ביותר (בשקלים שלמים, עד +100%) שבה האפשרות הראשונה בדירוג משתנה.
    סריקה בצעדים של 5% מהברוטו, ואז חיפוש בינארי בתוך הצעד שבו נמצא השינוי.
    מגבלה: שינוי שמתהפך בחזרה בתוך צעד אחד לא יימצא."""
    if not cands:
        return None

    def best(extra: int) -> dict:
        return _rank(_with_raise(emp, extra), today, cands, rules)[0][0]

    gross = emp["base"] + emp["commissions"] + emp["bonus"]
    limit = int(round(gross * max_pct))
    step = max(1, int(round(gross * step_pct)))
    first = best(0)["target"]

    lo = 0
    while lo < limit:
        hi = min(lo + step, limit)
        if best(hi)["target"] != first:
            while hi - lo > 1:  # best(lo) == first, best(hi) != first
                mid = (lo + hi) // 2
                if best(mid)["target"] == first:
                    lo = mid
                else:
                    hi = mid
            new = best(hi)
            return {"extra_gross": hi, "new_best": new["label"], "new_best_target": new["target"]}
        lo = hi
    return None


def rank_options(emp: dict, with_turning_point: bool = False) -> dict:
    """כל החלופות שהעובד זכאי להן (רכבים + ויתור), ממוינות מהנטו הגבוה לנמוך.
    ההשתתפות בכל רכב היא ברירת המחדל מהקטלוג — לא ההשתתפות בפועל."""
    rules = load_rules()
    catalogue = load_catalogue()

    today, cands = _rank_candidates(emp, rules, catalogue)
    if today is None:
        return {"status": "missing", **cands}

    options, today_net = _rank(emp, today, cands, rules)
    out = {"status": "ok", "today_net": today_net,
           "today_label": today.get("label", ""), "options": options}
    if with_turning_point:
        out["turning_point"] = _turning_point(emp, today, cands, rules)
    return out
