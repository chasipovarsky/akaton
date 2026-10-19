from app.net_engine import compute_net, compare

E001 = dict(base=18500, commissions=0, bonus=0, pension_pct=6,
            study_pct=2.5, credit_points=2.75)


def test_e001_today():
    res = compute_net(E001, value_of_use=4070, participation=300)
    print(res)
    assert round(res["net"]) == 11032
    assert round(res["tax"]) == 3443
    assert round(res["ni_health"]) == 2153


def test_e001_to_atto3():
    res = compare(E001,
                  today=dict(value_of_use=4070, participation=300),
                  alt=dict(value_of_use=3130, participation=300, electric=True))
    print(res)
    assert round(res["today"]["net"]) == 11032
    assert round(res["alt"]["net"]) == 11693
    assert round(res["diff"]) == 661
    assert round(res["alt"]["electric_gross"]) == 440