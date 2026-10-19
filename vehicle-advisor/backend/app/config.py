import json
from pathlib import Path
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RULES_PATH = DATA_DIR / "rules.json"
XLSX_PATH = DATA_DIR / "challenge_data.xlsx"


def load_rules() -> dict:
    with open(RULES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _sheet(name: str) -> pd.DataFrame:
    df = pd.read_excel(XLSX_PATH, sheet_name=name, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_catalogue() -> list[dict]:
    df = _sheet("קטלוג רכבים")
    out = []
    for _, r in df.iterrows():
        out.append({
            "id": r["מזהה"],
            "group": r["קבוצה"],
            "maker": r["תוצר"],
            "maker_code": int(r["קוד תוצר"]),
            "model": r["דגם (כינוי מסחרי)"],
            "model_code": int(r["קוד דגם"]),
            "reg_year": int(r["שנת רישום"]),
            "drive": r["הנעה"],
            "list_price": int(r["מחיר מחירון"]),
            "value_of_use_sheet": int(r["שווי שימוש חודשי"]),
            "company_cost": int(r["עלות חודשית לחברה (דמה)"]),
            "default_participation": int(r["השתתפות עובד ברירת מחדל (דמה)"]),
        })
    return out


def load_occupations() -> list[dict]:
    df = _sheet("עיסוקים")
    out = []
    for _, r in df.iterrows():
        out.append({
            "name": r["עיסוק"],
            "group": r["קבוצת זכאות"],
            "car_mandatory": r["רכב חובה"] == "כן",
            "can_waive": r["מותר לוותר"] == "כן",
            "maintenance": int(r["אחזקת רכב בוויתור (₪)"]),
        })
    return out