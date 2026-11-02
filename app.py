"""데모 — 문의 한 건을 넣으면 답변 · 호출한 도구 · 근거 조항 · 검증 결과 · 넘김 여부를 한 화면에 보여 준다.

실행: `streamlit run app.py` (키가 없으면 안내 문구만 나온다). `?q=문의 내용` 으로 열면 그 문의를 바로 돌린다.
사이드바 예시는 `data/fewshot.json`(예시용)에서만 가져온다 — 평가용 문항은 쓰지 않는다(tech_spec 4절).
"""
import re

import streamlit as st

import agent
import context

# 예시용 문항에 없는 시연 입력 두 가지 — 평가셋(`goldenset.json`)에도 없다.
DEMO_EXTRA = [
    ("정해지지 않은 칸 → 넘기기", "영수증 받을 수 있어요?"),
    ("정해지지 않은 부분만 빼고 답함", "택배사는 어디예요?"),
    ("카드번호 모양 입력 → 차단", "카드번호 4111 1111 1111 1111 인데 결제가 안 돼요"),
]
CHECKS = [
    ("①", "문서에 없는 숫자·기한·금액"),
    ("②", "인용 번호가 조회한 조항 안에 있는가"),
    ("③", "정해지지 않은(`[비움]`) 칸을 사실처럼 말했는가"),
    ("④", "금지 표현(`공구` · 내부 영문 상태명)"),
]
ACTION_LABEL = {
    "ANSWER": ("답변", "success"),
    "ESCALATE": ("사람에게 넘김", "warning"),
    "BLOCK": ("차단 — 모델에 보내지 않음", "error"),
    "NO_KEY": ("API 키 없음", "info"),
}

st.set_page_config(page_title="링커밋 소비자 응대 에이전트", page_icon="💬", layout="wide")


@st.cache_data
def clause_table() -> dict:
    return {c.id: c for ch in context.load_chapters().values() for c in ch["clauses"]}


@st.cache_data
def category_labels() -> dict:
    return {k: v["label"] for k, v in context.load_mapping()["categories"].items()}


def sidebar_examples() -> list[tuple[str, str]]:
    shots = context._load(context.FEWSHOT_PATH)
    return [(f"{s['id']} · {category_labels()[s['category']]}", s["question"]) for s in shots] + DEMO_EXTRA


def short(text: str, n: int = 34) -> str:
    first = re.split(r"[.,]", text, maxsplit=1)[0].strip()
    return first if len(first) <= n else first[:n] + "…"


def run_question(q: str) -> None:
    with st.spinner("문의를 분석하고 있습니다…"):
        try:
            st.session_state["result"] = agent.run(q)
            st.session_state["error"] = None
        except Exception as e:  # 네트워크·할당량 오류는 화면에 그대로 보여 준다
            st.session_state["result"] = None
            st.session_state["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    st.session_state["question"] = q


def escalation_reason(s: dict) -> str:
    trace = s.get("trace", [])
    gate = next((t for t in trace if t["node"] == "gate" and t.get("action") == "ESCALATE"), None)
    if gate:
        return f"분류 단계에서 넘김 — {gate['why']}"
    esc = next((t for t in trace if t["node"] == "escalate"), None)
    if esc and esc.get("from_") == "verify":
        return "검증에서 위반이 나와 한 번 다시 생성했지만 또 위반이라 넘김"
    return "조회한 조항에 답이 없거나 정해지지 않은 칸이라 답을 지어내지 않고 넘김"


def show_answer(s: dict) -> None:
    label, kind = ACTION_LABEL.get(s.get("action"), (s.get("action"), "info"))
    box = {"success": st.success, "warning": st.warning, "error": st.error, "info": st.info}[kind]
    body = s.get("answer") or s.get("notice") or ""
    box(f"**{label}**\n\n{body}")
    if s.get("action") == "ESCALATE":
        st.caption("넘긴 이유: " + escalation_reason(s))


def show_tools(s: dict) -> None:
    st.markdown("##### ① 분류와 호출한 도구")
    if not s.get("category"):
        st.write("모델을 부르지 않았습니다." if s.get("action") in ("BLOCK", "NO_KEY") else "분류 기록 없음")
        return
    sec = category_labels().get(s.get("secondary") or "", "없음")
    st.write(f"카테고리 **{category_labels()[s['category']]}** · 보조 카테고리 {sec} · 확신도 {s['confidence']}")
    st.caption(s.get("reason", ""))
    tools = s.get("called_tools")
    if tools:
        st.write("호출한 읽기 도구: " + " · ".join(f"`{t}`" for t in tools))
    elif tools is not None:
        st.write("호출한 읽기 도구: 없음(이관)")
    else:
        st.write("호출한 읽기 도구: 없음(분류 단계에서 넘김)")


def show_clauses(s: dict) -> None:
    st.markdown("##### ② 근거 조항")
    cited = list(dict.fromkeys(s.get("citations") or []))
    if not cited:
        st.write("답변에 인용한 조항이 없습니다." if s.get("action") != "BLOCK" else "해당 없음")
        if s.get("clause_ids"):
            st.caption(f"조회한 조항 {len(s['clause_ids'])}개 중 인용 0개")
        return
    table = clause_table()
    for cid in cited:
        c = table.get(cid)
        if not c:
            st.write(f"`[{cid}]` — 조회한 조항에 없는 번호")
            continue
        with st.expander(f"근거: [{cid}] {short(c.text)}"):
            st.write(c.text)
            st.caption(("확정 · 출처 " + c.source) if c.tag == "확정" else "정해지지 않은 칸")
    st.caption(f"조회한 조항 {len(s.get('clause_ids', []))}개 중 인용 {len(cited)}개")


def show_verify(s: dict) -> None:
    st.markdown("##### ③ 검증 결과")
    verifies = [t for t in s.get("trace", []) if t["node"] == "verify"]
    if not verifies:
        st.write("검증 단계에 가지 않았습니다." + (" (분류 단계에서 넘김)" if s.get("action") == "ESCALATE" else ""))
        return
    final = verifies[-1]["violations"]
    answer_escalated = s.get("action") == "ESCALATE" and not final and not any(t["node"] == "escalate" for t in s["trace"])
    for mark, name in CHECKS:
        hit = [v for v in final if v.startswith(mark)]
        if answer_escalated and mark != "④":
            st.write(f"➖ {mark} {name} — 넘기기 답변이라 검사하지 않음")
        elif hit:
            st.write(f"❌ {mark} {name}")
            for v in hit:
                st.caption(v)
        else:
            st.write(f"✅ {mark} {name}")
    if s.get("retries"):
        first = verifies[0]["violations"]
        st.info("1차 답변이 위반이라 한 번 다시 생성했습니다: " + " / ".join(first))


def main() -> None:
    st.title("💬 링커밋 소비자 응대 에이전트")
    st.caption("근거 조항만으로 답하고, 근거가 없으면 사람에게 넘깁니다. · 분류 "
               f"`{agent.model_name('router')}` · 답변 `{agent.model_name('answer')}` · 채점은 평가에서만 사용")

    with st.sidebar:
        st.subheader("예시 문의")
        st.caption("예시용 문항과 시연용 입력만 있습니다(평가용 문항은 쓰지 않음).")
        for i, (label, q) in enumerate(sidebar_examples()):
            if st.button(f"{label}\n\n{q}", key=f"ex{i}", use_container_width=True):
                st.session_state["input"] = q
                st.session_state["autorun"] = q

    if not agent.has_key():
        st.warning(agent.NO_KEY_MESSAGE)

    st.session_state.setdefault("input", "")
    q_param = st.query_params.get("q")
    if q_param and st.session_state.get("last_q_param") != q_param:
        st.session_state["last_q_param"] = q_param
        st.session_state["input"], st.session_state["autorun"] = q_param, q_param

    question = st.text_area("소비자 문의", key="input", height=90, placeholder="예: 주문 취소하고 싶은데 어디서 눌러요?")
    clicked = st.button("문의하기", type="primary", disabled=not agent.has_key())
    auto = st.session_state.pop("autorun", None)
    if (clicked and question.strip()) or (auto and agent.has_key()):
        run_question((auto or question).strip())

    if st.session_state.get("error"):
        st.error("처리 중 오류가 났습니다 — " + st.session_state["error"])
    s = st.session_state.get("result")
    if s:
        st.markdown(f"**문의** — {st.session_state.get('question', '')}")
        show_answer(s)
        c1, c2, c3 = st.columns(3)
        with c1, st.container(border=True):
            show_tools(s)
        with c2, st.container(border=True):
            show_clauses(s)
        with c3, st.container(border=True):
            show_verify(s)
        with st.expander("처리 과정(노드별 기록)"):
            st.json(s.get("trace", []))


main()
