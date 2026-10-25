import logging
import os
import re
import time

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types

from app import store
from app.config import load_catalogue, load_rules
from app.comparison import run_comparison, today_snapshot
from app.timing import timed

log = logging.getLogger("vehicle_advisor")

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


# הערה על יחידות: HttpOptions.timeout ב-google-genai הוא באלפיות שנייה. הספרייה ממירה אותו
# לשניות עבור httpx, ושולחת גם כותרת X-Server-Timeout (בשניות) — שרת Gemini עוצר את הקריאה
# בעצמו אחרי הזמן הזה ומחזיר 504 DEADLINE_EXCEEDED.
CALL_TIMEOUT_S = 60   # תקרה לקריאה אחת ל-Gemini (נשלח כ-60000 ms)
TURN_TIMEOUT_S = 90   # תקרה לתור שלם
MAX_TOOL_ROUNDS = 6   # סבבי כלים בתור אחד
# מודל חסר / מכסה / עומס / חריגת זמן בצד השרת — רק אז עוברים למודל הגיבוי
_FALLBACK_CODES = (404, 429, 503, 504)


def _timeout_ms(seconds: float) -> int:
    return int(seconds * 1000)


# ── "חשיבה" מינימלית: הבוט מפעיל כלים ומעביר מספרים מהם — אין צורך בחשיבה ארוכה ──
# לפי google-genai 2.31: thinking_budget נתמך רק במודלי 2.5 (0 = כבוי);
# ממודלי 3.x משתמשים ב-thinking_level (ו-thinking_budget שם מחזיר שגיאה).
_THINKING_REJECTED: set[str] = set()  # מודלים שדחו את ההגדרה — לא נשלח להם שוב


def _thinking_config(model: str) -> types.ThinkingConfig | None:
    name = model.lower().removeprefix("models/")
    if name.startswith("gemini-2.5"):
        if "pro" in name:
            # ב-2.5 Pro אי אפשר לכבות חשיבה — התקציב המינימלי
            return types.ThinkingConfig(thinking_budget=128)
        return types.ThinkingConfig(thinking_budget=0)  # flash / flash-lite: בלי חשיבה
    if re.match(r"gemini-[3-9]", name):
        # LOW נתמך גם ב-Flash וגם ב-Pro של דור 3 (MINIMAL — רק בחלק מהמודלים)
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
    return None  # מודל ישן / לא מוכר — לא שולחים הגדרה שאולי לא נתמכת


def _is_thinking_rejected(e: Exception) -> bool:
    """400 מהשרת שמתלונן על הגדרת החשיבה — נוותר עליה במקום להפיל את הקריאה."""
    return getattr(e, "code", None) == 400 and "thinking" in str(e).lower()


def _error_code(e: Exception) -> int | None:
    code = getattr(e, "code", None)
    if code in _FALLBACK_CODES:
        return code
    return next((c for c in _FALLBACK_CODES if str(c) in str(e)), None)


def _call_config(config, model: str, deadline: float | None) -> types.GenerateContentConfig:
    """הגדרות לקריאה אחת: timeout (מוגבל בזמן שנשאר לתור) + חשיבה מינימלית לפי המודל."""
    remaining = CALL_TIMEOUT_S if deadline is None else min(CALL_TIMEOUT_S, deadline - time.monotonic())
    if remaining <= 1:
        raise TurnTimeout()
    update = {"http_options": types.HttpOptions(timeout=_timeout_ms(remaining))}
    thinking = None if model in _THINKING_REJECTED else _thinking_config(model)
    if thinking is not None:
        update["thinking_config"] = thinking
    return config.model_copy(update=update)


def _generate(contents, config, deadline: float | None = None):
    """מנסה את המודלים לפי הסדר; עובר למודל הבא רק בשגיאת מודל חסר, מכסה, עומס או 504.
    בלי retries ובלי המתנות: כל ניסוי מוגבל ב-60 שניות או בזמן שנשאר לתור."""
    last = None
    for m in MODELS:
        try:
            try:
                with timed(f"gemini {m}"):
                    return _client.models.generate_content(
                        model=m, contents=contents, config=_call_config(config, m, deadline))
            except Exception as e:
                if not _is_thinking_rejected(e) or m in _THINKING_REJECTED:
                    raise
                # המודל לא תומך בהגדרת החשיבה — שולחים שוב פעם אחת בלעדיה, וזוכרים
                log.warning("model %s rejected thinking_config; retrying without it", m)
                _THINKING_REJECTED.add(m)
                with timed(f"gemini {m} (no thinking_config)"):
                    return _client.models.generate_content(
                        model=m, contents=contents, config=_call_config(config, m, deadline))
        except (httpx.TimeoutException, TimeoutError) as e:
            raise TurnTimeout() from e  # לא מנסים מודל אחר על timeout — זה רק יאריך
        except TurnTimeout:
            raise
        except Exception as e:
            if _error_code(e) is not None:
                last = e
                continue
            raise
    # כל המודלים נכשלו. אם האחרון חרג מהזמן בצד השרת (504) — זה timeout: הודעה ידידותית
    if _error_code(last) == 504:
        raise TurnTimeout() from last
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
            http_options=types.HttpOptions(timeout=_timeout_ms(CALL_TIMEOUT_S), retry_options=None),
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