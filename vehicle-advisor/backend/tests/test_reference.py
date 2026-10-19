import pytest
from app.net_engine import compare

EMPS = {
    "E-001": dict(base=18500, commissions=0, bonus=0, pension_pct=6, study_pct=2.5, credit_points=2.75),
    "E-002": dict(base=9800, commissions=2400, bonus=0, pension_pct=6, study_pct=0, credit_points=2.25),
    "E-003": dict(base=21000, commissions=0, bonus=0, pension_pct=6.5, study_pct=2.5, credit_points=2.75),
    "E-004": dict(base=16000, commissions=3000, bonus=0, pension_pct=6, study_pct=2.5, credit_points=2.25),
    "E-005": dict(base=27000, commissions=0, bonus=0, pension_pct=7, study_pct=2.5, credit_points=2.75),
    "E-006": dict(base=32000, commissions=0, bonus=2000, pension_pct=6, study_pct=2.5, credit_points=2.25),
}

CASES = {
    # id: (today, alt, net_today, net_alt, diff)
    "E-001": (dict(value_of_use=4070, participation=300),
              dict(value_of_use=3130, participation=300, electric=True),
              11032, 11693, 661),
    "E-002": (dict(value_of_use=2950, participation=250),
              dict(value_of_use=8120, participation=500, family_share=1150),
              8600, 5078, -3522),
    "E-003": (dict(value_of_use=0, participation=0, maintenance=1500),
              dict(value_of_use=2430, participation=250, electric=True),
              15047, 13109, -1938),
    "E-004": (dict(value_of_use=4790, participation=400),
              dict(value_of_use=2930, participation=300, electric=True),
              10693, 11900, 1207),
    "E-005": (dict(value_of_use=4570, participation=350),
              dict(value_of_use=0, participation=0, maintenance=2000),
              14244, 17806, 3562),
    "E-006": (dict(value_of_use=5150, participation=500),
              dict(value_of_use=4150, participation=500, electric=True),
              17073, 17794, 721),
}


@pytest.mark.parametrize("eid", list(CASES))
def test_reference(eid):
    today, alt, n_today, n_alt, diff = CASES[eid]
    res = compare(EMPS[eid], today, alt)
    print(eid, round(res["today"]["net"], 2), round(res["alt"]["net"], 2), round(res["diff"], 2))
    assert abs(res["today"]["net"] - n_today) <= 1
    assert abs(res["alt"]["net"] - n_alt) <= 1
    assert abs(res["diff"] - diff) <= 1.5