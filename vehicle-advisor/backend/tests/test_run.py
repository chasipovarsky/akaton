from app.comparison import run_comparison

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5,
            credit_points=2.75, group="ג", children=2, occupation="מנהל/ת מוצר",
            current_car_id="CAR-09", current_participation=300)


def test_e001_end_to_end():
    r = run_comparison(E001, "CAR-10", participation=300)
    assert r["status"] == "ok"
    assert r["today"]["net"] == 11032
    assert r["alt"]["net"] == 11693
    assert r["diff"] == 661


def test_asks_instead_of_guessing():
    r = run_comparison(E001, "CAR-10")
    assert r["status"] == "missing" and r["missing"] == "participation"


def test_not_eligible():
    r = run_comparison(E001, "CAR-17", participation=500)
    assert r["status"] == "not_eligible"


E002 = dict(base=9800, commissions=2400, bonus=0, pension_pct=6, study_pct=0,
            credit_points=2.25, group="ב", children=5, occupation="נציג/ת מכירות שטח",
            current_car_id="CAR-19", current_participation=500)

def test_family_car_today():
    r = run_comparison(E002, "CAR-05", participation=250)
    assert abs(r["today"]["net"] - 5078) <= 1
    assert abs(r["alt"]["net"] - 8600) <= 1
