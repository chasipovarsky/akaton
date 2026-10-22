"""
כיווץ vehicle_value_2026.csv ל-.gz לצורך העלאה ל-Git.
מריצים פעם אחת: python backend/scripts/compress_csv.py
המקור לא נמחק.
"""
import gzip
import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "data" / "vehicle_value_2026.csv"
DST = SRC.with_suffix(".csv.gz")

if not SRC.exists():
    print(f"שגיאה: {SRC} לא נמצא")
    raise SystemExit(1)

with SRC.open("rb") as f_in, gzip.open(DST, "wb") as f_out:
    shutil.copyfileobj(f_in, f_out)

src_mb = SRC.stat().st_size / 1_048_576
dst_mb = DST.stat().st_size / 1_048_576
print(f"נכווץ: {SRC.name} ({src_mb:.1f} MB) → {DST.name} ({dst_mb:.1f} MB)")
