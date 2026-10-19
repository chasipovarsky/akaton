from app.config import load_rules


def _income_tax(taxable: float, brackets) -> float:
    tax, prev = 0.0, 0.0
    for upper, rate in brackets:
        top = taxable if upper is None else min(taxable, upper)
        if top > prev:
            tax += (top - prev) * rate
        prev = upper if upper is not None else prev
        if upper is None or taxable <= upper:
            break
    return tax


def _two_tier(amount: float, cfg) -> float:
    thr, ceil = cfg["threshold"], cfg["ceiling"]
    low = min(amount, thr) * cfg["lowRate"]
    high = max(0.0, min(amount, ceil) - thr) * cfg["highRate"]
    return low + high


def compute_net(emp: dict, value_of_use: float = 0.0, participation: float = 0.0,
                extra_taxable: float = 0.0, family_share: float = 0.0,
                rules: dict | None = None) -> dict:
    """
    emp: base, commissions, bonus, pension_pct, study_pct, credit_points
    value_of_use: שווי שימוש חודשי (חייב במס, לא משולם במזומן)
    extra_taxable: סכום חייב שמשולם בפועל (אחזקה / החזר חשמל מגולם)
    """
    r = rules or load_rules()
    nm = r["netMethod"]

    gross = emp["base"] + emp["commissions"] + emp["bonus"]
    taxable_income = gross + extra_taxable
    taxable_for_tax = taxable_income + value_of_use

    ni = _two_tier(taxable_for_tax, nm["nationalInsurance"])
    health = _two_tier(taxable_for_tax, nm["health"])

    pension = gross * emp["pension_pct"] / 100
    study = gross * emp["study_pct"] / 100

    pc = nm["pensionCredit"]
    pension_credit = min(pension, pc["maxContributionRate"] * pc["incomeCeiling"]) * pc["rate"]

    tax = _income_tax(taxable_for_tax, nm["brackets"])
    tax -= emp["credit_points"] * nm["creditPointValue"]
    tax -= pension_credit
    tax = max(tax, 0.0)

    net = (taxable_income - tax - ni - health - pension - study
           - participation - family_share)

    return {
        "net": net, "tax": tax, "ni_health": ni + health,
        "taxable": taxable_for_tax,
    }


def gross_up_electric(emp: dict, value_of_use: float, participation: float,
                      base_extra: float = 0.0, family_share: float = 0.0,
                      target_net: float = 250.0, rules: dict | None = None) -> float:
    """מוצא ברוטו X כך שהנטו עולה בדיוק ב-target_net (ללא עיגול)."""
    base_net = compute_net(emp, value_of_use, participation,
                           base_extra, family_share, rules)["net"]
    lo, hi = target_net, target_net * 4
    for _ in range(100):
        mid = (lo + hi) / 2
        n = compute_net(emp, value_of_use, participation,
                        base_extra + mid, family_share, rules)["net"]
        if n - base_net < target_net:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def compare(emp: dict, today: dict, alt: dict, rules: dict | None = None) -> dict:
    """
    today / alt: dict עם value_of_use, participation, maintenance (אחזקה),
    electric (bool), family_share.
    """
    rules = rules or load_rules()
    target = rules["electric"]["netBonus"]

    def run(s):
        extra = s.get("maintenance", 0.0)
        gross_up = 0.0
        if s.get("electric"):
            gross_up = gross_up_electric(emp, s["value_of_use"], s["participation"],
                                         extra, s.get("family_share", 0.0),
                                         target_net=target, rules=rules)
        res = compute_net(emp, s["value_of_use"], s["participation"],
                          extra + gross_up, s.get("family_share", 0.0), rules)
        res["electric_gross"] = gross_up
        return res

    a, b = run(today), run(alt)
    return {"today": a, "alt": b, "diff": b["net"] - a["net"]}