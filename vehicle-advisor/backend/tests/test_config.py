from app.config import load_rules, load_catalogue, load_occupations
from app.vehicles import get_value_of_use

# מדווחים על חריגות במקום להיכשל מיד, כדי לראות את כולן
def test_catalogue_matches_csv():
    bad = []
    for car in load_catalogue():
        rows = get_value_of_use(2026, car["reg_year"], 1, car["maker_code"], car["model_code"])
        if len(rows) != 1:
            bad.append((car["id"], "לא נמצא/כפול", len(rows)))
        elif rows[0]["value_of_use"] != car["value_of_use_sheet"]:
            bad.append((car["id"], rows[0]["value_of_use"], car["value_of_use_sheet"]))
    print("חריגות:", bad)
    assert not bad


def test_counts():
    assert len(load_catalogue()) == 20
    assert len(load_occupations()) == 14
    assert load_rules()["budgets"]["ג"] == 4500