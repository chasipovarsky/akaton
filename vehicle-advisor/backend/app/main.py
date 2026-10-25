import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, APIRouter, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import store, turns
from google.genai import types

from app.config import load_catalogue, load_rules, load_demo_employees, warm_cache
from app.comparison import run_comparison, rank_options, eligible_options
from app.agent import chat as agent_chat, REQUIRED, TurnTimeout

TIMEOUT_MSG = "לא הצלחתי לסיים בזמן, אפשר לנסות שוב."

log = logging.getLogger("vehicle_advisor")
# לוג הזמנים (vehicle_advisor.timing) גלוי בלוג השרת
if not logging.getLogger("vehicle_advisor").handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))
    logging.getLogger("vehicle_advisor").addHandler(_h)
    logging.getLogger("vehicle_advisor").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # חימום: קבצי הנתונים נטענים בעליית השרת, לא בבקשה הראשונה
    try:
        warm_cache()
    except Exception:
        log.exception("warm cache failed")  # לא מפילים את השרת; ייטען בבקשה הראשונה
    yield
    turns.shutdown()


# ── אפליקציה ────────────────────────────────────────────────────────────────
app = FastAPI(title="Vehicle Advisor", docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)

_origins = ["http://localhost:4200"]
_extra = os.getenv("CORS_ORIGINS", "")
if _extra:
    _origins += [o.strip() for o in _extra.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── הגבלת קצב ל-/api/chat ───────────────────────────────────────────────────
_RATE_WINDOW = 600          # שניות (10 דקות)
_RATE_LIMIT   = 20          # בקשות לחלון
_MAX_MSG_LEN  = 1500        # תווים

_RATE_MAX_IPS = 10_000      # מקסימום כתובות שנשמרות בזיכרון

# כתובת → זמני בקשות. סדר המפתחות = לפי הבקשה האחרונה (הישנה ביותר ראשונה)
_rate_store: "OrderedDict[str, list[float]]" = OrderedDict()


def _client_ip(request: Request) -> str:
    # X-Forwarded-For אמין רק מאחורי פרוקסי של שירות האירוח (TRUST_PROXY=1)
    if os.getenv("TRUST_PROXY") == "1":
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # הערך האחרון הוא זה שהפרוקסי שלנו הוסיף
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _check_rate(ip: str) -> bool:
    """True = מותר לעבור. False = חסום."""
    now = time.time()
    # ניקוי כתובות שהבקשה האחרונה שלהן יצאה מחלון הזמן (הן בתחילת הסדר)
    while _rate_store:
        oldest_ip, oldest_hits = next(iter(_rate_store.items()))
        if oldest_hits and now - oldest_hits[-1] < _RATE_WINDOW:
            break
        del _rate_store[oldest_ip]
    hits = [t for t in _rate_store.pop(ip, []) if now - t < _RATE_WINDOW]
    allowed = len(hits) < _RATE_LIMIT
    if allowed:
        hits.append(now)
    _rate_store[ip] = hits
    # הגבלת גודל — מוחקים את הכתובות הישנות ביותר
    while len(_rate_store) > _RATE_MAX_IPS:
        _rate_store.popitem(last=False)
    return allowed


# ── אימות session_id ────────────────────────────────────────────────────────
_SID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")


def _require_sid(session_id: str) -> None:
    if not _SID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="session_id לא תקין")


# ── ראוטר API ────────────────────────────────────────────────────────────────
api = APIRouter(prefix="/api")


class CompareRequest(BaseModel):
    session_id: str
    car_id: str                          # מזהה CAR-xx או WAIVER
    participation: float | None = None   # אם לא נמסר — ההשתתפות ברירת המחדל מהקטלוג


class ChatRequest(BaseModel):
    session_id: str
    message: str


class RankRequest(BaseModel):
    session_id: str


class AnswerRequest(BaseModel):
    question_id: int
    answer: str


@api.get("/health")
def health():
    return {"status": "ok"}


@api.get("/catalogue")
def catalogue():
    return load_catalogue()


@api.get("/rules")
def rules():
    return load_rules()


def _profile_missing(profile: dict) -> list[str]:
    missing = [k for k in REQUIRED if k not in profile]
    if "current_car_id" not in profile and not profile.get("current_maintenance"):
        missing.append("current_car")
    return missing


@api.get("/cars")
def cars_endpoint(session_id: str):
    """רכבים שהעובדת זכאית להם לפי הפרופיל השמור בשיחה. בלי מודל שפה."""
    _require_sid(session_id)
    profile, _ = store.load_session(session_id)
    if "group" not in profile:
        return {"status": "missing_profile", "missing": ["group"], "cars": []}
    return {"status": "ok", "cars": eligible_options(profile)}


@api.post("/compare")
def compare_endpoint(req: CompareRequest, request: Request):
    """השוואה ישירה לרכב מהקטלוג — בלי Gemini, כך שהמערכת עובדת גם כשהמודל איטי או לא זמין.
    משתמש בפרופיל השמור בשיחה ובאותו run_comparison שהבוט מפעיל."""
    _require_sid(req.session_id)
    if not _check_rate(_client_ip(request)):
        return _rate_limited()
    if turns.is_busy(req.session_id):
        return _busy()  # תור של הבוט ישמור בסופו את הפרופיל — לא כותבים במקביל
    profile, history = store.load_session(req.session_id)
    missing = _profile_missing(profile)
    if missing:
        return {"status": "missing_profile", "missing": missing}

    participation, source = req.participation, "employee"
    if participation is None and req.car_id != "WAIVER":
        car = next((c for c in load_catalogue() if c["id"] == req.car_id), None)
        if car is None:
            raise HTTPException(status_code=404, detail="רכב לא נמצא בקטלוג")
        participation, source = car["default_participation"], "catalogue_default"

    res = run_comparison(profile, req.car_id, participation)
    if res.get("status") == "ok":
        res["participation_source"] = source if req.car_id != "WAIVER" else None
        profile["_last_comparison"] = res  # נשמר — אחרי רענון ההשוואה עדיין מוצגת
        profile.pop("_pending_target", None)
        store.save_session(req.session_id, profile, history)
    return res


def _rate_limited() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"reply": "הגעת למגבלת השאלות (20 בכל 10 דקות). אפשר לנסות שוב בעוד כמה דקות.",
                 "error": "rate_limit"},
    )


@api.post("/chat")
def chat_endpoint(req: ChatRequest, request: Request):
    _require_sid(req.session_id)
    if not _check_rate(_client_ip(request)):
        return _rate_limited()
    if len(req.message) > _MAX_MSG_LEN:
        return JSONResponse(
            status_code=400,
            content={"reply": f"ההודעה ארוכה מדי (מקסימום {_MAX_MSG_LEN} תווים).",
                     "error": "message_too_long"},
        )
    if not _start_turn(req.session_id, req.message):
        return _busy()
    return {"status": "pending"}


@api.post("/chat/resume")
def chat_resume(req: RankRequest, request: Request):
    """ממשיך את השיחה לבד כשהגיעה תשובת נציג — בלי הודעה מהעובדת.
    אם אין תשובה שמחכה, לא קוראים למודל בכלל. אחרת — אותו עיקרון: ריצה ברקע."""
    _require_sid(req.session_id)
    status = store.question_status(req.session_id)
    if not status["answers_ready"]:
        profile, _ = store.load_session(req.session_id)
        return {"resumed": False, "comparison": profile.get("_last_comparison"), **status}
    if not _check_rate(_client_ip(request)):
        return _rate_limited()
    # agent.chat מצרף את תשובות הנציג לפני ההודעה; ההודעה עצמה ריקה
    if not _start_turn(req.session_id, ""):
        return _busy()
    return {"status": "pending", "resumed": True}


QUOTA_MSG = "מכסת ה-AI להיום אזלה. המערכת תחזור לפעול מחר — אפשר לנסות שוב אז."
GENERIC_ERROR_MSG = "אירעה תקלה זמנית. אפשר לנסות שוב בעוד רגע."


def _busy() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"reply": "אני עדיין עונה על ההודעה הקודמת. רגע אחד ואפשר להמשיך.",
                 "error": "busy"},
    )


def _turn_error(sid: str):
    def to_message(e: Exception) -> str:
        """חריגה בתור → הודעה ידידותית בעברית. הפרטים בלוג השרת בלבד."""
        if isinstance(e, TurnTimeout):
            log.warning("chat turn timed out (sid=%s)", sid[:8])
            return TIMEOUT_MSG
        if "429" in str(e) or "quota" in str(e).lower() or "Resource has been exhausted" in str(e):
            log.warning("chat quota exhausted (sid=%s)", sid[:8])
            return QUOTA_MSG
        log.exception("chat failed (sid=%s)", sid[:8])
        return GENERIC_ERROR_MSG
    return to_message


def _start_turn(sid: str, message: str) -> bool:
    return turns.start(sid, message, lambda: agent_chat(sid, message), _turn_error(sid))


@api.post("/rank")
def rank_endpoint(req: RankRequest):
    """'מה הכי משתלם לי?' — דירוג כל החלופות לפי הפרופיל שנאסף בשיחה. בלי מודל שפה."""
    _require_sid(req.session_id)
    profile, _ = store.load_session(req.session_id)
    missing = [k for k in REQUIRED if k not in profile]
    if missing:
        return {"status": "missing_profile", "missing": missing}
    return rank_options(profile, with_turning_point=True)


# ── עובדות לדוגמה ───────────────────────────────────────────────────────────
class DemoRequest(BaseModel):
    session_id: str


def _demo_title(p: dict) -> str:
    k = p["base"] / 1000
    return f"קבוצה {p['group']}', {k:g} אלף ₪"


def _demo_summary(e: dict) -> str:
    """הודעת בוט שמסכמת את הפרטים — כך המודל רואה אותם בשיחה ולא שואל שוב."""
    p = e["profile"]
    car = (f"רכב היום {e['car_today']}, השתתפות {p['current_participation']:,} ₪"
           if "current_car_id" in p else f"ללא רכב, אחזקת רכב {p['current_maintenance']:,} ₪")
    return (
        "טענתי את הפרטים של העובדת לדוגמה: "
        f"קבוצה {p['group']}, שכר בסיס {p['base']:,} ₪, עמלות {p['commissions']:,} ₪, "
        f"בונוס {p['bonus']:,} ₪, פנסיה {p['pension_pct']}%, קרן השתלמות {p['study_pct']}%, "
        f"{p['credit_points']} נקודות זיכוי, {p['children']} ילדים עד גיל 23, "
        f"עיסוק {p['occupation']}, {car}.\n"
        'אפשר ללחוץ עכשיו על "מה הכי משתלם לי?", או לשאול אותי על רכב מסוים.'
    )


@api.get("/demo/employees")
def demo_employees():
    return [{"id": e["id"], "title": _demo_title(e["profile"])} for e in load_demo_employees()]


@api.post("/demo/{emp_id}")
def demo_load(emp_id: str, req: DemoRequest):
    _require_sid(req.session_id)
    emp = next((e for e in load_demo_employees() if e["id"] == emp_id), None)
    if emp is None:
        raise HTTPException(status_code=404, detail="עובדת לדוגמה לא נמצאה")
    if turns.is_busy(req.session_id):
        # תור שרץ עכשיו ישמור בסופו את הפרופיל הישן — לא מחליפים באמצע
        return _busy()
    _, history = store.load_session(req.session_id)
    # פרופיל חדש לגמרי (בלי השוואה קודמת), וההיסטוריה ממשיכה
    history += [
        types.Content(role="user", parts=[types.Part(
            text=f"טעינת עובדת לדוגמה: {_demo_title(emp['profile'])}")]),
        types.Content(role="model", parts=[types.Part(text=_demo_summary(emp))]),
    ]
    store.save_session(req.session_id, dict(emp["profile"]), history)
    return {
        "history": store.display_history(req.session_id),
        "comparison": None,
    }


@api.get("/chat/{session_id}/state")
def chat_state(session_id: str):
    _require_sid(session_id)
    # קודם מצב התור, ורק אחר כך הנתונים: אם התור כבר הסתיים, מה שנקרא אחריו סופי.
    # (בסדר ההפוך תור שמסתיים באמצע יחזיר busy=false עם פרופיל ישן)
    turn = turns.status(session_id)
    profile, _ = store.load_session(session_id)
    history = store.display_history(session_id)
    # ההודעה שבטיפול עוד לא נשמרה בהיסטוריה — מציגים אותה כדי שתשרוד רענון
    if turn["busy"] and turn["pending_message"]:
        history.append({"role": "user", "text": turn["pending_message"]})
    return {
        "history": history,
        "comparison": profile.get("_last_comparison"),
        **store.question_status(session_id),
        **turn,
    }


@api.get("/agent/questions")
def agent_questions(open_only: bool = False):
    rows = store.list_questions(open_only)
    # מקצרים session_id ל-8 תווים ראשונים לפני החזרה
    for r in rows:
        r["session_id"] = r["session_id"][:8]
    return rows


@api.post("/agent/answer")
def agent_answer(req: AnswerRequest):
    return {"ok": store.answer_question(req.question_id, req.answer)}


app.include_router(api)

# ── הגשת ממשק Angular (אם תיקיית dist קיימת) ────────────────────────────────
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist" / "frontend" / "browser"

if _DIST.exists():
    _ASSETS = _DIST / "assets"
    if _ASSETS.exists():
        app.mount("/assets", StaticFiles(directory=str(_ASSETS)), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str):
    # נתיבי /api שלא קיימים → 404 (לא index.html)
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found")
    if not _DIST.exists():
        raise HTTPException(status_code=404, detail="Frontend not built")
    root = _DIST.resolve()
    candidate = (root / full_path).resolve()
    if candidate.is_file() and candidate.is_relative_to(root):
        return FileResponse(str(candidate))
    return FileResponse(str(root / "index.html"))
