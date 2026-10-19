import traceback

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import store
from app.config import load_catalogue, load_rules
from app.comparison import run_comparison
from app.agent import chat as agent_chat

app = FastAPI(title="Vehicle Advisor")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4200"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CompareRequest(BaseModel):
    employee: dict
    target: str
    participation: float | None = None


class ChatRequest(BaseModel):
    session_id: str
    message: str


class AnswerRequest(BaseModel):
    question_id: int
    answer: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/catalogue")
def catalogue():
    return load_catalogue()


@app.get("/rules")
def rules():
    return load_rules()


@app.post("/compare")
def compare_endpoint(req: CompareRequest):
    return run_comparison(req.employee, req.target, req.participation)


@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    try:
        reply = agent_chat(req.session_id, req.message)
        profile, _ = store.load_session(req.session_id)
        return {"reply": reply, "comparison": profile.get("_last_comparison")}
    except Exception as e:
        traceback.print_exc()
        return {
            "reply": "אירעה תקלה זמנית. אפשר לנסות שוב בעוד רגע.",
            "error": f"{type(e).__name__}: {e}",
        }


@app.get("/chat/{session_id}/state")
def chat_state(session_id: str):
    profile, _ = store.load_session(session_id)
    return {
        "history": store.display_history(session_id),
        "comparison": profile.get("_last_comparison"),
    }


@app.get("/agent/questions")
def agent_questions(open_only: bool = False):
    return store.list_questions(open_only)


@app.post("/agent/answer")
def agent_answer(req: AnswerRequest):
    return {"ok": store.answer_question(req.question_id, req.answer)}