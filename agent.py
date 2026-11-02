"""그래프 — 순서 4 단계: guard → classify → gate.

assemble · answer · verify 는 순서 5 에서 gate 뒤에 잇는다(tech_spec 2절). 지금은 gate 를 통과하면
`action="CONTINUE"` 로 끝난다(임시 값, 순서 5 에서 없앤다).
"""
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

import context
import prompts

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# 모델 어댑터 — 10/30 실습과 같은 방식(Gemini 키를 OpenAI 호환 주소로). 이름은 역할별 환경변수로 뺀다(이식 대비).
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
ROLE_ENV = {"router": "CS_ROUTER_MODEL", "answer": "CS_ANSWER_MODEL", "judge": "CS_JUDGE_MODEL"}
ROLE_DEFAULT = {
    "router": "openai:gemini-flash-latest",
    "answer": "openai:gemini-2.5-flash",
    "judge": "openai:gemini-flash-latest",
}
GATE_THRESHOLD = float(os.environ.get("CS_GATE_THRESHOLD", "0.5"))  # PRD 규칙 4 — 초기 0.5, 평가셋으로 다시 정한다

NO_KEY_MESSAGE = (
    "API 키가 없어 모델을 부를 수 없습니다. `.env.example` 을 `.env` 로 복사해 `GEMINI_API_KEY` 를 채운 뒤 다시 실행하세요."
)


def has_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def model_name(role: str) -> str:
    return os.environ.get(ROLE_ENV[role]) or ROLE_DEFAULT[role]


def make_model(role: str):
    """역할(router · answer · judge)에 맞는 채팅 모델. 호출 조건은 10/30 과 같다(temperature 0 · timeout 60 · 재시도 2)."""
    from langchain.chat_models import init_chat_model

    os.environ["OPENAI_API_KEY"] = os.environ["GEMINI_API_KEY"]
    os.environ["OPENAI_BASE_URL"] = GEMINI_BASE_URL
    return init_chat_model(model_name(role), temperature=0, timeout=60, max_retries=2)


# --- 분류 스키마 -----------------------------------------------------------------------------------
class RouteDecision(BaseModel):
    category: Literal["ORDER_PAY", "PRODUCT", "SHIPPING", "CANCEL_REFUND", "OUT_OF_SCOPE"]
    secondary: Literal["ORDER_PAY", "PRODUCT", "SHIPPING", "CANCEL_REFUND"] | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str


class State(TypedDict, total=False):
    question: str
    category: str
    secondary: str | None
    confidence: float
    reason: str
    action: str          # ANSWER · ESCALATE · BLOCK (순서 4 에서는 통과 시 CONTINUE)
    called_tools: list[str]
    context: str
    clause_ids: list[str]
    answer: str
    citations: list[str]
    violations: list[str]
    retries: int
    trace: list[dict]
    notice: str


# --- guard ---------------------------------------------------------------------------------------
# 카드번호 모양(13~19자리, 공백·하이픈 허용)과 비밀번호를 알려 주는 모양. 걸리면 모델에 보내지 않는다.
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
PASSWORD_RE = re.compile(r"(비밀번호|패스워드|password|passwd|pwd)\s*(?:은|는|이|가|:|=)?\s*[A-Za-z0-9!@#$%^&*()_+\-]{4,}", re.I)
BLOCK_MESSAGE = "카드번호나 비밀번호로 보이는 내용이 들어 있어 처리하지 않았습니다. 개인정보는 입력하지 말고 다시 문의해 주세요."


def looks_sensitive(text: str) -> bool:
    return bool(CARD_RE.search(text) or PASSWORD_RE.search(text))


def _log(state: State, **entry) -> list[dict]:
    return [*state.get("trace", []), entry]


def guard(state: State) -> State:
    if looks_sensitive(state["question"]):
        return {"action": "BLOCK", "notice": BLOCK_MESSAGE, "trace": _log(state, node="guard", result="BLOCK")}
    return {"trace": _log(state, node="guard", result="통과")}


# --- classify ------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _router_chain():
    shots = context._load(context.FEWSHOT_PATH)
    system = prompts.build_route_prompt(shots)
    llm = make_model("router").with_structured_output(RouteDecision)
    return system, llm


def classify(state: State) -> State:
    system, llm = _router_chain()
    t = time.time()
    d: RouteDecision = llm.invoke([("system", system), ("human", f"문의: {state['question']}")])
    return {
        "category": d.category,
        "secondary": d.secondary if d.secondary != d.category else None,
        "confidence": d.confidence,
        "reason": d.reason,
        "trace": _log(state, node="classify", category=d.category, secondary=d.secondary,
                      confidence=d.confidence, reason=d.reason, seconds=round(time.time() - t, 1)),
    }


# --- gate — 모델을 부르지 않는다 -------------------------------------------------------------------------
def gate(state: State) -> State:
    out_of_scope = state["category"] == "OUT_OF_SCOPE"
    low = state["confidence"] < GATE_THRESHOLD
    action = "ESCALATE" if out_of_scope or low else "CONTINUE"
    why = "범위 밖" if out_of_scope else f"확신도 {state['confidence']} < {GATE_THRESHOLD}" if low else "통과"
    return {"action": action, "trace": _log(state, node="gate", action=action, why=why)}


def build():
    g = StateGraph(State)
    g.add_node("guard", guard)
    g.add_node("classify", classify)
    g.add_node("gate", gate)
    g.set_entry_point("guard")
    g.add_conditional_edges("guard", lambda s: END if s.get("action") == "BLOCK" else "classify")
    g.add_edge("classify", "gate")
    g.add_edge("gate", END)
    return g.compile()


@lru_cache(maxsize=1)
def _graph():
    return build()


def run(question: str) -> State:
    """문의 한 건을 처리한다. 키가 없으면 모델을 부르지 않고 안내 문구만 돌려준다(P11)."""
    if not has_key():
        return {"question": question, "action": "NO_KEY", "notice": NO_KEY_MESSAGE, "trace": []}
    return _graph().invoke({"question": question, "retries": 0, "trace": []})
