#!/usr/bin/env python3
"""
추출 평가 + 전체 파이프라인 실행

    python core/run_extract.py            전체 케이스: 자연어 → 추출 → 판정
    python core/run_extract.py --case T05 한 건만
    python core/run_extract.py --extract-only   추출만 채점 (판정 생략)

추출 문제와 판정 문제를 구분해서 재기 위해 두 단계를 따로 채점한다.
  · 추출 평가 — 날짜·장소·시각·체류시간·필수 조건을 정확히 옮겼는가.
                입력에 없는 값을 추가했는가. 모호한 것을 미확인으로 남겼는가.
  · 판정 평가 — 사람이 확인한 구조화 일정을 넣었을 때 코드가 기대한 결과를 내는가 (run.py)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from extract import extract, to_itinerary  # noqa: E402
from verdict import judge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
facts, policy, suite = load("facts.json"), load("policy.json"), load("tests.json")

args = sys.argv[1:]
extract_only = "--extract-only" in args
flag = lambda name, default: next(
    (args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), default)
only = flag("--case", None)
MODEL = flag("--model", "claude-opus-5")
EFFORT = flag("--effort", "low")
PRICE = {  # per MTok (input, output)
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
cases = [c for c in suite["cases"] if not only or c["id"].startswith(only)]

LINE = "═" * 78
MARK = {"pass": "○", "fail": "✕", "unknown": "?", "not_applicable": "–"}


def grade_extraction(case, got: dict) -> list[str]:
    """tests.json 의 date / stops / hard_constraints 가 기대 추출값이다."""
    problems = []
    if got.get("date") != case["date"]:
        problems.append(f"date 기대 {case['date']} → 추출 {got.get('date')}")

    want_stops, got_stops = case["stops"], got.get("stops", [])
    if len(want_stops) != len(got_stops):
        problems.append(f"스톱 개수 기대 {len(want_stops)} → 추출 {len(got_stops)}")
    for i, want in enumerate(want_stops):
        if i >= len(got_stops):
            break
        g = got_stops[i]
        if want["place"] != g.get("place"):
            problems.append(f"stops[{i}].place 기대 {want['place']} → 추출 {g.get('place')}")
        if want.get("start") != g.get("start"):
            problems.append(f"stops[{i}].start 기대 {want.get('start')} → 추출 {g.get('start')}")
        # 입력에 없던 체류시간을 채워 넣었는지 — 가장 중요한 항목
        if "dwell_minutes" not in want and "dwell_minutes" in g:
            problems.append(f"stops[{i}] 입력에 없는 체류시간 {g['dwell_minutes']}분을 추가했다")
        elif want.get("dwell_minutes") != g.get("dwell_minutes"):
            problems.append(f"stops[{i}].dwell_minutes 기대 {want.get('dwell_minutes')} → 추출 {g.get('dwell_minutes')}")

    want_hc, got_hc = case.get("hard_constraints", []), got.get("hard_constraints", [])
    if len(want_hc) != len(got_hc):
        problems.append(f"필수 조건 개수 기대 {len(want_hc)} → 추출 {len(got_hc)}")
    for i, want in enumerate(want_hc):
        if i >= len(got_hc):
            break
        for k in ("type", "place", "time"):
            if want[k] != got_hc[i].get(k):
                problems.append(f"HC[{i}].{k} 기대 {want[k]} → 추출 {got_hc[i].get(k)}")
    return problems


def ask_key() -> str:
    """환경변수에 있으면 그걸 쓰고, 없으면 물어본다. 입력값은 화면에 안 찍힌다."""
    import os
    from getpass import getpass
    for a in args:
        if a.startswith("--key="):
            return a[6:].strip()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    return getpass("Anthropic API 키 입력: ").strip()


key = ask_key()
if not key:
    raise SystemExit("키가 비어 있다.")

client = anthropic.Anthropic(api_key=key)
results, totals = [], {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}

for case in cases:
    print(f"\n{LINE}\n  {case['id']}  [{case['set']}]\n{LINE}")
    print(f"입력: {case['raw']}\n")

    try:
        ex, usage = extract(case["raw"], client, model=MODEL, effort=EFFORT)
    except anthropic.APIError as e:
        print(f"❌ 추출 실패: {type(e).__name__} {e}")
        results.append({"id": case["id"], "extraction_ok": False, "error": str(e)})
        continue

    totals["in"] += usage["input_tokens"]
    totals["out"] += usage["output_tokens"]
    totals["cache_read"] += usage["cache_read"]
    totals["cache_write"] += usage["cache_write"]
    got = to_itinerary(ex)

    print("[추출 결과]")
    wd = f"  사용자가 말한 요일 {ex.weekday_stated}" if ex.weekday_stated else ""
    print(f"  날짜  {ex.date}  ({ex.date_source}){wd}")
    for s in ex.stops:
        dwell = f"{s.dwell_minutes}분" if s.dwell_minutes is not None else "미입력"
        area = f"  ⚠ area — {s.scope_note}" if s.scope == "area" else ""
        print(f"  {s.start or '--:--'}  {s.place}  체류 {dwell}  ({s.start_source}){area}")
    for hc in ex.hard_constraints:
        print(f"  [필수] {hc.time}  {hc.place}  {hc.type}")
    for n in ex.notes:
        print(f"  note: {n}")

    ex_problems = grade_extraction(case, got)
    print(f"\n[추출 채점]  {'✅ 라벨과 일치' if not ex_problems else '❌'}")
    for p in ex_problems:
        print(f"  ❌ {p}")
    print(f"  토큰 in {usage['input_tokens']} / out {usage['output_tokens']}"
          f"  (캐시 읽기 {usage['cache_read']} / 쓰기 {usage['cache_write']})"
          f"  {usage['latency_ms']}ms")

    row = {"id": case["id"], "set": case["set"], "extraction_ok": not ex_problems,
           "extraction_problems": ex_problems, "extracted": got, "usage": usage,
           "notes": ex.notes}

    if not extract_only:
        # 추출 결과를 그대로 판정 코어에 넣는다. 사람이 고치지 않는다.
        if got.get("date"):
            out = judge(got, facts, policy)
            ok = out["summary"]["verdict"] == case["expect"]
            print(f"\n[판정]  {out['summary']['verdict']}  "
                  f"{'✅ 기대와 일치' if ok else '❌ 기대 ' + case['expect']}")
            n = out["summary"]["counts"]
            print(f"  pass {n['pass']} / fail {n['fail']} / unknown {n['unknown']} / n.a. {n['not_applicable']}")
            row["verdict"] = out["summary"]["verdict"]
            row["verdict_ok"] = ok
            row["end_to_end_ok"] = ok and not ex_problems
        else:
            print("\n[판정]  건너뜀 — 날짜를 추출하지 못했다")
            row["verdict"] = None
            row["end_to_end_ok"] = False

    results.append(row)

# ── 요약 ────────────────────────────────────────────────────────────
print(f"\n{LINE}\n  요약\n{LINE}")
print(f"  {'케이스':<26}{'추출':<8}{'판정':<8}전체")
for r in results:
    ex_m = "✅" if r.get("extraction_ok") else "❌"
    v_m = "–" if extract_only else ("✅" if r.get("verdict_ok") else "❌")
    e2e = "–" if extract_only else ("✅" if r.get("end_to_end_ok") else "❌")
    print(f"  {r['id']:<24}{ex_m:<8}{v_m:<8}{e2e}")

n_ex = sum(1 for r in results if r.get("extraction_ok"))
print(f"\n  추출 {n_ex}/{len(results)}")
if not extract_only:
    n_e2e = sum(1 for r in results if r.get("end_to_end_ok"))
    n_v = sum(1 for r in results if r.get("verdict_ok"))
    print(f"  판정 {n_v}/{len(results)}")
    print(f"  전체 {n_e2e}/{len(results)}  (추출과 판정이 모두 맞은 건수)")

# input_tokens 에는 캐시 읽기·쓰기 토큰이 안 들어 있으므로 따로 더해야 한다.
#   캐시 쓰기 = 입력의 1.25배 (5분 TTL) / 캐시 읽기 = 입력의 0.1배
IN, OUT = PRICE.get(MODEL, (5.0, 25.0))
cost = (totals["in"] * IN
        + totals["cache_write"] * IN * 1.25
        + totals["cache_read"] * IN * 0.1
        + totals["out"] * OUT) / 1e6
print(f"\n  토큰 in {totals['in']:,} / out {totals['out']:,}"
      f" / 캐시 읽기 {totals['cache_read']:,} 쓰기 {totals['cache_write']:,}")
print(f"  비용 약 ${cost:.4f}   (건당 ${cost / max(len(results), 1):.4f})")
lat = [r["usage"]["latency_ms"] for r in results if r.get("usage")]
if lat:
    print(f"  지연 중앙값 {sorted(lat)[len(lat) // 2]}ms / 최대 {max(lat)}ms")
print(f"  모델 {MODEL} / effort {EFFORT}")

run = {
    "run_id": f"extract-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
    "ran_at": datetime.now(timezone.utc).isoformat(),
    "model": MODEL,
    "effort": EFFORT,
    "facts_snapshot": facts["snapshot_id"],
    "tokens": totals,
    "pricing_per_mtok": {"input": IN, "output": OUT, "cache_write": IN * 1.25, "cache_read": IN * 0.1},
    "estimated_cost_usd": round(cost, 4),
    "estimated_cost_per_case_usd": round(cost / max(len(results), 1), 4),
    "results": results,
}
(HERE / "last-extract-run.json").write_text(
    json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  기록: core/last-extract-run.json  ({run['run_id']})")
