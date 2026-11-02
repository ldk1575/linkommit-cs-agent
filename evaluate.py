"""평가 — 분류 측정(순서 4) · 전체 측정과 채점기 자기검증(순서 6).

실행
  python evaluate.py check-grader            채점기가 모범 답안은 통과시키고 오답은 떨어뜨리는지(P5)
  python evaluate.py run --tag baseline      평가용 20건 전체 측정 — 도구 호출·답변 적절성·분류 지표(P6·P7·P9)
  python evaluate.py classify --tag x        분류·gate 만(답변 호출 없음)
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache

from pydantic import BaseModel
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

import agent
import context
import prompts

LABELS = ["ORDER_PAY", "PRODUCT", "SHIPPING", "CANCEL_REFUND", "OUT_OF_SCOPE"]
SHORT = {"ORDER_PAY": "주문", "PRODUCT": "상품", "SHIPPING": "배송", "CANCEL_REFUND": "취소", "OUT_OF_SCOPE": "범위밖"}
RESULTS_DIR = context.ROOT / "results"
BLANK_ITEMS = {"O4", "P4", "S4", "C4"}   # 정답이 `[비움]` 때문에 넘기기인 문항(범위 밖 X1~X4 와 구분)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


def _meta(tag: str, kind: str) -> dict:
    return {"measured_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "kind": kind, "tag": tag,
            "models": {role: agent.model_name(role) for role in agent.ROLE_ENV},  # 모델 이름만 — 환경변수 값(키)은 쓰지 않는다
            "gate_threshold": agent.GATE_THRESHOLD}


# --- 채점기 -----------------------------------------------------------------------------------------
class JudgeOut(BaseModel):
    must_results: list[bool]
    forbid_hits: list[bool]
    reason: str


@lru_cache(maxsize=1)
def _judge_llm():
    return agent.make_model("judge").with_structured_output(JudgeOut)


def _judge(item: dict, answer: str) -> JudgeOut:
    for attempt in (1, 2):   # 목록 길이가 어긋나면 한 번 더 묻는다
        out: JudgeOut = _judge_llm().invoke(prompts.build_judge_messages(item, answer))
        if len(out.must_results) == len(item["must"]) and len(out.forbid_hits) == len(item["forbid"]):
            return out
    raise ValueError(f"채점 목록 길이 불일치 (must {len(out.must_results)}/{len(item['must'])}, forbid {len(out.forbid_hits)}/{len(item['forbid'])})")


def grade(item: dict, out: dict) -> dict:
    """답변 적절성 — ANSWER 정답: 답했고 must 전부 + forbid 없음. ESCALATE 정답: 넘겼고 forbid 없음.

    `out` 은 {"action", "answer"}. 판정은 `CS_JUDGE_MODEL` 이 하고, 모델 없이 정할 수 있는 건(동작이 다름) 모델을 부르지 않는다.
    """
    want, got = item["action"], out.get("action")
    if got != want:
        return {"pass": False, "reason": f"동작이 다르다 (정답 {want}, 실제 {got})", "judge_called": False}
    # 넘기기 정답이고 표준 이관 문구면 말한 내용이 없어 forbid 를 어길 수 없다
    if want == "ESCALATE" and out.get("answer", "").strip() == agent.ESCALATE_MESSAGE:
        return {"pass": True, "reason": "표준 이관 문구", "judge_called": False}
    j = _judge(item, out.get("answer", ""))
    must_ok, hit = all(j.must_results), any(j.forbid_hits)
    return {"pass": must_ok and not hit, "reason": j.reason, "judge_called": True,
            "must_results": j.must_results, "forbid_hits": j.forbid_hits}


def check_grader() -> bool:
    """P5 — ① 모범 답안(`reference`) 20건 전부 통과 ② `forbid` 를 일부러 어긴 오답 3건 전부 불통과.

    덧붙인 대조군 두 개 — ③ 사실이 빠진 답은 불통과 ④ 표현만 바꾼 정답은 통과(표현이 아니라 사실을 보는지).
    """
    if not agent.has_key():
        print(agent.NO_KEY_MESSAGE)
        return False
    gold = context._load(context.GOLDEN_PATH)
    by_id = {g["id"]: g for g in gold}

    def ref(g):
        return grade(g, {"action": g["action"], "answer": g["reference"]})

    wrong_ids = ["O1", "C2", "O4"]   # ANSWER 두 건(사실·숫자)과 ESCALATE 한 건(지어낸 답)
    wrong = {i: {"action": by_id[i]["action"], "answer": by_id[i]["reference"] + " " + by_id[i]["forbid"][0] + "."} for i in wrong_ids}
    paraphrase = "옵션이 있는 상품은 고객님이 옵션을 직접 선택하셔야 하고, 선택하기 전에는 구매가 진행되지 않으며 옵션을 고르라는 안내가 나옵니다."
    vague = "자세한 내용은 상품 상세 화면에서 확인해 주세요."
    jobs = ([("모범", g["id"], lambda g=g: ref(g)) for g in gold]
            + [("오답", i, lambda i=i: grade(by_id[i], wrong[i])) for i in wrong_ids]
            + [("사실 누락", "O1", lambda: grade(by_id["O1"], {"action": "ANSWER", "answer": vague})),
               ("표현만 바꿈", "O1", lambda: grade(by_id["O1"], {"action": "ANSWER", "answer": paraphrase}))])
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda j: j[2](), jobs))
    rows = [{"kind": k, "id": i, **r} for (k, i, _), r in zip(jobs, results)]

    refs = [r for r in rows if r["kind"] == "모범"]
    bad = [r for r in rows if r["kind"] == "오답"]
    ctrl_miss = next(r for r in rows if r["kind"] == "사실 누락")
    ctrl_para = next(r for r in rows if r["kind"] == "표현만 바꿈")
    ok = (all(r["pass"] for r in refs) and not any(r["pass"] for r in bad)
          and not ctrl_miss["pass"] and ctrl_para["pass"])
    print("[P5] 채점기 자기검증")
    print(f"  모범 답안 통과 {sum(r['pass'] for r in refs)}/{len(refs)} (목표 20/20) · 오답 불통과 {sum(not r['pass'] for r in bad)}/{len(bad)} (목표 3/3)")
    print(f"  대조군 — 사실 누락 불통과 {not ctrl_miss['pass']} · 표현만 바꾼 정답 통과 {ctrl_para['pass']}")
    for r in rows:
        should = r["kind"] in ("모범", "표현만 바꿈")
        if r["pass"] != should:
            print(f"  ✗ {r['kind']} {r['id']}: 통과={r['pass']} — {r['reason']}")
    record = {**_meta("grader-check", "grader"), "ok": ok, "items": rows}
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"grader_{_stamp()}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  저장:", path, "→", "통과" if ok else "실패")
    return ok


# --- 지표 -------------------------------------------------------------------------------------------
def classification_metrics(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["error"] is None and r["category"]]
    y_true, y_pred = [r["expected"] for r in ok], [r["category"] for r in ok]
    boundary = [r for r in ok if r["boundary"]]
    return {
        "accuracy": round(accuracy_score(y_true, y_pred), 3),
        "macro_f1": round(f1_score(y_true, y_pred, labels=LABELS, average="macro", zero_division=0), 3),
        "confusion_matrix": {"labels": LABELS, "rows_true_cols_pred": confusion_matrix(y_true, y_pred, labels=LABELS).tolist()},
        "boundary_secondary_correct": f"{sum(r['secondary'] == r['boundary'] for r in boundary)}/{len(boundary)}",
        "false_secondary": [r["id"] for r in ok if not r["boundary"] and r["secondary"]],
    }


def print_confusion(cm: dict) -> None:
    print("혼동 행렬 (행=정답, 열=예측)")
    print("          " + "".join(f"{SHORT[l]:>6}" for l in LABELS))
    for lab, row in zip(LABELS, cm["rows_true_cols_pred"]):
        print(f"  {SHORT[lab]:6}  " + "".join(f"{n:>6}" for n in row))


def _save(record: dict, prefix: str, tag: str):
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{prefix}_{_stamp()}_{tag}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --- 분류만(순서 4) ---------------------------------------------------------------------------------
def _classify_one(item: dict) -> dict:
    t = time.time()
    try:
        s = agent.run(item["question"], until="gate")
        return {"category": s.get("category"), "secondary": s.get("secondary"), "confidence": s.get("confidence"),
                "reason": s.get("reason"), "action": s.get("action"), "seconds": round(time.time() - t, 1), "error": None}
    except Exception as e:  # 한 문항이 막혀도 나머지는 잰다
        return {"category": None, "secondary": None, "confidence": None, "reason": None, "action": None,
                "seconds": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:200]}"}


def run_classify(tag: str, workers: int = 6) -> dict:
    if not agent.has_key():
        print(agent.NO_KEY_MESSAGE)
        raise SystemExit(1)
    gold = context._load(context.GOLDEN_PATH)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        got = list(ex.map(_classify_one, gold))
    rows = []
    for g, r in zip(gold, got):
        gate_expected = "ESCALATE" if g["category"] == "OUT_OF_SCOPE" else "CONTINUE"
        rows.append({"id": g["id"], **r, "question": g["question"], "expected": g["category"], "boundary": g.get("boundary"),
                     "gate_expected": gate_expected, "category_ok": r["category"] == g["category"],
                     "gate_ok": r["action"] == gate_expected})
    ok = [r for r in rows if r["error"] is None]
    summary = {"n": len(rows), "errors": len(rows) - len(ok), **classification_metrics(rows),
               "gate_correct": sum(r["gate_ok"] for r in ok), "escalated_at_gate": sum(r["action"] == "ESCALATE" for r in ok),
               "total_seconds": round(time.time() - started, 1)}
    record = {**_meta(tag, "classify"), "summary": summary, "items": rows}
    path = _save(record, "classify", tag)
    s = summary
    print(f"측정일 {record['measured_at']} · 분류 모델 {record['models']['router']} · 임계값 {record['gate_threshold']} · 태그 {tag}")
    print(f"문항 {s['n']} (오류 {s['errors']}) · 정확도 {s['accuracy']} · macro F1 {s['macro_f1']} · {s['total_seconds']}초")
    print_confusion(s["confusion_matrix"])
    print(f"gate 정답 {s['gate_correct']}/{s['n']} · gate 가 넘긴 건수 {s['escalated_at_gate']} · 경계 문항 secondary {s['boundary_secondary_correct']} · 경계 아닌데 붙은 문항 {s['false_secondary'] or '없음'}")
    for r in (r for r in rows if r["error"] or not r["category_ok"] or not r["gate_ok"]):
        print(f"  ✗ {r['id']} 정답 {r['expected']} → {r['category']} / {r['secondary']} / {r['confidence']} / {r['action']} {r['error'] or ''}")
    print("저장:", path)
    return record


# --- 전체 측정(순서 6) ------------------------------------------------------------------------------
def _full_one(item: dict) -> dict:
    t = time.time()
    base = {"id": item["id"], "question": item["question"], "expected": item["category"], "boundary": item.get("boundary"),
            "expected_action": item["action"], "expected_tools": item["tools"]}
    try:
        s = agent.run(item["question"])
        called = s.get("called_tools") or []
        g = grade(item, {"action": s.get("action"), "answer": s.get("answer", "")})
        final_check = (agent.check_answer(s["answer"], s["action"], item["question"], s["context"], set(s["clause_ids"]))
                       if s.get("context") else [])
        escalated_by = next((tr["node"] for tr in s["trace"] if tr["node"] == "escalate"), None) or ("answer" if s.get("action") == "ESCALATE" else None)
        return {**base, "category": s.get("category"), "secondary": s.get("secondary"), "confidence": s.get("confidence"),
                "action": s.get("action"), "called_tools": called, "tools_ok": set(called) == set(item["tools"]),
                "answer": s.get("answer"), "citations": s.get("citations", []), "retries": s.get("retries", 0),
                "verify_violations": [tr["violations"] for tr in s["trace"] if tr["node"] == "verify"],
                "escalated_by": escalated_by, "final_recheck": final_check, "grade": g, "answer_ok": g["pass"],
                "seconds": round(time.time() - t, 1), "error": None}
    except Exception as e:
        return {**base, "category": None, "secondary": None, "confidence": None, "action": None, "called_tools": [],
                "tools_ok": False, "answer": None, "citations": [], "retries": 0, "final_recheck": [], "grade": None,
                "answer_ok": False, "seconds": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:200]}"}


def run_eval(tag: str, workers: int = 6) -> dict:
    if not agent.has_key():
        print(agent.NO_KEY_MESSAGE)
        raise SystemExit(1)
    gold = context._load(context.GOLDEN_PATH)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(_full_one, gold))
    n = len(rows)
    esc_expected = [r for r in rows if r["expected_action"] == "ESCALATE"]
    answered = [r for r in rows if r["expected_action"] == "ANSWER"]
    blank = [r for r in rows if r["id"] in BLANK_ITEMS]
    ans_rows = [r for r in rows if r["action"] == "ANSWER"]
    summary = {
        "n": n, "errors": sum(r["error"] is not None for r in rows),
        "tool_call_score": f"{sum(r['tools_ok'] for r in rows)}/{n}",
        "answer_score": f"{sum(r['answer_ok'] for r in rows)}/{n}",
        "answer_score_answer_items": f"{sum(r['answer_ok'] for r in answered)}/{len(answered)}",
        "escalate_expected": len(esc_expected),
        "escalated_correctly": sum(r["action"] == "ESCALATE" for r in esc_expected),
        "blank_items_filled": [r["id"] for r in blank if r["action"] == "ANSWER"],   # `[비움]` 을 채운(지어낸) 답
        "answer_without_citation": [r["id"] for r in ans_rows if not r["citations"]],
        "citation_outside_loaded": [r["id"] for r in ans_rows if any("②" in v for v in r["final_recheck"])],
        "forbidden_term_in_answer": [r["id"] for r in rows if any("④" in v for v in r["final_recheck"])],
        "retried": [r["id"] for r in rows if r["retries"]],
        "total_seconds": round(time.time() - started, 1),
        **classification_metrics(rows),
    }
    record = {**_meta(tag, "full"), "summary": summary, "items": rows}
    path = _save(record, "run", tag)
    report(record, path)
    return record


def report(record: dict, path) -> None:
    s = record["summary"]
    print(f"측정일 {record['measured_at']} · 분류 {record['models']['router']} · 답변 {record['models']['answer']} · 채점 {record['models']['judge']} · 임계값 {record['gate_threshold']} · 태그 {record['tag']}")
    print(f"문항 {s['n']} (오류 {s['errors']}) · {s['total_seconds']}초")
    print(f"도구 호출 적절성 {s['tool_call_score']} · 답변 적절성 {s['answer_score']} (답하는 문항만 {s['answer_score_answer_items']})")
    print(f"넘기기 정답 {s['escalate_expected']}건 중 넘긴 수 {s['escalated_correctly']} · [비움]을 채운 답 {s['blank_items_filled'] or 0}건")
    print(f"인용 없는 답 {s['answer_without_citation'] or 0} · 조회한 장 밖 인용 {s['citation_outside_loaded'] or 0} · 금지 표현 {s['forbidden_term_in_answer'] or 0} · 재생성한 문항 {s['retried'] or '없음'}")
    print(f"분류 정확도 {s['accuracy']} · macro F1 {s['macro_f1']} · 경계 secondary {s['boundary_secondary_correct']} · 경계 아닌데 붙은 문항 {s['false_secondary'] or '없음'}")
    print_confusion(s["confusion_matrix"])
    bad = [r for r in record["items"] if r["error"] or not r["tools_ok"] or not r["answer_ok"]]
    print(f"틀린 문항 {len(bad)}건")
    for r in bad:
        why = r["error"] or (r["grade"] or {}).get("reason", "")
        tools = "OK" if r["tools_ok"] else f"{r['called_tools']} (기대 {r['expected_tools']})"
        print(f"  ✗ {r['id']} 도구 {tools} · 답변 {'OK' if r['answer_ok'] else 'NG'} · 동작 {r['action']} (기대 {r['expected_action']})\n     「{r['question']}」\n     → {r['answer']}\n     이유: {why}")
    print("저장:", path)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-grader")
    for name in ("run", "classify"):
        p = sub.add_parser(name)
        p.add_argument("--tag", default="baseline")
        p.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    if a.cmd == "check-grader":
        raise SystemExit(0 if check_grader() else 1)
    (run_eval if a.cmd == "run" else run_classify)(a.tag, a.workers)
