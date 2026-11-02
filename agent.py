"""그래프 — guard → classify → gate → assemble → answer → verify (tech_spec 2절).

`action` 의 최종 값은 ANSWER · ESCALATE · BLOCK · NO_KEY. `CONTINUE` 는 gate 가 다음 노드로 보낼 때만 쓰는
중간 값이라 전체 그래프의 최종 상태에는 남지 않는다(`until="gate"` 로 분류만 잴 때는 남는다).
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

ESCALATE_MESSAGE = (
    "죄송합니다. 문의하신 내용은 이 안내만으로 정확하게 답변드리기 어려워 담당자에게 넘겨 드리겠습니다. "
    "고객센터를 통해 안내받으실 수 있습니다."
)
FORBIDDEN_TERMS = ["공구", "paid", "confirmed", "refunded", "preparing", "shipped", "delivered"]  # PRD 규칙 9
DEADLINE_UNITS = {"일", "영업일", "시간", "월"}   # 기한 단위는 크기와 상관없이 직접 값만 허용(10/30 교훈)
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
    retry_next: bool     # verify 가 위반을 찾아 한 번 다시 생성하러 갈 때만 True
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


# --- assemble — 읽기 도구를 코드가 부른다(모델이 고르지 않는다) -----------------------------------------
def assemble(state: State) -> State:
    got = context.assemble_context(state["category"], state.get("secondary"))
    return {
        "context": got["context"], "clause_ids": sorted(got["clause_ids"]), "called_tools": got["called_tools"],
        "trace": _log(state, node="assemble", called_tools=got["called_tools"], clauses=len(got["clause_ids"])),
    }


# --- answer ----------------------------------------------------------------------------------------
class AnswerOut(BaseModel):
    action: Literal["ANSWER", "ESCALATE"]
    answer: str


@lru_cache(maxsize=1)
def _answer_llm():
    return make_model("answer").with_structured_output(AnswerOut)


def answer(state: State) -> State:
    t = time.time()
    retry_violations = state.get("violations") if state.get("retries", 0) > 0 else None
    out: AnswerOut = _answer_llm().invoke(prompts.build_answer_messages(state["question"], state["context"], retry_violations))
    return {
        "action": out.action, "answer": out.answer.strip(), "citations": CLAUSE_ID_RE.findall(out.answer),
        "trace": _log(state, node="answer", action=out.action, retry=state.get("retries", 0), seconds=round(time.time() - t, 1)),
    }


# --- verify — 모델을 부르지 않는다. 기계로 검사하는 네 가지(tech_spec 3절) -------------------------------------
CLAUSE_ID_RE = context.CLAUSE_ID_RE
NUMBER_RE = r"\d{1,3}(?:,\d{3})+|\d+"
NUM_UNIT_RE = re.compile(rf"({NUMBER_RE})\s*(영업일|일|시간|개|원|%|월)")


def _ints(text: str) -> set[int]:
    return {int(n.replace(",", "")) for n in re.findall(NUMBER_RE, CLAUSE_ID_RE.sub("", text))}


def check_answer(answer: str, action: str, question: str, context_text: str, clause_ids: set[str]) -> list[str]:
    """위반 목록(비면 통과). ESCALATE 는 단정하는 내용이 없어 금지 표현(④)만 본다."""
    found = []
    low = answer.lower()
    for term in FORBIDDEN_TERMS:                                                       # ④ 금지 표현
        if term in low:
            found.append(f"④ 금지 표현 「{term}」 — 소비자 응대에 쓰지 않는다")
    if action == "ESCALATE":
        return found

    cited = CLAUSE_ID_RE.findall(answer)                                                # ② 인용 번호
    if not cited:
        found.append("② 답변에 근거 조항 번호가 없다")
    for cid in cited:
        if cid not in clause_ids:
            found.append(f"② 인용한 [{cid}] 는 조회한 조항에 없다")

    base = _ints(context_text) | _ints(question)                                       # ① 문서에 없는 숫자
    arithmetic = {x for a in base for b in base for x in (a * b, a + b, abs(a - b))}
    for num, unit in NUM_UNIT_RE.findall(CLAUSE_ID_RE.sub("", answer)):
        v = int(num.replace(",", ""))
        allowed = base if unit in DEADLINE_UNITS else base | arithmetic
        if v not in allowed:
            found.append(f"① 「{num}{unit}」 은 조회한 조항이나 문의에 없는 값이다")

    cues = context.load_mapping()["blank_cues"]                                        # ③ [비움] 단정
    for cid in sorted(clause_ids):
        for cue in cues.get(cid, []):
            if cue in answer:
                found.append(f"③ 「{cue}」 — [{cid}] 은 정해지지 않은 칸인데 사실처럼 말했다")
    return found


def verify(state: State) -> State:
    v = check_answer(state["answer"], state["action"], state["question"], state["context"], set(state["clause_ids"]))
    retries = state.get("retries", 0)
    retry = bool(v) and retries == 0
    return {
        "violations": v, "retries": retries + 1 if retry else retries, "retry_next": retry,
        "trace": _log(state, node="verify", violations=v, next="재생성" if retry else "이관" if v else "통과"),
    }


def escalate(state: State) -> State:
    return {
        "action": "ESCALATE", "answer": ESCALATE_MESSAGE, "citations": [],
        "trace": _log(state, node="escalate", from_=("verify" if state.get("violations") else "gate")),
    }


def _after_gate(s: State) -> str:
    return "escalate" if s["action"] == "ESCALATE" else "assemble"


def _after_verify(s: State) -> str:
    if not s["violations"]:
        return END
    return "answer" if s["retry_next"] else "escalate"


def build(until: str = "end"):
    g = StateGraph(State)
    g.add_node("guard", guard)
    g.add_node("classify", classify)
    g.add_node("gate", gate)
    g.set_entry_point("guard")
    g.add_conditional_edges("guard", lambda s: END if s.get("action") == "BLOCK" else "classify")
    g.add_edge("classify", "gate")
    if until == "gate":
        g.add_edge("gate", END)
        return g.compile()
    g.add_node("assemble", assemble)
    g.add_node("answer", answer)
    g.add_node("verify", verify)
    g.add_node("escalate", escalate)
    g.add_conditional_edges("gate", _after_gate)
    g.add_edge("assemble", "answer")
    g.add_edge("answer", "verify")
    g.add_conditional_edges("verify", _after_verify)
    g.add_edge("escalate", END)
    return g.compile()


@lru_cache(maxsize=2)
def _graph(until: str):
    return build(until)


def run(question: str, until: str = "end") -> State:
    """문의 한 건을 처리한다. 키가 없으면 모델을 부르지 않고 안내 문구만 돌려준다(P11)."""
    if not has_key():
        return {"question": question, "action": "NO_KEY", "notice": NO_KEY_MESSAGE, "trace": []}
    return _graph(until).invoke({"question": question, "retries": 0, "trace": []})


def self_check() -> bool:
    """`verify` 의 네 가지 검사가 정상 답은 통과시키고 어긴 답은 잡는지 — 모델을 부르지 않는다."""
    def ctx(cat, sec=None):
        got = context.assemble_context(cat, sec)
        return got["context"], set(got["clause_ids"])

    cancel, order, product = ctx("CANCEL_REFUND"), ctx("ORDER_PAY"), ctx("PRODUCT")
    ship_cancel = ctx("SHIPPING", "CANCEL_REFUND")
    q_c2 = "개당 10,900원짜리 3개 주문했는데 1개만 취소하면 얼마 돌려받아요?"
    cases = [  # (이름, 답변, action, 질문, (context, ids), 기대 위반 표지)
        ("정상 — C2 모범 답안", "배송 상태가 「준비중」이라면 [CS-cancel-1] 수량이 2개 이상인 주문은 일부만 취소할 수 있습니다. [CS-cancel-2] 환불액은 취소 수량에 단가를 곱한 금액이라 1개를 취소하면 10,900원입니다. [CS-cancel-3]", "ANSWER", q_c2, cancel, []),
        ("정상 — 산술(2개 취소 21,800원)", "2개를 취소하면 환불액은 21,800원입니다. [CS-cancel-3]", "ANSWER", q_c2, cancel, []),
        ("정상 — 출고 기한 1~14 · 기본 3", "출고 기한은 1영업일부터 14영업일 사이이고 기본은 3영업일입니다. [CS-shipping-2]", "ANSWER", "언제 출고돼요?", ctx("SHIPPING"), []),
        ("정상 — 넘기기 안내", "죄송합니다. 정해진 내용이 없어 담당자에게 넘겨 드리겠습니다.", "ESCALATE", "결제는 어떤 방법으로 하나요?", order, []),
        ("① 지어낸 금액", "산술에 없는 금액입니다. 환불액은 12,345원입니다. [CS-cancel-3]", "ANSWER", q_c2, cancel, ["①"]),
        ("① 배송비 3,000원", "배송비 3,000원이 부과됩니다. [CS-shipping-3]", "ANSWER", "배송비 있어요?", ctx("SHIPPING"), ["①"]),
        ("① 기한은 산술 금지(3+3=6영업일)", "출고는 6영업일 이내입니다. [CS-shipping-2]", "ANSWER", "언제 출고돼요?", ctx("SHIPPING"), ["①"]),
        ("①③ 9월 15일 입고 예정(둘 다 잡힘)", "9월 15일에 다시 입고될 예정입니다. [CS-product-13]", "ANSWER", "품절 언제 입고돼요?", product, ["①", "③"]),
        ("② 인용이 조회한 장 밖", "출고는 3영업일입니다. [CS-shipping-2]", "ANSWER", "언제 출고돼요?", order, ["②"]),
        ("② 인용이 아예 없음", "옵션은 직접 골라야 합니다.", "ANSWER", "옵션 안 고르면요?", order, ["②"]),
        ("③ 결제수단 단정", "카카오페이로 결제하실 수 있습니다. [CS-order-1]", "ANSWER", "결제 뭐로 해요?", order, ["③"]),
        ("③ 환불 입금 단정", "취소하면 영업일 3일 이내에 환불됩니다. [CS-cancel-1]", "ANSWER", "환불 며칠?", cancel, ["③"]),
        ("③ 경계 문항의 정상 출고 문장은 안 걸림", "출고 기한은 3영업일이 기본입니다. [CS-shipping-2] 단순 변심 반품·교환은 배송비가 청구됩니다. [CS-cancel-12]", "ANSWER", "배송 관련", ship_cancel, []),
        ("④ 공구가", "공구가로 판매 중입니다. [CS-product-2]", "ANSWER", "가격", product, ["④"]),
        ("④ 영문 상태명", "주문 상태가 refunded 입니다. [CS-cancel-11]", "ANSWER", "상태", cancel, ["④"]),
        ("④ 넘기기에도 금지어 적용", "paid 상태라 넘겨 드리겠습니다.", "ESCALATE", "상태", cancel, ["④"]),
    ]
    ok = True
    print("[verify 단위 검사]")
    for name, ans, action, q, (c, ids), want in cases:
        got = check_answer(ans, action, q, c, ids)
        marks = sorted({v[0] for v in got})
        passed = marks == sorted(want)
        ok &= passed
        print(f"  {'OK' if passed else 'NG'}  {name:34} 위반 {marks or '없음'}" + ("" if passed else f" (기대 {want}) {got}"))
    print("전체:", "통과" if ok else "실패")
    return ok


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(0 if self_check() else 1)
