import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

from app import store
from app.config import load_catalogue, load_rules
from app.comparison import run_comparison

load_dotenv()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", MODEL).split(",") if m.strip()]
_client = None

SYSTEM_PROMPT = """את/ה "יועץ הרכב של שטראוס": עוזר/ת בעברית שעונה לעובד על השאלה "מה יקרה לנטו שלי?" אם יעבור לרכב אחר, לרכב חשמלי או יוותר על הרכב.

כללים מחייבים:
1. לעולם אל תחשב נטו, מס, שווי שימוש או זכאות בעצמך. כל מספר מגיע רק מהכלים.
2. אל תנחש ואל תמציא נתונים או זכאות. אם חסר נתון, שאל/י את העובד, ושאל/י רק על מה שחסר.
3. כל נתון שהעובד מוסר שמור מיד עם update_profile.
4. נתונים נדרשים: קבוצת זכאות (א-ד), שכר בסיס, עמלות, בונוס, אחוז פנסיה, אחוז קרן השתלמות, נקודות זיכוי, מספר ילדים עד גיל 23, עיסוק, והרכב הנוכחי (או אחזקת רכב אם כבר ויתר) והשתתפות נוכחית. אם אין עמלות או בונוס, שמור/י 0 לאחר שהעובד אישר.
5. לפני בחירת רכב, השתמש/י ב-list_cars כדי למצוא את המזהה (CAR-xx) לפי השם שהעובד אמר.
6. אם חסרה השתתפות ברכב החדש והעובד לא יודע, השתמש/י ב-ask_agent ואמר/י לעובד: "חסר לי X, שאלתי את הנציג". אל תנחש. כשמגיעה תשובת נציג בהודעה שמסומנת [תשובת נציג], השתמש/י בה והמשך/י לחשב.
7. אם הכלי מחזיר שהעובד אינו זכאי, הסבר/י את הסיבה כפי שהיא ואל תציע/י עקיפה.
8. בסיום, הצג/י: נטו היום, נטו בחלופה והפרש. אחר כך הצג/י את שדה explanation שהכלי החזיר כפי שהוא, ללא שינוי, תוספת או חישוב. אל תציין/י סכום ברוטו של החזר חשמל; העובד רואה 250 ₪ נטו בלבד.
9. עברית פשוטה וקצרה."""

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
        return res

    if name == "ask_agent":
        qid = store.add_question(sid, args["question"])
        return {"status": "sent", "question_id": qid, "note": "השאלה נשלחה לנציג"}

    return {"error": f"כלי לא מוכר: {name}"}


def _generate(contents, config):
    """מנסה את המודלים לפי הסדר; עובר למודל הבא בשגיאת מודל חסר, מכסה או עומס."""
    last = None
    for m in MODELS:
        try:
            return _client.models.generate_content(model=m, contents=contents, config=config)
        except Exception as e:
            if any(code in str(e) for code in ("404", "429", "503")):
                last = e
                continue
            raise
    raise last


def chat(sid: str, message: str) -> str:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    profile, contents = store.load_session(sid)
    base_len = len(contents)

    # תשובות נציג שהגיעו מאז ההודעה האחרונה
    prefix = "".join(
        f'[תשובת נציג לשאלה "{q["question"]}"]: {q["answer"]}\n' for q in store.pop_answers(sid)
    )
    contents.append(types.Content(role="user", parts=[types.Part(text=prefix + message)]))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[TOOLS],
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    try:
        for _ in range(8):  # מגבלה על לולאת כלים
            resp = _generate(contents, config)
            contents.append(resp.candidates[0].content)
            calls = resp.function_calls or []
            if not calls:
                store.save_session(sid, profile, contents)
                return resp.text or ""
            parts = []
            for fc in calls:
                result = _run_tool(sid, profile, fc.name, dict(fc.args or {}))
                parts.append(types.Part.from_function_response(
                    name=fc.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
        store.save_session(sid, profile, contents)
        return "מצטער/ת, משהו השתבש. נסה/י שוב."
    except Exception:
        # לא שומרים היסטוריה חצי-גמורה, אבל כן את הפרופיל שנאסף
        store.save_session(sid, profile, contents[:base_len])
        raise