import contextvars
import logging
import os
import re
import threading
import time

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types

from app import store, turns
from app.config import load_catalogue, load_rules
from app.comparison import eligible_options, run_comparison, today_snapshot
from app.timing import TurnStats

log = logging.getLogger("vehicle_advisor")

load_dotenv()
# ברירת מחדל לפי מדידה מול Gemini (11/2026): שני המודלים החזירו מספרים והסבר מדויקים מהכלים;
# ה-lite היה המהיר והיציב ביותר (1.1-1.8 שניות לתשובה הראשונה, 3 מתוך 3), ו-3.5-flash כגיבוי.
DEFAULT_MODELS = "gemini-3.5-flash-lite,gemini-3.5-flash"


def _models_from_env() -> list[str]:
    """GEMINI_MODELS (לפי סדר הגיבוי), או GEMINI_MODEL יחיד, או ברירת המחדל."""
    raw = os.getenv("GEMINI_MODELS") or os.getenv("GEMINI_MODEL") or DEFAULT_MODELS
    return [m.strip() for m in raw.split(",") if m.strip()]


MODELS = _models_from_env()
_client = None
_client_lock = threading.Lock()

SYSTEM_PROMPT = """את "יועצת הרכב של שטראוס": עוזרת בעברית שעונה לעובדת על השאלה "מה יקרה לנטו שלי?" אם תעבור לרכב אחר, לרכב חשמלי או תוותר על הרכב.

לשון:
- דברי על עצמך בלשון נקבה ("אני צריכה", "אני בודקת", "שאלתי").
- פני למשתמשת בלשון נקבה ("את", "כתבי", "יש לך").
- כשמדברים על ויתור על הרכב, אמרי רק "תוותרי על הרכב" או "ויתור על הרכב" ולא ניסוחים אחרים.

כללים מחייבים:
1. לעולם אל תחשבי נטו, מס, שווי שימוש או זכאות בעצמך. כל מספר מגיע רק מהכלים.
2. אל תנחשי ואל תמציאי נתונים או זכאות. אם חסר נתון, שאלי את העובדת, ושאלי רק על מה שחסר.
3. כל נתון שהעובדת מוסרת שמרי מיד עם update_profile.
4. נתונים נדרשים: קבוצת זכאות (א-ד), שכר בסיס, עמלות, בונוס, אחוז פנסיה, אחוז קרן השתלמות, נקודות זיכוי, מספר ילדים עד גיל 23, עיסוק, והרכב הנוכחי (או אחזקת רכב אם כבר ויתרה) והשתתפות נוכחית. אם אין עמלות או בונוס, שמרי 0 אחרי שהעובדת אישרה.
5. איך שואלים:
   - בכל הודעה שאלי לכל היותר שתיים-שלוש שאלות, לפי סדר החשיבות: קודם קבוצה ושכר בסיס, אחר כך הפרשות ונקודות זיכוי, ואחר כך ילדים, עיסוק והרכב הנוכחי.
   - אל תציגי רשימה של כל הפרטים החסרים בבת אחת.
   - לפני כל שאלה בדקי בפרופיל מה כבר נשמר, ולעולם אל תשאלי שוב על פרט שכבר נמסר.
6. בסוף כל הודעה של העובדת מצורף [הקשר מהמערכת] עם הפרופיל השמור, הנתונים שחסרים והרכבים שהיא יכולה לבחור (מזהה CAR-xx ושם). קחי משם את מזהה הרכב, ואם צריך העבירי את שם הרכב כפי שהעובדת אמרה. ההקשר אינו הודעה של העובדת: אל תצטטי אותו ואל תזכירי אותו.
   - אפשר לקרוא לכמה כלים באותה תשובה, למשל update_profile ו-compare_alternative יחד.
7. אם חסרה השתתפות ברכב החדש והעובדת לא יודעת, השתמשי ב-ask_agent ואמרי לה: "חסר לי X, שאלתי את הנציג". אל תנחשי. אם ask_agent מחזיר today_net, הוסיפי: "בינתיים, הנטו שלך היום הוא X ₪. ההשוואה המלאה תופיע אחרי שהנציג יענה." (X רק מהכלי). כשמגיעה תשובת נציג בהודעה שמסומנת [תשובת נציג], השתמשי בה מיד והמשיכי לחשב עם compare_alternative, בלי לחכות להודעה נוספת מהעובדת.
8. אם הכלי מחזיר שהעובדת אינה זכאית, הסבירי את הסיבה כפי שהיא ואל תציעי עקיפה.
9. בסיום, הציגי: נטו היום, נטו בחלופה והפרש. אחר כך הציגי את שדה explanation שהכלי החזיר כפי שהוא, בלי שינוי, תוספת או חישוב. אל תציני סכום ברוטו של החזר חשמל; העובדת רואה 250 ₪ נטו בלבד.
10. עברית פשוטה וקצרה, בלי הקדמות ובלי לחזור על מה שכבר נאמר."""

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
    "current_car_id": ("string", "מזהה הרכב הנוכחי בקטלוג, CAR-xx (או שם הרכב)"),
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
        "description": "מחשב השוואה בין המצב היום לחלופה. target הוא מזהה רכב CAR-xx, שם הרכב (למשל ATTO 3), או WAIVER לוויתור. participation היא ההשתתפות ברכב החדש, ורק אם העובד אמר אותה.",
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


def _resolve_car(text) -> tuple[str | None, list[dict]]:
    """מזהה CAR-xx, WAIVER או שם רכב → (מזהה, []). אם השם תואם כמה רכבים או אף אחד → (None, אפשרויות)."""
    t = str(text or "").strip()
    if not t:
        return None, []
    if t.upper() == "WAIVER":
        return "WAIVER", []
    cat = load_catalogue()
    by_id = next((c for c in cat if c["id"].upper() == t.upper()), None)
    if by_id is not None:
        return by_id["id"], []
    key = t.casefold()
    exact = [c for c in cat if key in (c["model"].casefold(), f'{c["maker"]} {c["model"]}'.casefold())]
    found = exact or [c for c in cat if key in c["model"].casefold()]
    if len(found) == 1:
        return found[0]["id"], []
    return None, [{"id": c["id"], "model": c["model"]} for c in found]


def _missing_fields(profile: dict) -> list[str]:
    missing = [k for k in REQUIRED if k not in profile]
    if "current_car_id" not in profile and not profile.get("current_maintenance"):
        missing.append("current_car_id")
    return missing


# ── פלט כלים קומפקטי: רק מה שהמודל צריך כדי לענות (פרטי התצוגה נשמרים בפרופיל) ──
_COMPARISON_KEYS = ("status", "reason", "today", "alt", "diff", "explanation")


def _compact_result(name: str, result):
    if not isinstance(result, dict):
        return result
    if name == "compare_alternative" and result.get("status") == "ok":
        return {k: result[k] for k in _COMPARISON_KEYS if k in result}
    if name == "update_profile" and "profile" in result:
        # פורמט ישן (פרופיל מלא) → מה נשמר ומה עוד חסר
        return {"saved": result.get("saved", []), "missing": _missing_fields(result["profile"])}
    if name == "list_cars" and result.get("cars") and "maker" in result["cars"][0]:
        return {"cars": [{"id": c["id"], "model": c["model"], "drive": c["drive"]} for c in result["cars"]]}
    return result


def _compact_history(contents: list) -> list:
    """תוצאות כלים מתורים קודמים — בצורה מצומצמת. הטקסט של השיחה נשאר כמו שהוא."""
    out = []
    for c in contents:
        parts = c.parts or []
        if not any(p.function_response for p in parts):
            out.append(c)
            continue
        new_parts = []
        for p in parts:
            fr = p.function_response
            if fr is not None and isinstance(fr.response, dict) and "result" in fr.response:
                compact = _compact_result(fr.name, fr.response["result"])
                if compact is not fr.response["result"]:
                    p = types.Part.from_function_response(name=fr.name, response={"result": compact})
            new_parts.append(p)
        out.append(types.Content(role=c.role, parts=new_parts))
    return out


def _context_text(profile: dict) -> str:
    """[הקשר מהמערכת] בסוף הודעת העובדת: פרופיל, מה חסר, ורכבים זמינים — כדי לחסוך סבבי כלים.
    בסוף הבקשה ולא בפרומפט — כך תחילת הבקשה (פרומפט + כלים + היסטוריה) קבועה ונשמרת ב-cache."""
    visible = {k: v for k, v in profile.items() if not k.startswith("_")}
    lines = ["[הקשר מהמערכת, לא נכתב על ידי העובדת]",
             "פרופיל שמור: " + (", ".join(f"{k}={v}" for k, v in visible.items()) or "ריק"),
             "חסרים: " + (", ".join(_missing_fields(profile)) or "אין")]
    if profile.get("group"):
        cur = next((c for c in load_catalogue() if c["id"] == profile.get("current_car_id")), None)
        if cur is not None:
            lines.append(f'רכב נוכחי: {cur["id"]} {cur["model"]}')
        opts = [" ".join(x for x in (o["id"], o["label"], o["drive"]) if x)
                for o in eligible_options(profile)]
        lines.append("רכבים שאפשר לבחור: " + "; ".join(opts))
    return "\n".join(lines)


def _run_tool(sid: str, profile: dict, name: str, args: dict) -> dict:
    if name == "update_profile":
        saved = []
        for k, v in args.items():
            if k not in PROFILE_FIELDS:
                continue
            if k == "current_car_id":
                car_id, options = _resolve_car(v)
                if car_id is None or car_id == "WAIVER":
                    # שם רכב שלא זוהה — לא שומרים ניחוש; המודל ישאל את העובדת
                    return {"saved": saved, "status": "unknown_car", "field": k, "options": options}
                v = car_id
            profile[k] = v
            saved.append(k)
        return {"saved": saved, "missing": _missing_fields(profile)}

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
        return {"cars": [{"id": c["id"], "model": c["model"], "drive": c["drive"]} for c in cars]}

    if name == "compare_alternative":
        missing = [k for k in REQUIRED if k not in profile]
        if missing:
            return {"status": "missing_profile", "missing": missing}
        # כלי משולב: מקבל גם שם רכב, כך שלא צריך סבב נפרד של list_cars
        target, options = _resolve_car(args.get("target"))
        if target is None:
            return {"status": "unknown_car", "options": options}
        res = run_comparison(profile, target, args.get("participation"))
        if res.get("status") == "ok":
            profile["_last_comparison"] = res
            profile.pop("_pending_target", None)
        elif res.get("status") == "missing" and res.get("missing") == "participation":
            # נזכור לאיזו חלופה חסרה השתתפות — אם השאלה תישלח לנציג, נציג בינתיים את מצב היום
            profile["_pending_target"] = target
        return _compact_result(name, res)

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
DEFAULT_CALL_TIMEOUT_S = 40


def _call_timeout_from_env() -> float:
    """GEMINI_CALL_TIMEOUT (שניות). ערך חסר או לא תקין → ברירת המחדל."""
    raw = os.getenv("GEMINI_CALL_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_CALL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if value <= 0:
        log.warning("GEMINI_CALL_TIMEOUT=%r is not a positive number; using %ss", raw, DEFAULT_CALL_TIMEOUT_S)
        return DEFAULT_CALL_TIMEOUT_S
    return value


CALL_TIMEOUT_S = _call_timeout_from_env()  # תקרה לקריאה אחת ל-Gemini; קריאה רגילה לוקחת 1-3 שניות
TURN_TIMEOUT_S = 90   # תקרה לתור שלם
MIN_FALLBACK_S = 5    # אחרי timeout עוברים למודל הבא רק אם נשאר לפחות זמן כזה בתור
# כשיש מודל גיבוי — ניסיון לא אחרון מוגבל לזמן קצר: במדידה מודל עמוס החזיר 504 רק אחרי ~38 שניות,
# ותשובה תקינה הגיעה כמעט תמיד תוך כמה שניות. המודל האחרון בתור מקבל את מלוא CALL_TIMEOUT_S.
FALLBACK_AFTER_S = 12
# מודל שנכשל בעומס / timeout / מכסה זז לסוף התור לזמן מה — התור הבא לא מחכה לו שוב
COOLDOWN_S = 120
COOLDOWN_MISSING_S = 3600  # 404: המודל לא זמין למפתח הזה
MAX_OUTPUT_TOKENS = 2048  # תקרת ביטחון; תשובה רגילה קצרה בהרבה
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


def _call_config(config, model: str, deadline: float | None,
                 cap: float | None = None) -> types.GenerateContentConfig:
    """הגדרות לקריאה אחת: timeout (מוגבל בזמן שנשאר לתור, וב-cap אם יש) + חשיבה מינימלית לפי המודל."""
    limit = CALL_TIMEOUT_S if cap is None else min(CALL_TIMEOUT_S, cap)
    remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
    if remaining <= 1:
        raise TurnTimeout()
    update = {"http_options": types.HttpOptions(timeout=_timeout_ms(remaining))}
    thinking = None if model in _THINKING_REJECTED else _thinking_config(model)
    if thinking is not None:
        update["thinking_config"] = thinking
    return config.model_copy(update=update)


# סטטיסטיקת התור הנוכחי (לכל thread בנפרד) — בלי לשנות את החתימה של _generate
_stats: contextvars.ContextVar[TurnStats | None] = contextvars.ContextVar("turn_stats", default=None)


def _call_model(m: str, contents, config, deadline, cap: float | None = None):
    """קריאה אחת למודל, עם זמן וטוקנים בלוג."""
    t0 = time.perf_counter()
    resp = _client.models.generate_content(
        model=m, contents=contents, config=_call_config(config, m, deadline, cap))
    ms = (time.perf_counter() - t0) * 1000
    u = getattr(resp, "usage_metadata", None)
    log.info("gemini %s: %.0f ms (prompt=%s cached=%s output=%s thoughts=%s)", m, ms,
             *(getattr(u, k, None) for k in ("prompt_token_count", "cached_content_token_count",
                                             "candidates_token_count", "thoughts_token_count")))
    stats = _stats.get()
    if stats is not None:
        stats.add_call(m, ms, resp)
    return resp


_cooldown: dict[str, float] = {}  # מודל → עד מתי (monotonic) הוא בסוף התור
_cooldown_lock = threading.Lock()


def _mark_failed(model: str, e: Exception) -> None:
    seconds = COOLDOWN_MISSING_S if _error_code(e) == 404 else COOLDOWN_S
    with _cooldown_lock:
        _cooldown[model] = time.monotonic() + seconds
    log.warning("model %s failed (%s); moved to the end of the order for %ss",
                model, _error_code(e) or type(e).__name__, seconds)


def _model_order() -> list[str]:
    """MODELS לפי הסדר, ומודלים שנכשלו לאחרונה בסוף (ולא בחוץ — אם כולם נכשלו, עדיין מנסים)."""
    now = time.monotonic()
    with _cooldown_lock:
        cooling = {m for m, until in _cooldown.items() if until > now}
    return [m for m in MODELS if m not in cooling] + [m for m in MODELS if m in cooling]


def _generate(contents, config, deadline: float | None = None):
    """מנסה את המודלים לפי הסדר; עובר למודל הבא בשגיאת מודל חסר, מכסה, עומס, 504 או timeout.
    בלי retries ובלי המתנות. ניסיון שיש אחריו גיבוי מוגבל ב-FALLBACK_AFTER_S;
    האחרון — ב-CALL_TIMEOUT_S. תמיד גם בזמן שנשאר לתור."""
    last = None
    order = _model_order()
    for i, m in enumerate(order):
        cap = FALLBACK_AFTER_S if i < len(order) - 1 else None
        try:
            try:
                return _call_model(m, contents, config, deadline, cap)
            except Exception as e:
                if not _is_thinking_rejected(e) or m in _THINKING_REJECTED:
                    raise
                # המודל לא תומך בהגדרת החשיבה — שולחים שוב פעם אחת בלעדיה, וזוכרים
                log.warning("model %s rejected thinking_config; retrying without it", m)
                _THINKING_REJECTED.add(m)
                return _call_model(m, contents, config, deadline, cap)
        except (httpx.TimeoutException, TimeoutError) as e:
            # המודל תקוע — עוברים מיד לבא, אם נשאר מספיק זמן בתור; אחרת הודעה ידידותית
            if deadline is not None and deadline - time.monotonic() < MIN_FALLBACK_S:
                raise TurnTimeout() from e
            _mark_failed(m, e)
            last = e
            continue
        except TurnTimeout:
            raise
        except Exception as e:
            if _error_code(e) is not None:
                _mark_failed(m, e)
                last = e
                continue
            raise
    # כל המודלים נכשלו. אם האחרון חרג מהזמן (504 או timeout) — הודעה ידידותית
    if _error_code(last) == 504 or isinstance(last, (httpx.TimeoutException, TimeoutError)):
        raise TurnTimeout() from last
    raise last


def chat(sid: str, message: str) -> str:
    stats = TurnStats()
    token = _stats.set(stats)
    try:
        out = _chat(sid, message, stats)
    except Exception as e:
        stats.log(f"failed ({type(e).__name__})")
        raise
    finally:
        _stats.reset(token)
    stats.log("ok")
    return out


def warm_client() -> None:
    """client אחד לכל השרת. יצירה לוקחת כמה שניות (SSL), לכן נעשית בעלייה ולא בהודעה הראשונה."""
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(
                api_key=os.environ["GEMINI_API_KEY"],
                # timeout ברירת מחדל לכל בקשה; retry_options=None — בלי ניסיונות חוזרים של הספרייה
                http_options=types.HttpOptions(timeout=_timeout_ms(CALL_TIMEOUT_S), retry_options=None),
            )


# שלב התור, למסך העובדת: בודקת את הנתונים → מחשבת (כלים) → מנסחת תשובה
STAGE_CHECKING, STAGE_COMPUTING, STAGE_WRITING = "checking", "computing", "writing"


def _chat(sid: str, message: str, stats: TurnStats) -> str:
    if _client is None:
        warm_client()
    deadline = time.monotonic() + TURN_TIMEOUT_S
    turns.set_stage(sid, STAGE_CHECKING)

    profile, contents = store.load_session(sid)
    contents = _compact_history(contents)
    base_len = len(contents)

    # תשובות נציג שהגיעו מאז ההודעה האחרונה
    answers = store.pop_answers(sid)
    prefix = "".join(
        f'[תשובת נציג לשאלה "{q["question"]}"]: {q["answer"]}\n' for q in answers
    )
    user_msg = types.Content(role="user", parts=[types.Part(text=prefix + message)])
    # ההקשר נשלח רק בתור הזה — לא נשמר בהיסטוריה ולא מוצג לעובדת
    contents.append(types.Content(role="user", parts=[
        types.Part(text=prefix + message), types.Part(text=_context_text(profile))]))

    def save(history):
        if len(history) > base_len:
            history = history[:base_len] + [user_msg] + history[base_len + 1:]
        store.save_session(sid, profile, history)

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[TOOLS],
        temperature=0,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):  # עד 6 סבבי כלים + תשובה סופית
            resp = _generate(contents, config, deadline)
            contents.append(resp.candidates[0].content)
            calls = resp.function_calls or []
            if not calls:
                save(contents)
                return resp.text or ""
            turns.set_stage(sid, STAGE_COMPUTING)
            parts = []
            for fc in calls:
                t0 = time.perf_counter()
                result = _run_tool(sid, profile, fc.name, dict(fc.args or {}))
                stats.add_tool(fc.name, (time.perf_counter() - t0) * 1000)
                parts.append(types.Part.from_function_response(
                    name=fc.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
            turns.set_stage(sid, STAGE_WRITING)
        # חרגנו ממספר הסבבים — כמו timeout: לא שומרים חצי תור
        raise TurnTimeout()
    except Exception:
        # לא שומרים היסטוריה חצי-גמורה, אבל כן את הפרופיל שנאסף
        save(contents[:base_len])
        # תשובות הנציג לא נוצלו — מחזירים אותן כדי שלא ילכו לאיבוד
        store.unmark_delivered([q["id"] for q in answers])
        raise