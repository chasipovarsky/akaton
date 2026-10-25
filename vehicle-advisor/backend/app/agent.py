import os
import time

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types

from app import store
from app.config import load_catalogue, load_rules
from app.comparison import run_comparison, today_snapshot
from app.timing import timed

load_dotenv()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", MODEL).split(",") if m.strip()]
_client = None

SYSTEM_PROMPT = """את "יועצת הרכב של שטראוס": עוזרת בעברית שעונה לעובדת על השאלה "מה יקרה לנטו שלי?" אם תעבור לרכב אחר, לרכב חשמלי או תוותר על הרכב.

לשון:
- דברי על עצמך בלשון נקבה ("אני צריכה", "אני בודקת", "שאלתי").
- פני למשתמשת בלשון נקבה ("את", "כתבי", "יש לך").

כללים מחייבים:
1. לעולם אל תחשבי נטו, מס, שווי שימוש או זכאות בעצמך. כל מספר מגיע רק מהכלים.
2. אל תנחשי ואל תמציאי נתונים או זכאות. אם חסר נתון, שאלי את העובדת, ושאלי רק על מה שחסר.
3. כל נתון שהעובדת מוסרת שמרי מיד עם update_profile.
4. נתונים נדרשים: קבוצת זכאות (א-ד), שכר בסיס, עמלות, בונוס, אחוז פנסיה, אחוז קרן השתלמות, נקודות זיכוי, מספר ילדים עד גיל 23, עיסוק, והרכב הנוכחי (או אחזקת רכב אם כבר ויתרה) והשתתפות נוכחית. אם אין עמלות או בונוס, שמרי 0 אחרי שהעובדת אישרה.
5. איך שואלים:
   - בכל הודעה שאלי לכל היותר שתיים-שלוש שאלות, לפי סדר החשיבות: קודם קבוצה ושכר בסיס, אחר כך הפרשות ונקודות זיכוי, ואחר כך ילדים, עיסוק והרכב הנוכחי.
   - אל תציגי רשימה של כל הפרטים החסרים בבת אחת.
   - לפני כל שאלה בדקי בפרופיל מה כבר נשמר, ולעולם אל תשאלי שוב על פרט שכבר נמסר.
6. לפני בחירת רכב, השתמשי ב-list_cars כדי למצוא את המזהה (CAR-xx) לפי השם שהעובדת אמרה.
7. אם חסרה השתתפות ברכב החדש והעובדת לא יודעת, השתמשי ב-ask_agent ואמרי לה: "חסר לי X, שאלתי את הנציג". אל תנחשי. אם ask_agent מחזיר today_net, הוסיפי: "בינתיים, הנטו שלך היום הוא X ₪. ההשוואה המלאה תופיע אחרי שהנציג יענה." (X רק מהכלי). כשמגיעה תשובת נציג בהודעה שמסומנת [תשובת נציג], השתמשי בה מיד והמשיכי לחשב עם compare_alternative, בלי לחכות להודעה נוספת מהעובדת.
8. אם הכלי מחזיר שהעובדת אינה זכאית, הסבירי את הסיבה כפי שהיא ואל תציעי עקיפה.
9. בסיום, הציגי: נטו היום, נטו בחלופה והפרש. אחר כך הציגי את שדה explanation שהכלי החזיר כפי שהוא, בלי שינוי, תוספת או חישוב. אל תציני סכום ברוטו של החזר חשמל; העובדת רואה 250 ₪ נטו בלבד.
10. עברית פשוטה וקצרה."""

PROFILE_FIELDS = {
    "base": ("number", "שכר בסיס חודשי"),
    "commissions": ("number", "עמלות חודשיות"),
    "bonus": ("number", "בונוס חודשי"),
    "pension_pct": ("number", "אחוז פנסיה עובד"),
    "study_pct": ("number", "אחוז קרן השתלמות עובד"),
    "credit_points": ("number", "נקודות זיכוי"),
    "group": ("string", "קבוצת זכאות: א, ב, ג או ד"),
    "children": ("integer", "מספר ילדים עד גיל 23"),
    "occupation": ("string", "העיסוק, כפי שמופיע בטבלת העיסוקים"),
    "current_car_id": ("string", "מזהה הרכב הנוכחי בקטלוג, CAR-xx"),
    "current_participation": ("number", "השתתפות העובד ברכב הנוכחי"),
    "current_maintenance": ("number", "אחזקת רכב אם העובד כבר ויתר על הרכב"),
}
REQUIRED = ["base", "commissions", "bonus", "pension_pct", "study_pct",
            "credit_points", "group", "children", "occupation"]

TOOLS = types.Tool(function_declarations=[
    {
        "name": "update_profile",
        "description": "שומר נתוני עובד שנמסרו בשיחה. שלח/י רק את השדות שהעובד אמר.",
        "parameters": {
            "type": "object",
            "properties": {k: {"type": t, "description": d}
                           for k, (t, d) in PROFILE_FIELDS.items()},
        },
    },
    {
        "name": "list_cars",
        "description": "מחזיר את רכבי הקטלוג שהעובד יכול לבחור מהם לפי הקבוצה שנשמרה בפרופיל.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "compare_alternative",
        "description": "מחשב השוואה בין המצב היום לחלופה. target הוא מזהה רכב CAR-xx או WAIVER לוויתור. participation היא ההשתתפות ברכב החדש, ורק אם העובד אמר אותה.",
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "participation": {"type": "number"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "ask_agent",
        "description": "שולח שאלה לנציג התחבורה כשחסר מידע שהעובד לא יודע.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
])


def _run_tool(sid: str, profile: dict, name: str, args: dict) -> dict:
    if name == "update_profile":
        for k, v in args.items():
            if k in PROFILE_FIELDS:
                profile[k] = v
        visible = {k: v for k, v in profile.items() if not k.startswith("_")}
        return {"saved": list(args), "profile": visible}

    if name == "list_cars":
        rules = load_rules()
        group = profile.get("group")
        if not group:
            return {"missing": "group", "question": "באיזו קבוצת זכאות (א-ד) את/ה?"}
        order = rules["groupOrder"]
        cars = [c for c in load_catalogue()
                if c["group"] in order and order.index(c["group"]) <= order.index(group)]
        if profile.get("children", 0) >= rules["familyTrack"]["minChildren"]:
            cars += [c for c in load_catalogue() if c["group"] == "משפחתי"]
        return {"cars": [{"id": c["id"], "group": c["group"], "maker": c["maker"],
                          "model": c["model"], "drive": c["drive"]} for c in cars]}

    if name == "compare_alternative":
        missing = [k for k in REQUIRED if k not in profile]
        if missing:
            return {"status": "missing_profile", "missing": missing}
        res = run_comparison(profile, args["target"], args.get("participation"))
        if res.get("status") == "ok":
            profile["_last_comparison"] = res
            profile.pop("_pending_target", None)
        elif res.get("status") == "missing" and res.get("missing") == "participation":
            # נזכור לאיזו חלופה חסרה השתתפות — אם השאלה תישלח לנציג, נציג בינתיים את מצב היום
            profile["_pending_target"] = args["target"]
        return res

    if name == "ask_agent":
        qid = store.add_question(sid, args["question"])
        target = profile.get("_pending_target")
        if target:
            snap = today_snapshot(profile, target)
            if snap["status"] == "pending":
                profile["_last_comparison"] = snap
                return {"status": "sent", "question_id": qid, "note": "השאלה נשלחה לנציג",
                        "today_net": snap["today"]["net"]}
        return {"status": "sent", "question_id": qid, "note": "השאלה נשלחה לנציג"}

    return {"error": f"כלי לא מוכר: {name}"}


class TurnTimeout(Exception):
    """התור לא הסתיים בזמן (תקרת זמן או מספר סבבי כלים) — מחזירים הודעה ידידותית."""


CALL_TIMEOUT_S = 30   # תקרה לקריאה אחת ל-Gemini
TURN_TIMEOUT_S = 90   # תקרה לתור שלם
MAX_TOOL_ROUNDS = 6   # סבבי כלים בתור אחד
_FALLBACK_CODES = (404, 429, 503)  # מודל חסר / מכסה / עומס — רק אז עוברים למודל הגיבוי


def _is_fallback_error(e: Exception) -> bool:
    code = getattr(e, "code", None)
    if code in _FALLBACK_CODES:
        return True
    return any(str(c) in str(e) for c in _FALLBACK_CODES)


def _generate(contents, config, deadline: float | None = None):
    """מנסה את המודלים לפי הסדר; עובר למודל הבא רק בשגיאת מודל חסר, מכסה או עומס.
    בלי retries ובלי המתנות: כל ניסוי מוגבל ב-30 שניות או בזמן שנשאר לתור."""
    last = None
    for m in MODELS:
        remaining = CALL_TIMEOUT_S if deadline is None else min(CALL_TIMEOUT_S, deadline - time.monotonic())
        if remaining <= 1:
            raise TurnTimeout()
        cfg = config.model_copy(update={"http_options": types.HttpOptions(timeout=int(remaining * 1000))})
        try:
            with timed(f"gemini {m}"):
                return _client.models.generate_content(model=m, contents=contents, config=cfg)
        except (httpx.TimeoutException, TimeoutError) as e:
            raise TurnTimeout() from e  # לא מנסים מודל אחר על timeout — זה רק יאריך
        except Exception as e:
            if _is_fallback_error(e):
                last = e
                continue
            raise
    raise last


def chat(sid: str, message: str) -> str:
    with timed("bot turn total"):
        return _chat(sid, message)


def _chat(sid: str, message: str) -> str:
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            # timeout ברירת מחדל לכל בקשה; retry_options=None — בלי ניסיונות חוזרים של הספרייה
            http_options=types.HttpOptions(timeout=CALL_TIMEOUT_S * 1000, retry_options=None),
        )
    deadline = time.monotonic() + TURN_TIMEOUT_S

    profile, contents = store.load_session(sid)
    base_len = len(contents)

    # תשובות נציג שהגיעו מאז ההודעה האחרונה
    answers = store.pop_answers(sid)
    prefix = "".join(
        f'[תשובת נציג לשאלה "{q["question"]}"]: {q["answer"]}\n' for q in answers
    )
    contents.append(types.Content(role="user", parts=[types.Part(text=prefix + message)]))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[TOOLS],
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):  # עד 6 סבבי כלים + תשובה סופית
            resp = _generate(contents, config, deadline)
            contents.append(resp.candidates[0].content)
            calls = resp.function_calls or []
            if not calls:
                store.save_session(sid, profile, contents)
                return resp.text or ""
            parts = []
            for fc in calls:
                with timed(f"tool {fc.name}"):
                    result = _run_tool(sid, profile, fc.name, dict(fc.args or {}))
                parts.append(types.Part.from_function_response(
                    name=fc.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
        # חרגנו ממספר הסבבים — כמו timeout: לא שומרים חצי תור
        raise TurnTimeout()
    except Exception:
        # לא שומרים היסטוריה חצי-גמורה, אבל כן את הפרופיל שנאסף
        store.save_session(sid, profile, contents[:base_len])
        # תשובות הנציג לא נוצלו — מחזירים אותן כדי שלא ילכו לאיבוד
        store.unmark_delivered([q["id"] for q in answers])
        raise