#!/usr/bin/env python3
"""실행 기록 세 개를 읽어 평가 보고를 갈래별로 나눠 쓴다. API 는 부르지 않는다."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).parent))
from labels import EXTRACTION_FIELDS  # noqa: E402
from report import ARM_NAMES  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE.parent / "docs" / "eval-report.md"

# 정답과 다르게 낸 판정의 갈래다. 위에서부터 더 위험하다.
ERROR_TYPES = {
    ("undetermined", "feasible"): "보류인데 가능 — 확인하지 못한 일정을 괜찮다고 했다",
    ("infeasible", "feasible"): "불가능인데 가능 — 안 되는 일정을 괜찮다고 했다",
    ("feasible", "infeasible"): "가능인데 불가능",
    ("undetermined", "infeasible"): "보류인데 불가능",
    ("infeasible", "undetermined"): "불가능인데 보류",
    ("feasible", "undetermined"): "가능인데 보류",
}
NO_ANSWER = "답을 받지 못함 — 거부 · 형식 오류"

# 확인하지 못한 이유를 누가 풀 수 있는지 나눈다.
SOLVED_BY = {
    "사용자": {"PLACE_NOT_RESOLVED", "NO_USER_DWELL", "LATE_BY_LOWER_BOUND", "LATE_BY_ESTIMATE", "INFERRED_INPUT",
              "BUFFER_NOT_MET", "NO_BUFFER_RULE", "MISSING_DATE", "INVALID_DATE", "MISSING_STOPS", "MISSING_PLACE",
              "MISSING_START_TIME", "INVALID_TIME", "INCOMPLETE_HARD_CONSTRAINT", "MISMATCHED_WEEKDAY",
              "AT_LAST_ADMISSION", "DWELL_BEYOND_TOUR"},
    "데이터": {"NO_CLOSED_DAY_DATA", "NO_OPERATING_HOURS_DATA", "EVIDENCE_EXPIRED", "UNSUPPORTED_PLACE",
              "HOLIDAY_CALENDAR_UNVERIFIED", "UNVERIFIED_TRAVEL_TIME", "LOWER_BOUND_ONLY",
              "UNVERIFIED_ALWAYS_OPEN", "RULE_CONFLICT"},
    "코드": {"MIDDAY_HARD_CONSTRAINT", "MULTIPLE_HARD_CONSTRAINTS"},
}
SOLVER = {reason: who for who, reasons in SOLVED_BY.items() for reason in reasons}


def error_type(expect: str, got: str | None) -> str | None:
    """맞으면 None 이다. refused 나 invalid 는 답을 받지 못한 것으로 센다."""
    if got == expect:
        return None
    return ERROR_TYPES.get((expect, got), NO_ANSWER)


def unknowns(result: dict) -> list[dict]:
    return ([c for c in result["checks"] if c["status"] == "unknown"]
            + [h["policy"] for h in result["hard_constraints"] if h["policy"]["status"] == "unknown"])


def no_question(check: dict) -> bool:
    """사용자가 풀어야 하는데 물을 말이 없는 검사를 찾는다."""
    return SOLVER.get(check.get("unknown_reason")) == "사용자" and "user" not in (check.get("how_to_resolve") or {})


def attribution(row: dict) -> str:
    """끝까지 돌린 한 건이 틀렸으면 어느 단계 탓인지 가린다."""
    if row.get("error"):
        return "구조화 추출 실패 — 거부 · 형식 오류"
    if row.get("verdict") is None:
        return "판정 못 함 — 날짜를 추출하지 못했다"
    if row.get("end_to_end_ok"):
        return "맞음"
    if row.get("extraction_ok"):
        return "판정 탓 — 구조화 추출은 맞았다"
    return "구조화 추출 탓 — 판정도 틀렸다" if not row.get("verdict_ok") else "구조화 추출만 틀림 — 판정은 맞았다"


CONDITION_NAMES = {"code_commit": "코드 커밋", "fingerprints": "입력 지문"}


def mismatched_conditions(records: dict[str, dict]) -> list[str]:
    """조건이 다른 숫자를 한 보고서에 섞지 않게 세 기록의 커밋과 입력 지문을 맞춰 본다."""
    problems = []
    for key in ("code_commit", "fingerprints"):
        values = {name: json.dumps(r["conditions"].get(key), sort_keys=True) for name, r in records.items()}
        if len(set(values.values())) > 1:
            problems.append(f"{CONDITION_NAMES[key]}이 기록마다 다르다: " + ", ".join(f"{n} {v[:20]}" for n, v in values.items()))
    return problems


def short(case_id: str) -> str:
    return case_id.split("-")[0]


def table(header: list[str], rows: list[list]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |",
            *("| " + " | ".join(str(x) for x in row) + " |" for row in rows)]


def build(run: dict, ext: dict, cmp: dict) -> str:
    c = run["conditions"]
    fp = c.get("fingerprints", {})
    md = ["# 평가 보고", "",
          "> `core/eval_report.py`가 실행 기록 세 개를 읽어 만든다. 손으로 고치지 않는다 — 기록을 다시 만들면 이 파일도 다시 만든다.", "",
          f"조건 — 코드 `{(c.get('code_commit') or '?')[:7]}` · 정답 문제 `{fp.get('tests.json')}` · "
          f"사실 `{fp.get('facts.json')}` · 우리 기준 `{fp.get('policy.json')}` · "
          f"기준 날짜 {cmp['conditions'].get('reference_date')}", "",
          f"기록 — 판정 `{run['run_id']}` · 구조화 추출 `{ext['run_id']}` · 비교 실험 `{cmp['run_id']}`", "",
          # 받아 둔 응답을 다시 쓸 때가 있어 어느 실행에서 받은 응답인지 적는다.
          "LLM 응답 — " + " / ".join(
              f"{name}: 새로 받음 {r['responses']['new']}건 · 받아 둔 응답 {r['responses']['reused']}건"
              f"(받은 실행 {' · '.join(f'`{x}`' for x in r['responses']['received_in'])})"
              for name, r in (("구조화 추출", ext), ("비교 실험", cmp))), ""]
    for p in mismatched_conditions({"판정": run, "구조화 추출": ext, "비교 실험": cmp}):
        md += [f"> ⚠ {p}", ""]

    # 1 · 2. 오류 유형별 결과와 잘못된 정상 판정
    rows = [r for r in cmp["rows"] if r.get("label_current", True)]
    arms = cmp["arms"]
    kinds = {arm: Counter() for arm in arms}
    cases = {arm: {} for arm in arms}
    for r in rows:
        for arm in arms:
            kind = error_type(r["expect"], r["arms"][arm]["verdict"])
            if kind:
                kinds[arm][kind] += 1
                cases[arm].setdefault(kind, []).append(short(r["id"]))
    md += ["## 1. 오류 유형별 결과", "",
           f"비교 실험 {len(rows)}건에서 네 비교 방식이 정답과 다르게 낸 답을 '정답인데 답' 짝으로 센다. "
           "위에서부터 더 위험하다.", ""]
    md += table(["오류 유형", *(ARM_NAMES.get(a, a) for a in arms)],
                [[k, *(kinds[a][k] for a in arms)] for k in [*ERROR_TYPES.values(), NO_ANSWER]]
                + [["맞음", *(len(rows) - sum(kinds[a].values()) for a in arms)]])
    md += ["", "어느 문제였나", ""]
    for arm in arms:
        for kind in [*ERROR_TYPES.values(), NO_ANSWER]:
            if kind in cases[arm]:
                md.append(f"- {ARM_NAMES.get(arm, arm)} — {kind.split(' — ')[0]}: " + " · ".join(cases[arm][kind]))
    false_pass = ERROR_TYPES[("infeasible", "feasible")]
    md += ["", "## 2. 불가능인데 가능 — 잘못된 정상 판정", "",
           "정답이 불가능인데 가능이라고 답한 건수 — "
           + " · ".join(f"{ARM_NAMES.get(a, a)} {kinds[a][false_pass]}" for a in arms) + ".", ""]

    # 3. 검사별 채점
    m = cmp["metrics"]
    md += ["## 3. 검사별 채점", "",
           "전체 판정이 맞아도 검사가 틀릴 수 있다. 정답의 기대 검사(`expect_checks`)마다 상태를 맞춰 보고, "
           "검사 상태와 전체 판정이 서로 맞물리는지(위반이 있으면 불가능, 없고 미확인이 있으면 보류) 본다. "
           "필수 일정은 판정 코드만 따로 낸다 — LLM 방식은 마지막 장소에서 가는 이동 검사로 적는다.", ""]
    md += table(["", *(ARM_NAMES.get(a, a) for a in arms)],
                [["검사별 정확도", *(m[a]["check_accuracy"] for a in arms)],
                 ["기대 검사를 답하지 않음 (개)", *(m[a]["checks_missing"] for a in arms)],
                 ["검사와 전체 판정이 어긋남 (건)", *(m[a]["verdict_inconsistent"] for a in arms)]])
    md += ["", "어느 문제였나 — 전체 판정은 맞았는데 검사가 틀린 문제와, 검사와 전체 판정이 어긋난 문제", ""]
    for arm in arms:
        right_but = [short(r["id"]) for r in rows if r["graded"][arm]["verdict_ok"] and r["graded"][arm]["checks_wrong"]]
        odd = [short(r["id"]) for r in rows if r["graded"][arm]["consistent"] is False]
        if right_but:
            md.append(f"- {ARM_NAMES.get(arm, arm)} — 판정은 맞고 검사가 틀림: " + " · ".join(right_but))
        if odd:
            md.append(f"- {ARM_NAMES.get(arm, arm)} — 검사와 전체 판정이 어긋남: " + " · ".join(odd))
    md.append("")

    # 4. 판정 코드의 보류 비율과 원인
    res = [r for r in run["results"] if r.get("label_current", True)]
    held = [r for r in res if r["summary"]["verdict"] == "undetermined"]
    by_reason, case_count = Counter(), Counter()
    who_per_case = Counter()
    silent = []
    for r in held:
        items = unknowns(r)
        for u in items:
            by_reason[u.get("unknown_reason")] += 1
            if no_question(u):
                silent.append(f"{short(r['id'])} {u.get('type', '필수 일정')} {u.get('unknown_reason')}")
        reasons = {u.get("unknown_reason") for u in items}
        for reason in reasons:
            case_count[reason] += 1
        who = sorted({SOLVER.get(x, "?") for x in reasons})
        who_per_case[" + ".join(who)] += 1
    md += ["## 4. 보류 비율과 원인 — 판정 코드", "",
           f"보류 {len(held)}건 / {len(res)}건 ({len(held) / max(len(res), 1):.3f}). 보류한 문제에서 확인하지 못한 검사의 이유다.", ""]
    md += table(["이유", "나온 문제", "검사 수", "누가 푸나"],
                [[f"`{k}`", case_count[k], v, SOLVER.get(k, "?")] for k, v in by_reason.most_common()])
    md += ["", "보류 한 건을 풀려면 누가 움직여야 하나 — "
           + " · ".join(f"{k} {v}건" for k, v in who_per_case.most_common()) + ".", ""]
    if silent:
        md += ["사용자에게 물어야 풀리는데 물을 말(`how_to_resolve.user`)이 없는 검사 — " + " · ".join(silent) + ".", ""]

    # 5. 추출
    ex_rows = ext["results"]
    graded = [r for r in ex_rows if "extraction_fields" in r]
    md += ["## 5. 구조화 추출과 끝까지 돌린 결과", "",
           f"구조화 추출 {sum(bool(r.get('extraction_ok')) for r in ex_rows)}/{len(ex_rows)}. 항목별 —", ""]
    md += table(["항목", "맞음"], [[label, f"{sum(r['extraction_fields'][k] for r in graded)}/{len(ex_rows)}"]
                                  for k, label in EXTRACTION_FIELDS.items()])
    blame = Counter(attribution(r) for r in ex_rows)
    md += ["", "끝까지 돌린 결과 — " + " · ".join(f"{k} {v}건" for k, v in blame.most_common()) + ".", ""]

    # 6. 비용과 지연
    lat = [r["usage"]["latency_ms"] for r in ex_rows if r.get("usage") and r["usage"]["latency_ms"] is not None]
    judge_ms = [r["elapsed_ms"] for r in res]
    usd = lambda v: "—" if v is None else f"${v}"  # 단가를 모르거나 사용량이 남지 않은 응답이다
    sec = lambda ms: "—" if ms is None else f"{ms / 1000:.1f}초"
    cost_rows = [[f"구조화 추출 ({ext['model']})", usd(ext["estimated_cost_per_case_usd"]), sec(median(lat))]]
    cost_rows += [[ARM_NAMES.get(arm, arm), usd(v["per_case_usd"]), sec(v["latency_median_ms"])]
                  for arm, v in cmp.get("cost", {}).items()]
    judge = median(judge_ms)  # run.py 가 0.01ms 단위로 반올림해 적는다
    cost_rows.append(["판정 코드", "$0", "1ms 미만" if judge < 1 else f"{judge:.0f}ms"])
    md += ["## 6. 비용과 지연", "",
           "건당 비용과 지연 중앙값이다. LLM 쪽은 응답을 받을 때 잰 값이다. 판정 코드는 외부를 부르지 않는다.", ""]
    md += table(["", "건당 비용", "지연 중앙값"], cost_rows)
    return "\n".join(md) + "\n"


def main():
    load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
    md = build(load("last-run.json"), load("last-extract-run.json"), load("last-compare.json"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(md)
    OUT.write_text(md, encoding="utf-8")
    print(f"기록: {OUT.relative_to(HERE.parent).as_posix()}")


if __name__ == "__main__":
    main()
