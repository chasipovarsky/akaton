from app import store


def test_questions_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()

    qid = store.add_question("s1", "מה ההשתתפות?")
    assert len(store.list_questions(open_only=True)) == 1

    assert store.pop_answers("s1") == []          # עוד אין תשובה
    assert store.answer_question(qid, "350 ₪ מ-1/1") is True
    assert store.answer_question(qid, "שוב") is False   # אי אפשר לענות פעמיים

    got = store.pop_answers("s1")
    assert got[0]["answer"] == "350 ₪ מ-1/1"
    assert store.pop_answers("s1") == []          # נמסרה כבר פעם אחת


def test_session_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    store.init_db()
    store.save_session("s1", {"base": 18500, "group": "ג"}, [])
    profile, history = store.load_session("s1")
    assert profile == {"base": 18500, "group": "ג"} and history == []