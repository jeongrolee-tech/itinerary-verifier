#!/usr/bin/env python3
"""
판정 코어 채점기 겸 실행 기록기

    python core/run.py                 요약표 + 틀린 케이스만 상세
    python core/run.py --all           전부 상세
    python core/run.py --case T05      한 케이스만 상세
    python core/run.py --json          결과 JSON

화면 완성도보다 중간 결과와 실패 원인을 확인할 수 있는 실행 기록이 먼저다.
최종 판정만 남기지 않고 어떤 값이 추출됐고, 어떤 기본값을 적용했으며,
어떤 근거와 규칙으로 판정했는지를 함께 남긴다.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from labels import stale_reason  # noqa: E402
from report import render, report  # noqa: E402
from runmeta import describe, run_meta  # noqa: E402
from verdict import UNKNOWN, judge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
VERSION = "core-0.2"
load = lambda name: json.loads((HERE / name).read_text(encoding="utf-8"))

facts, policy, suite = load("facts.json"), load("policy.json"), load("tests.json")

args = sys.argv[1:]
json_only = "--json" in args
show_all = "--all" in args
only = next((args[i + 1] for i, a in enumerate(args) if a == "--case" and i + 1 < len(args)), None)

# --case 를 주면 해당 접두사로 시작하는 케이스만 실행한다.
# 예전에는 상세 출력만 줄이고 전체 케이스를 계속 돌려서,
# 특정 케이스를 확인하려는 사용자가 전체 결과와 섞인 출력을 봐야 했다.
cases = [c for c in suite["cases"] if not only or c["id"].startswith(only)]
if only and not cases:
    raise SystemExit(f"케이스를 찾지 못했다: {only}")

MARK = {"pass": "○", "fail": "✕", "unknown": "?", "not_applicable": "–"}
LINE = "═" * 78

FACTS_SNAPSHOT = facts["snapshot_id"]
LABEL_SNAPSHOT = suite.get("labeled_against_snapshot")


def label_is_current(case: dict) -> bool:
    """라벨이 지금 데이터에서도 유효한가. 판단은 core/labels.py — run_extract · compare 와 같은 규칙이다."""
    return stale_reason(case, suite, facts) is None


def detail(case, out, problems):
    print(f"\n{LINE}\n  {case['id']}  [{case['set']}]  기대 {case['expect']}\n{LINE}")
    print(f"입력: {case['raw']}")
    print(f"라벨 이유: {case['why']}\n")

    print("[추출]")
    for s in case["stops"]:
        d = f"체류 {s['dwell_minutes']}분 (사용자)" if s.get("dwell_minutes") else "체류 미입력"
        print(f"  {s['start']}  {s['place']}  {d}")
    for hc in case.get("hard_constraints", []):
        print(f"  [필수] {hc['time']}  {hc['place']}  {hc['type']}  (사용자)")

    print("\n[적용한 기본값]")
    print("  없음 — 사용자가 다 말했다" if not out["assumptions"] else "", end="")
    for a in out["assumptions"]:
        print(f"  {a['id']}  {a['field']} = {a['value']}  ({a['rule']})  ← {a['reason']}")
    if not out["assumptions"]:
        print()

    print("\n[검사]")
    for c in out["checks"]:
        print(f"  {MARK[c['status']]} {c['status']:<15} {c['type']:<26} {c['target']}")
        if c["status"] == "fail":
            print(f"      → {c['detail']}")
        elif c["status"] == "unknown":
            print(f"      → [{c['unknown_reason']}] {c['detail']}")
        elif c["status"] == "not_applicable":
            print(f"      → {c['reason']}")
        if c.get("notice"):
            print(f"      안내 {c['notice']}")
        ev = c.get("evidence") or {}
        if ev.get("url"):
            span = f", 유효 ~{ev['valid_until']}" if ev.get("valid_until") else ""
            print(f"      근거 {ev.get('source')} {ev['url']} (확인 {ev.get('checked_at')}{span})")
        elif ev.get("snapshot_id"):
            print(f"      근거 {ev['source']} {ev['method']} snapshot={ev['snapshot_id']}")
        for who, what in (c.get("how_to_resolve") or {}).items():
            print(f"      해결({who}) {what}")

    for h in out["hard_constraints"]:
        e, p = h["event"], h["policy"]
        print(f"\n[필수 조건 {h['id']}]  \"{e['raw']}\"")
        print(f"  사용자가 말한 것   {e['type']} {e['place']} {e['time']}  ({e['source']})")
        print(f"  서비스 권장 기준   도착 {p['required_arrival']} (여유 {p['buffer_minutes']}분, {p['buffer_source']})")
        if p.get("estimated_arrival"):
            basis = "  ← 기본값 체류시간으로 계산한 참고값" if p.get("depends_on_assumptions") else ""
            print(f"  계산한 예상 도착   {p['estimated_arrival']}  "
                  f"({e['time']}까지 {p['margin_vs_event']:+d}분 / 권장까지 {p['margin_vs_required']:+d}분){basis}")
        print(f"  → {MARK[p['status']]} {p['status']}  severity={p['severity']}")
        for k, label in (("detail", ""), ("confirmed", "확인된 것   "), ("not_confirmed", "확인 못 한 것 ")):
            if p.get(k):
                print(f"     {label}{p[k]}")

    print("\n" + render(report(out)))

    n = out["summary"]["counts"]
    print(f"\n[판정]  {out['summary']['verdict']}")
    print(f"  pass {n['pass']} / fail {n['fail']} / unknown {n['unknown']} / n.a. {n['not_applicable']}")
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
    else:
        print("  ✅ 라벨과 일치")


def grade(case, out):
    """기대 판정과 기대 검사 결과를 라벨과 대조한다."""
    problems = []
    if out["summary"]["verdict"] != case["expect"]:
        problems.append(f"verdict 기대 {case['expect']} → 실제 {out['summary']['verdict']}")
    for want in case.get("expect_checks", []):
        got = next((c for c in out["checks"]
                    if c["type"] == want["type"] and c["target"] == want["target"]), None)
        if got is None:
            problems.append(f"{want['type']} / {want['target']} 검사가 아예 없다")
        elif got["status"] != want["status"]:
            problems.append(f"{want['type']} / {want['target']} 기대 {want['status']} → 실제 {got['status']}")
    for want in case.get("expect_hard_constraints", []):
        got = next((h for h in out["hard_constraints"] if h["id"] == want["id"]), None)
        if got is None:
            problems.append(f"필수 조건 {want['id']} 이 없다")
            continue
        for k in ("status", "severity"):
            if k in want and got["policy"].get(k) != want[k]:
                problems.append(f"{want['id']}.{k} 기대 {want[k]} → 실제 {got['policy'].get(k)}")
    return problems


def grade_applied(case, out):
    """
    기대했던 사실·가정·누락 정보가 결과에 흔적으로 남았는지 확인한다.

    expect_applied 의 문장은 사람이 읽는 설명이라 문자열로 대조할 수 없다.
    그 설명이 요구하는 최소 구조가 결과에 남아 있는지를 본다.
    """
    expected = case.get("expect_applied") or {}
    problems = []
    checks = out.get("checks", [])
    hard = out.get("hard_constraints", [])

    if expected.get("facts") and not any(c.get("evidence") for c in checks):
        problems.append("expect_applied.facts 는 있지만 검사 근거가 기록되지 않았다")

    if expected.get("assumptions") and not out.get("assumptions"):
        problems.append("expect_applied.assumptions 는 있지만 assumptions 가 비어 있다")

    has_unknown = any(c.get("status") == UNKNOWN for c in checks) or any(
        h.get("policy", {}).get("status") == UNKNOWN for h in hard
    )
    if expected.get("missing") and not has_unknown:
        problems.append("expect_applied.missing 은 있지만 unknown 결과가 없다")

    return problems


results, rows = [], []
for case in cases:
    started = datetime.now(timezone.utc)
    out = judge(case, facts, policy)
    ms = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 2)
    problems = grade(case, out) + grade_applied(case, out)
    results.append({"id": case["id"], "set": case["set"], "expect": case["expect"],
                    "match": not problems, "problems": problems, "elapsed_ms": ms,
                    "label_current": label_is_current(case),
                    "stale_reason": stale_reason(case, suite, facts),
                    "expect_applied": case.get("expect_applied"), **out})
    rows.append((case, out, problems))

    if not json_only and (show_all or (only and case["id"].startswith(only)) or (not only and problems)):
        detail(case, out, problems)

# ── 요약 ────────────────────────────────────────────────────────────
if not json_only:
    print(f"\n{LINE}\n  요약   facts={FACTS_SNAPSHOT}\n{LINE}")
    print(f"  {'케이스':<26}{'셋':<10}{'기대':<15}{'실제':<15}판정")
    for case, out, problems in rows:
        # 미검토 라벨과 일치했다고 ✅ 를 주면 안 된다. 검토되지 않은 정답과 맞은 것뿐이다.
        if not label_is_current(case):
            mark = "⚠ 미검토(일치)" if not problems else "⚠ 미검토(불일치)"
        else:
            mark = "✅" if not problems else "❌"
        print(f"  {case['id']:<24}{case['set']:<10}{case['expect']:<15}"
              f"{out['summary']['verdict']:<15}{mark}")

    scored = [r for r in results if r["label_current"]]
    stale = [r for r in results if not r["label_current"]]

    for name in ("dev", "holdout"):
        sub = [r for r in scored if r["set"] == name]
        hit = sum(1 for r in sub if r["match"])
        print(f"\n  {name:<9} {hit}/{len(sub)} 일치" if sub else f"\n  {name:<9} 채점 대상 없음")

    # ⭐ 이 프로젝트의 게이트 지표. 0 이 아니면 나머지 숫자는 볼 필요가 없다.
    #    정확도가 높아도 "확인 못 한 일정을 통과시켰다" 가 한 건 있으면
    #    검증 서비스로서는 실패다. 그래서 정확도와 따로 세고 따로 보여준다.
    #    라벨 미검토 케이스를 분모에 넣으면 거짓 경보가 나므로 제외한다.
    leaked = [r for r in scored
              if r["expect"] == "undetermined" and r["summary"]["verdict"] == "feasible"]
    print(f"\n  미확인을 통과로 낸 건수: {len(leaked)} / 채점 {len(scored)}건"
          f"  (0이 아니면 다른 지표를 볼 필요가 없다)")

    if stale:
        print(f"\n  ⚠ 라벨 미검토 {len(stale)}건 — 채점에서 제외했다.")
        print(f"    라벨 기준 스냅샷 {LABEL_SNAPSHOT} ≠ 현재 {FACTS_SNAPSHOT}")
        for r in stale:
            print(f"    {r['id']:<24}{r.get('stale_reason') or '스냅샷 불일치'}")
        print("    → 바뀐 데이터를 보고 정답을 다시 확인한 뒤, 케이스에 labeled_against 를 현재 스냅샷으로 적는다")

run = {
    "_역할": "판정 실행 기록. 최종 판정만이 아니라 케이스별 추출값·적용한 기본값·"
            "검사별 상태와 근거를 남긴다. 실패 원인을 되짚기 위한 파일이며 "
            "run.py 가 덮어쓴다.",
    "run_id": f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
    "version": VERSION,
    "ran_at": datetime.now(timezone.utc).isoformat(),
    "facts_snapshot": facts["snapshot_id"],
    "holidays_verified": facts["holidays"]["verified"],
    "labeled_at": suite["labeled_at"],
    "conditions": run_meta(),
    "results": results,
}
# 일부만 돌린 결과(--case)는 공식 기록을 덮어쓰지 않는다 — compare.py 와 같은 규칙. README 가 인용하는 것은 전체 실행이다
RECORD = "last-run-subset.json" if only else "last-run.json"
(HERE / RECORD).write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

all_match = all(r["match"] for r in results)
if json_only:
    print(json.dumps(run, ensure_ascii=False, indent=2))
else:
    print(f"\n  기록: core/{RECORD}  ({run['run_id']}, {VERSION})")
    print(describe(run["conditions"]))
    if not facts["holidays"]["verified"]:
        print("  ⚠ 공휴일 캘린더 미검증 — 휴무일 예외 규칙이 걸린 검사는 unknown 으로 나온다.")

sys.exit(0 if all_match else 1)
