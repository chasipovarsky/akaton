from app.comparison import build_alternative, build_waiver

E002 = dict(group="ב", children=5, occupation="נציג/ת מכירות שטח")
E005 = dict(group="ג", children=1, occupation="מנהל/ת פיתוח")


def test_family_share_e002():
    alt, info = build_alternative(E002, "CAR-19", 500)
    assert alt["value_of_use"] == 8120
    assert alt["family_share"] == 1150


def test_not_eligible_group():
    alt, info = build_alternative(dict(group="א", children=0), "CAR-17", 200)
    assert alt is None and info["eligible"] is False


def test_missing_participation():
    alt, info = build_alternative(E005, "CAR-10", None)
    assert alt is None and info["missing"] == "participation"


def test_waiver_allowed_and_blocked():
    alt, _ = build_waiver(E005)
    assert alt["maintenance"] == 2000
    alt, info = build_waiver(E002)
    assert alt is None and info["eligible"] is False