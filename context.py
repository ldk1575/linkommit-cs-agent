"""근거 문서 읽기 — 조항 파싱 · 읽기 도구 · 컨텍스트 조립 · 분리 검사.

모델은 이 파일을 부르지 않는다. `agent.py` 의 assemble 노드가 코드로 부른다(tech_spec 2·4절).
`python context.py` 로 P2 · P3 검사를 돌린다.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent
POLICY_DIR = ROOT / "docs" / "policy"
MAPPING_PATH = ROOT / "data" / "mapping.json"

CLAUSE_RE = re.compile(r"^\*\*\[(CS-([a-z]+)-(\d+))\]\*\* (.*)$")
CLAUSE_ID_RE = re.compile(r"\[(CS-[a-z]+-\d+)\]")
TAG_RE = re.compile(r"\s*\[(확정: [^\]]+|비움)\]$")


@dataclass(frozen=True)
class Clause:
    id: str       # CS-cancel-2
    slug: str     # cancel
    text: str     # 태그를 뗀 문장
    tag: str      # "확정" | "비움"
    source: str   # [확정: …] 의 출처, 비움이면 ""


def load_mapping() -> dict:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def _body(path: Path) -> str:
    """front-matter 와 안내용 인용 줄(`> …`)을 뺀 본문 — 제목 · 조항 · 소제목만 남는다."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    return "\n".join(l for l in text.splitlines() if not l.startswith(">")).strip()


def parse_clauses(body: str) -> list[Clause]:
    clauses = []
    for line in body.splitlines():
        m = CLAUSE_RE.match(line)
        if not m:
            continue
        cid, slug, _, rest = m.groups()
        tag_m = TAG_RE.search(rest)
        if not tag_m:
            raise ValueError(f"{cid}: 문장 끝에 [확정: …] 또는 [비움] 태그가 없다")
        tag_raw = tag_m.group(1)
        clauses.append(Clause(
            id=cid, slug=slug, text=TAG_RE.sub("", rest),
            tag="비움" if tag_raw == "비움" else "확정",
            source="" if tag_raw == "비움" else tag_raw.removeprefix("확정: "),
        ))
    return clauses


def load_chapters() -> dict[str, dict]:
    """{slug: {"file", "body", "clauses"}} — 매핑표가 가리키는 장 파일 전부."""
    chapters = {}
    for slug, fname in load_mapping()["chapters"].items():
        body = _body(POLICY_DIR / fname)
        chapters[slug] = {"file": fname, "body": body, "clauses": parse_clauses(body)}
    return chapters


def render_clauses(clauses: list[Clause]) -> str:
    """프롬프트에 넣는 모양 — `[CS-…] 문장`, 비운 칸만 끝에 `[비움]` 을 남겨 모델이 알아보게 한다."""
    return "\n".join(
        f"[{c.id}] {c.text}" + (" [비움]" if c.tag == "비움" else "") for c in clauses
    )


# 읽기 도구 4개 — 각각 한 장의 조항 본문만 돌려준다(front-matter 제외). 모델이 고르지 않고 코드가 부른다.
def _chapter_tool(slug: str):
    def tool() -> str:
        return render_clauses(load_chapters()[slug]["clauses"])
    return tool


get_order_policy = _chapter_tool("order")
get_product_policy = _chapter_tool("product")
get_shipping_policy = _chapter_tool("shipping")
get_cancel_policy = _chapter_tool("cancel")

TOOLS = {
    "get_order_policy": get_order_policy,
    "get_product_policy": get_product_policy,
    "get_shipping_policy": get_shipping_policy,
    "get_cancel_policy": get_cancel_policy,
}


def assemble_context(category: str, secondary: str | None = None) -> dict:
    """항상 넣는 0·9장 + category 장 + secondary 장(있고 다를 때)을 조립한다.

    돌려주는 값: context(프롬프트용 글) · clause_ids(그 안의 조항 번호 집합) ·
    called_tools(실제로 부른 읽기 도구 이름 — 기대 도구와 비교하는 값, 0·9장 읽기는 세지 않는다).
    """
    mapping = load_mapping()
    cats = mapping["categories"]
    for c in (category, secondary):
        if c is not None and c not in cats:
            raise ValueError(f"알 수 없는 카테고리: {c}")

    chapters = load_chapters()
    sections: list[str] = []
    ids: set[str] = set()
    called: list[str] = []

    def add(title: str, rendered: str):
        sections.append(f"## {title}\n{rendered}")
        ids.update(CLAUSE_ID_RE.findall(rendered))

    for slug in mapping["always"]:
        add(f"항상 읽는 장: {chapters[slug]['file']}", render_clauses(chapters[slug]["clauses"]))

    wanted = [category] + ([secondary] if secondary and secondary != category else [])
    for cat in wanted:
        tool_name = cats[cat]["tool"]
        if tool_name is None:        # 범위 밖 — 읽을 장이 이미 항상 읽는 9장이다
            continue
        add(f"{cats[cat]['label']}: {chapters[cats[cat]['chapter']]['file']}", TOOLS[tool_name]())
        called.append(tool_name)

    return {"context": "\n\n".join(sections), "clause_ids": ids, "called_tools": called}


def check_mapping() -> bool:
    """P2 — 카테고리 5개 모두 연결된 장 파일이 있고, 도구는 장과 일대일이다."""
    mapping, ok = load_mapping(), True
    print("[P2] 카테고리 ↔ 장 연결")
    for cat, v in mapping["categories"].items():
        fname = mapping["chapters"].get(v["chapter"])
        exists = fname is not None and (POLICY_DIR / fname).exists()
        tool_ok = v["tool"] is None or v["tool"] in TOOLS
        ok &= exists and tool_ok
        print(f"  {cat:14} → {fname or '없음':20} 도구 {v['tool'] or '(없음 → 이관)':20} {'OK' if exists and tool_ok else '실패'}")
    return ok and len(mapping["categories"]) == 5


def check_separation() -> bool:
    """P3 — ① 0·9장 본문에 숫자·카테고리 전용 용어가 없다 ② 카테고리마다 자기 장만 들어간다."""
    mapping, chapters, ok = load_mapping(), load_chapters(), True
    print("[P3] 컨텍스트 분리")
    for slug in mapping["always"]:
        # 제목 줄(`# 0. 공통`)의 장 번호와 조항 번호·태그는 본문이 아니다
        text = "\n".join(
            TAG_RE.sub("", CLAUSE_RE.sub(lambda m: m.group(4), l))
            for l in chapters[slug]["body"].splitlines() if not l.startswith("#")
        )
        digits = re.findall(r"\d+", text)
        terms = [t for t in mapping["exclusive_terms"] if t in text]
        bad = bool(digits or terms)
        ok &= not bad
        print(f"  항상 읽는 {chapters[slug]['file']:20} 숫자 {digits or '없음'} · 전용 용어 {terms or '없음'}")
    own = {cat: v["chapter"] for cat, v in mapping["categories"].items()}
    for cat, slug in own.items():
        got = assemble_context(cat)
        slugs = {i.split("-")[1] for i in got["clause_ids"]}
        expect = set(mapping["always"]) | {slug}
        mixed = slugs - expect
        ok &= not mixed and slugs >= expect
        print(f"  {cat:14} 읽은 장 {sorted(slugs)} 도구 {got['called_tools']} {'섞임 없음' if not mixed else f'섞임! {sorted(mixed)}'}")
    sec = assemble_context("SHIPPING", "CANCEL_REFUND")
    sec_slugs = {i.split("-")[1] for i in sec["clause_ids"]}
    ok &= sec_slugs == {"common", "scope", "shipping", "cancel"} and len(sec["called_tools"]) == 2
    print(f"  SHIPPING+CANCEL_REFUND 읽은 장 {sorted(sec_slugs)} 도구 {sec['called_tools']}")
    return ok


GOLDEN_PATH = ROOT / "data" / "goldenset.json"
FEWSHOT_PATH = ROOT / "data" / "fewshot.json"


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_goldenset() -> bool:
    """P4 — 평가용 20건의 분포 · 넘기기 8건 · 도구 규칙 · 모범 답안의 인용이 조회 범위 안에 있는지."""
    mapping, chapters, gold = load_mapping(), load_chapters(), _load(GOLDEN_PATH)
    cats, always = mapping["categories"], mapping["always"]
    tool_slug = {v["tool"]: v["chapter"] for v in cats.values() if v["tool"]}
    clause_tag = {c.id: c.tag for ch in chapters.values() for c in ch["clauses"]}
    problems: list[str] = []

    ids = [g["id"] for g in gold]
    if len(set(ids)) != len(ids):
        problems.append("id 중복")
    per_cat = {c: sum(g["category"] == c for g in gold) for c in cats}
    n_escalate = sum(g["action"] == "ESCALATE" for g in gold)
    if len(gold) != 20:
        problems.append(f"건수 {len(gold)} (20 이어야 함)")
    if any(n != 4 for n in per_cat.values()):
        problems.append(f"카테고리별 건수 {per_cat} (각 4 이어야 함)")
    if n_escalate != 8:
        problems.append(f"넘기기 정답 {n_escalate}건 (8 이어야 함)")

    for g in gold:
        gid, tools = g["id"], g["tools"]
        if g["category"] not in cats or g["action"] not in ("ANSWER", "ESCALATE"):
            problems.append(f"{gid}: category/action 값")
        if any(t not in tool_slug for t in tools) or len(set(tools)) != len(tools):
            problems.append(f"{gid}: tools 값 {tools}")
        cat_tool = cats[g["category"]]["tool"]
        if g["category"] == "OUT_OF_SCOPE":
            if tools or g["action"] != "ESCALATE":
                problems.append(f"{gid}: 범위 밖은 tools 비고 ESCALATE 이어야 함")
        elif cat_tool not in tools:
            problems.append(f"{gid}: 자기 카테고리 도구 {cat_tool} 가 tools 에 없음")
        if len(tools) == 2 and not g.get("boundary"):
            problems.append(f"{gid}: tools 가 둘인데 boundary 가 없음")
        if g.get("boundary") and (len(tools) != 2 or cats[g["boundary"]]["tool"] not in tools):
            problems.append(f"{gid}: boundary {g['boundary']} 의 도구가 tools 에 없음")
        if not g["forbid"] or not g["question"].strip() or not g["reference"].strip():
            problems.append(f"{gid}: forbid/question/reference 비어 있음")
        if g["action"] == "ANSWER" and not g["must"]:
            problems.append(f"{gid}: ANSWER 인데 must 가 비어 있음")
        if g["action"] == "ESCALATE" and g["must"]:
            problems.append(f"{gid}: ESCALATE 인데 must 가 있음")
        # 모범 답안의 인용 — ANSWER 는 1개 이상, 조회한 장 안에 있는 [확정] 조항이어야 한다
        cited = CLAUSE_ID_RE.findall(g["reference"])
        allowed = set(always) | {tool_slug[t] for t in tools if t in tool_slug}
        if g["action"] == "ANSWER" and not cited:
            problems.append(f"{gid}: 모범 답안에 인용이 없음")
        if g["action"] == "ESCALATE" and cited:
            problems.append(f"{gid}: 넘기기 모범 답안에 인용이 있음")
        for cid in cited:
            if cid.split("-")[1] not in allowed:
                problems.append(f"{gid}: 인용 {cid} 가 조회한 장 밖")
            if clause_tag.get(cid) != "확정":
                problems.append(f"{gid}: 인용 {cid} 가 [확정] 조항이 아님")

    print("[P4] 평가용 goldenset.json")
    print(f"  건수 {len(gold)} · 카테고리별 {per_cat} · 넘기기 정답 {n_escalate}건({n_escalate * 100 // max(len(gold), 1)}%) · 경계 문항 {[g['id'] for g in gold if g.get('boundary')]}")
    for p in problems:
        print("  ✗", p)
    return not problems


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s)


def check_split() -> bool:
    """P4 — 평가용과 예시용의 id · 질문 문장 중복 0, 거의 같은 문장(유사도 0.7 이상)도 0."""
    from difflib import SequenceMatcher

    gold, shots = _load(GOLDEN_PATH), _load(FEWSHOT_PATH)
    same_id = {g["id"] for g in gold} & {s["id"] for s in shots}
    same_q = {_norm(g["question"]) for g in gold} & {_norm(s["question"]) for s in shots}
    close = [
        (g["id"], s["id"], round(r, 2))
        for g in gold for s in shots
        if (r := SequenceMatcher(None, _norm(g["question"]), _norm(s["question"])).ratio()) >= 0.7
    ]
    bad_shot = [s["id"] for s in shots if s["category"] not in load_mapping()["categories"]
                or (s["secondary"] is not None and s["secondary"] not in load_mapping()["categories"])
                or not 0 <= s["confidence"] <= 1]
    print("[P4] 예시용 fewshot.json 과 누수 검사")
    print(f"  예시용 {len(shots)}건 · 카테고리 {sorted({s['category'] for s in shots})}")
    print(f"  id 중복 {len(same_id)} · 질문 문장 중복 {len(same_q)} · 거의 같은 문장 {close or 0} · 값 오류 {bad_shot or 0}")
    return len(shots) >= 6 and not (same_id or same_q or close or bad_shot)


if __name__ == "__main__":
    results = [check_mapping(), check_separation(), check_goldenset(), check_split()]
    print("전체:", "통과" if all(results) else "실패")
    raise SystemExit(0 if all(results) else 1)
