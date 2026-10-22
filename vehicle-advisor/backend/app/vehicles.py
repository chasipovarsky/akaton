from pathlib import Path
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_GZ_PATH  = DATA_DIR / "vehicle_value_2026.csv.gz"
_CSV_PATH = DATA_DIR / "vehicle_value_2026.csv"

COLUMN_MAP = {
    "שנת מס": "tax_year",
    "שנת רישום": "reg_year",
    "קוד סוג רכב": "vehicle_type_code",
    "קוד תוצר": "maker_code",
    "תוצר": "maker_name",
    "קוד דגם": "model_code",
    "תאור דגם": "model_desc",
    "סוג דלק": "fuel_type",
    "כינוי מסחרי": "commercial_name",
    "1-חשמלי.2-היברידי.3-פלג-אין": "drive_code",
    "מחיר מחירון": "list_price",
    "שווי שימוש": "value_of_use",
}

KEY = ["tax_year", "reg_year", "vehicle_type_code", "maker_code", "model_code"]

_df = None


def load_vehicles() -> pd.DataFrame:
    global _df
    if _df is None:
        src = _GZ_PATH if _GZ_PATH.exists() else _CSV_PATH
        df = pd.read_csv(src, encoding="cp1255")
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns=COLUMN_MAP)
        df = df[[c for c in COLUMN_MAP.values() if c in df.columns]]
        for col in df.select_dtypes(include=["object", "str"]).columns:
            df[col] = df[col].astype(str).str.strip()
        _df = df
    return _df


def get_value_of_use(tax_year, reg_year, vehicle_type_code, maker_code, model_code):
    """מחזיר רשימת התאמות לפי המפתח הרשמי (בדרך כלל אחת)."""
    df = load_vehicles()
    mask = (
        (df["tax_year"] == int(tax_year))
        & (df["reg_year"] == int(reg_year))
        & (df["vehicle_type_code"] == int(vehicle_type_code))
        & (df["maker_code"] == int(maker_code))
        & (df["model_code"] == int(model_code))
    )
    return df[mask].to_dict("records")