"""평가 — 순서 4 단계: 분류·gate 만 평가용 20건에 돌려 분류 지표를 낸다.

도구 호출·답변 적절성·채점기 자기검증은 순서 6 에서 이 파일에 더한다(tech_spec 5절).
실행: `python evaluate.py --tag baseline`
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

import agent
import context

LABELS = ["ORDER_PAY", "PRODUCT", "SHIPPING", "CANCEL_REFUND", "OUT_OF_SCOPE"]
RESULTS_DIR = context.ROOT / "results"


def _one(item: dict) -> dict:
    t = time.time()
    try:
        s = agent.run(item["question"], until="gate")
        return {"id": item["id"], "category": s.get("category"), "secondary": s.get("secondary"),
                "confidence": s.get("confidence"), "reason": s.get("reason"), "action": s.get("action"),
                "seconds": round(time.time() - t, 1), "error": None}
    except Exception as e:  # 한 문항이 막혀도 나머지는 잰다
        return {"id": item["id"], "category": None, "secondary": None, "confidence": None, "reason": None,
                "action": None, "seconds": round(time.time() - t, 1), "error": f"{type(e).__name__}: {str(e)[:200]}"}


def run_classify(tag: str, workers: int = 6) -> dict:
    if not agent.has_key():
        print(agent.NO_KEY_MESSAGE)
        raise SystemExit(1)
    gold = context._load(context.GOLDEN_PATH)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        got = list(ex.map(_one, gold))
    total_seconds = round(time.time() - started, 1)

    rows = []
    for g, r in zip(gold, got):
        gate_expected = "ESCALATE" if g["category"] == "OUT_OF_SCOPE" else "CONTINUE"
        rows.append({
            **r, "question": g["question"], "expected": g["category"], "boundary": g.get("boundary"),
            "gate_expected": gate_expected,
            "category_ok": r["category"] == g["category"],
            "gate_ok": r["action"] == gate_expected,
            "secondary_ok": (r["secondary"] == g.get("boundary")),
        })
    ok = [r for r in rows if r["error"] is None]
    y_true = [r["expected"] for r in ok]
    y_pred = [r["category"] for r in ok]
    boundary = [r for r in ok if r["boundary"]]
    summary = {
        "n": len(rows), "errors": len(rows) - len(ok),
        "accuracy": round(accuracy_score(y_true, y_pred), 3),
        "macro_f1": round(f1_score(y_true, y_pred, labels=LABELS, average="macro", zero_division=0), 3),
        "confusion_matrix": {"labels": LABELS, "rows_true_cols_pred": confusion_matrix(y_true, y_pred, labels=LABELS).tolist()},
        "gate_correct": sum(r["gate_ok"] for r in ok),
        "escalated_at_gate": sum(r["action"] == "ESCALATE" for r in ok),
        "boundary_secondary_correct": f"{sum(r['secondary_ok'] for r in boundary)}/{len(boundary)}",
        "false_secondary": [r["id"] for r in ok if not r["boundary"] and r["secondary"]],
        "total_seconds": total_seconds,
    }
    record = {
        "measured_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kind": "classify", "tag": tag,
        "models": {role: agent.model_name(role) for role in agent.ROLE_ENV},
        "gate_threshold": agent.GATE_THRESHOLD, "summary": summary, "items": rows,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"classify_{datetime.now().strftime('%Y%m%d-%H%M')}_{tag}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    report(record, path)
    return record


def report(record: dict, path) -> None:
    s = record["summary"]
    print(f"측정일 {record['measured_at']} · 분류 모델 {record['models']['router']} · 임계값 {record['gate_threshold']} · 태그 {record['tag']}")
    print(f"문항 {s['n']} (오류 {s['errors']}) · 정확도 {s['accuracy']} · macro F1 {s['macro_f1']} · {s['total_seconds']}초")
    print("혼동 행렬 (행=정답, 열=예측)")
    short = {"ORDER_PAY": "주문", "PRODUCT": "상품", "SHIPPING": "배송", "CANCEL_REFUND": "취소", "OUT_OF_SCOPE": "범위밖"}
    print("          " + "".join(f"{short[l]:>6}" for l in LABELS))
    for lab, row in zip(LABELS, s["confusion_matrix"]["rows_true_cols_pred"]):
        print(f"  {short[lab]:6}  " + "".join(f"{n:>6}" for n in row))
    print(f"gate 정답 {s['gate_correct']}/{s['n']} (범위 밖만 넘겨야 함) · gate 가 넘긴 건수 {s['escalated_at_gate']} · "
          f"경계 문항 secondary {s['boundary_secondary_correct']} · 경계 아닌데 secondary 가 붙은 문항 {s['false_secondary'] or '없음'}")
    wrong = [r for r in record["items"] if r["error"] or not r["category_ok"] or not r["gate_ok"]]
    print(f"틀린 문항 {len(wrong)}건")
    for r in wrong:
        print(f"  {r['id']} 정답 {r['expected']} → 예측 {r['category']} / {r['secondary']} / 확신도 {r['confidence']} / 동작 {r['action']}"
              f"{' / 오류 ' + r['error'] if r['error'] else ''}\n     「{r['question']}」 이유: {r['reason']}")
    print("저장:", path)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    run_classify(a.tag, a.workers)
