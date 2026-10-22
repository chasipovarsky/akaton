import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from google.genai import types

_default_db = Path(__file__).resolve().parent.parent / "data" / "app.db"
DB_PATH = Path(os.getenv("DB_PATH", str(_default_db)))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            profile TEXT NOT NULL DEFAULT '{}',
            history TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT,
            delivered INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)


def load_session(sid: str):
    """מחזיר (profile, history) כאשר history הוא רשימת Content של Gemini."""
    with _conn() as con:
        row = con.execute("SELECT profile, history FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return {}, []
    history = [types.Content.model_validate_json(s) for s in json.loads(row["history"])]
    return json.loads(row["profile"]), history


def save_session(sid: str, profile: dict, history: list):
    h = json.dumps([c.model_dump_json(exclude_none=True) for c in history])
    with _conn() as con:
        con.execute(
            "INSERT INTO sessions (id, profile, history) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET profile=excluded.profile, history=excluded.history",
            (sid, json.dumps(profile, ensure_ascii=False), h),
        )


def display_history(sid: str) -> list[dict]:
    """רק הודעות טקסט (בלי קריאות כלים), להצגה במסך העובד אחרי רענון."""
    _, history = load_session(sid)
    out = []
    for c in history:
        text = "".join(p.text for p in (c.parts or []) if getattr(p, "text", None))
        text = re.sub(r'\[תשובת נציג לשאלה ".*?"\]: (.*)\n', r'תשובת הנציג: \1\n', text)
        if text:
            out.append({"role": "user" if c.role == "user" else "bot", "text": text})
    return out


def add_question(sid: str, question: str) -> int:
    with _conn() as con:
        cur = con.execute("INSERT INTO questions (session_id, question) VALUES (?, ?)", (sid, question))
        return cur.lastrowid


def list_questions(open_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM questions"
    if open_only:
        sql += " WHERE answer IS NULL"
    sql += " ORDER BY id"
    with _conn() as con:
        return [dict(r) for r in con.execute(sql).fetchall()]


def answer_question(qid: int, answer: str) -> bool:
    with _conn() as con:
        cur = con.execute("UPDATE questions SET answer=? WHERE id=? AND answer IS NULL", (answer, qid))
        return cur.rowcount == 1


def pop_answers(sid: str) -> list[dict]:
    """תשובות נציג שעוד לא נמסרו לשיחה; מסמן אותן כנמסרו."""
    with _conn() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM questions WHERE session_id=? AND answer IS NOT NULL AND delivered=0",
            (sid,)).fetchall()]
        for r in rows:
            con.execute("UPDATE questions SET delivered=1 WHERE id=?", (r["id"],))
    return rows


init_db()