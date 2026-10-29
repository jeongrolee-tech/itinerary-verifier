#!/usr/bin/env python3
"""추출 평가. 추출과 판정을 따로 채점해 틀린 곳이 어느 단계인지 가른다."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from extract import SYSTEM, Extraction, request, to_itinerary  # noqa: E402
from labels import EXTRACTION_FIELDS, case_facts, grade_extraction, stale_reason  # noqa: E402
from llm import BatchMiss, InvalidOutput, Refusal, ask_many  # noqa: E402
from model_info import DEFAULT_MODEL, PRICES, cost_usd, effort_for  # noqa: E402
from report import INPUT_SOURCE_NAMES, SET_NAMES, VERDICT_NAMES, pad  # noqa: E402
from response_cache import ResponseCache, confirm_spend, request_key  # noqa: E402
from runmeta import describe, fingerprint, library_versions, record_name, reference_date, run_meta  # noqa: E402
from spend import Spend  # noqa: E402
from verdict import EVENT_NAMES, judge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
facts, policy = load("facts.json"), load("policy.json")

args = sys.argv[1:]
extract_only = "--extract-only" in args
flag = lambda name, default: next(
    (args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), default)
SUITE_FILE = flag("--suite", "tests.json")
suite = load(SUITE_FILE)
only = flag("--case", None)
REPEAT = int(flag("--repeat", "0"))
# 반복은 답이 흔들렸던 오전 · 오후 없는 시각과 꼭 타야 할 열차 문제만 부른다.
STABILITY_CASES = "T12,T22,T23,T27,T33"
only = only or (STABILITY_CASES if REPEAT else None)
MODEL = flag("--model", DEFAULT_MODEL)
EFFORT = effort_for(MODEL, flag("--effort", "low"))
cases = [c for c in suite["cases"] if not only or c["id"].startswith(tuple(only.split(",")))]

REFERENCE_DATE = reference_date(suite, flag("--reference-date", None))
# 모델은 출력 스키마도 받으므로 스키마 지문도 남긴다.
SCHEMA = json.dumps(anthropic.transform_schema(Extraction), ensure_ascii=False, sort_keys=True)
CONDITIONS = run_meta(SUITE_FILE, reference_date=REFERENCE_DATE, libraries=library_versions(),
                      prompts={"extract": fingerprint(SYSTEM), "extract_schema": fingerprint(SCHEMA)})

LINE = "═" * 78
MARK = {"pass": "○", "fail": "✕", "unknown": "?", "not_applicable": "–"}




def ask_key() -> str:
    import os
    from getpass import getpass
    for a in args:
        if a.startswith("--key="):
            return a[6:].strip()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    return getpass("Anthropic API 키 입력: ").strip()


RUN_ID = f"extract-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def call_all(client, meter: Spend, label: str, todo: dict[str, dict]) -> dict:
    """첫 요청만 바로, 나머지는 배치로 불러 값을 절반으로 줄인다."""
    try:
        got = ask_many(client, todo)
    except anthropic.APIError as e:
        raise SystemExit(f"❌ API 호출 실패: {type(e).__name__} {e}\n{meter.summary()}") from e
    for name, ans in got.items():
        usage = None if isinstance(ans, BatchMiss) else ans.usage if isinstance(ans, Exception) else ans[1]
        if usage is None:
            continue  # 배치에서 못 받은 답은 청구되지 않는다
        cost = meter.claude(label, usage)
        print(f"[비용] {name.split('-')[0]} {'배치' if usage.get('batch') else '바로'} "
              f"{'단가 모름' if cost is None else f'${cost:.4f}'} · 이번 실행 누적 ${meter.claude_usd:.4f}")
    return got


def repeat_runs(n: int) -> None:
    """같은 요청을 n 번 새로 불러 답이 흔들리는지 잰다. 공식 기록의 응답은 건드리지 않는다."""
    total = n * len(cases)
    confirm_spend({"구조화 추출 반복": total}, 0,
                  {"구조화 추출 반복": cache.estimate_usd(MODEL, "extract", total, batch=True)})
    key = ask_key()
    if not key:
        raise SystemExit("키가 비어 있다.")
    client, meter, rows = anthropic.Anthropic(api_key=key), Spend(), []
    got = call_all(client, meter, "구조화 추출 반복",
                   {f"{case['id']}|{r}": requests[case["id"]] for case in cases for r in range(n)})
    for case in cases:
        runs = []
        for r in range(n):
            ans = got[f"{case['id']}|{r}"]
            if isinstance(ans, BatchMiss):
                raise SystemExit(f"❌ 배치에서 답을 받지 못했다(청구되지 않는다) — {ans}. 반복 기록을 남기지 않는다\n"
                                 f"{meter.summary()}")
            if isinstance(ans, (Refusal, InvalidOutput)):
                runs.append({"error": f"{type(ans).__name__}: {ans}"})
                continue
            got_it = to_itinerary(ans[0])
            runs.append({"extracted": got_it,
                         "wrong_fields": [k for k, msgs in grade_extraction(case, got_it, facts).items() if msgs]})
        right = sum(1 for r in runs if "extracted" in r and not r["wrong_fields"])
        distinct = len({json.dumps(r.get("extracted") or r.get("error"), ensure_ascii=False, sort_keys=True)
                        for r in runs})
        wrong = sorted({k for r in runs for k in r.get("wrong_fields", [])})
        rows.append({"id": case["id"], "right": right, "distinct_answers": distinct, "runs": runs})
        print(f"  {pad(case['id'], 26)}정답과 일치 {right}/{n} · 서로 다른 답 {distinct}가지"
              + (f" · 틀린 항목 {', '.join(EXTRACTION_FIELDS[k] for k in wrong)}" if wrong else ""))
    print(meter.summary())
    out = {"_역할": "구조화 추출 반복 기록. 같은 요청을 여러 번 새로 불러 답이 흔들리는지 잰다. "
                    "공식 기록(last-extract-run.json)과 따로 둔다. run_extract.py --repeat 이 덮어쓴다.",
           "run_id": RUN_ID.replace("extract-", "extract-repeat-"), "ran_at": datetime.now(timezone.utc).isoformat(),
           "model": MODEL, "effort": EFFORT, "repeat": n, "conditions": CONDITIONS,
           "spent_usd": round(meter.claude_usd, 4), "rows": rows}
    name = record_name("last-extract-stability.json", SUITE_FILE)
    (HERE / name).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  기록: core/{name}  ({out['run_id']})")
    print(describe(CONDITIONS))

# 부르기 전에 응답 캐시와 맞춰 보고, 새 호출이 있으면 건수와 예상 비용을 보여 주고 묻는다.
cache = ResponseCache()
requests = {c["id"]: request(c["raw"], MODEL, today=REFERENCE_DATE, effort=EFFORT) for c in cases}
keys = {cid: request_key(**req) for cid, req in requests.items()}
if REPEAT:
    repeat_runs(REPEAT)
    raise SystemExit
stored = {}
if "--fresh" not in args:
    stored = {cid: hit for cid, key in keys.items() if (hit := cache.get(key))}
new_calls = len(cases) - len(stored)
confirm_spend({"구조화 추출": new_calls}, len(stored),
              {"구조화 추출": cache.estimate_usd(MODEL, "extract", new_calls, batch=True)})

results, usages, runs = [], [], set()
meter = Spend()
fetched = {}
if new_calls:
    key = ask_key()
    if not key:
        raise SystemExit("키가 비어 있다.")
    got = call_all(anthropic.Anthropic(api_key=key), meter, "구조화 추출",
                   {c["id"]: requests[c["id"]] for c in cases if c["id"] not in stored})
    missed = []
    for cid, ans in got.items():
        if isinstance(ans, BatchMiss):
            missed.append(f"{cid} — {ans}")
            continue
        if isinstance(ans, Refusal):
            result, usage = {"error": f"refusal: {ans}"}, ans.usage  # 거부된 호출도 청구될 수 있다
        elif isinstance(ans, InvalidOutput):
            result, usage = {"error": f"invalid_output: {ans}"}, ans.usage  # 형식 오류도 청구된 호출이다
        else:
            # 캐시에는 판정 코드가 읽는 일정과 메모로 둔다.
            result, usage = {"extracted": to_itinerary(ans[0]), "notes": ans[0].notes}, ans[1]
        fetched[cid] = cache.put(keys[cid], run=RUN_ID, label="extract", result=result, usage=usage)
    if missed:
        # 일부만 받은 채로 공식 기록을 쓰지 않는다.
        raise SystemExit(f"❌ 배치에서 {len(missed)}건의 답을 받지 못했다(청구되지 않는다): {' / '.join(missed[:3])}\n"
                         "  여기까지 받은 응답은 응답 캐시에 남아 있다. 다시 돌리면 남은 것만 부른다.\n"
                         f"{meter.summary()}")

for case in cases:
    print(f"\n{LINE}\n  {case['id']}  [{SET_NAMES.get(case['set'], case['set'])}]\n{LINE}")
    print(f"입력: {case['raw']}\n")

    hit = stored.get(case["id"]) or fetched[case["id"]]
    result, usage = hit["result"], hit["usage"]
    usages.append(usage)
    runs.add(hit["run"])

    if "error" in result:
        print(f"❌ 구조화 추출 실패 — {result['error']}")
        results.append({"id": case["id"], "extraction_ok": False, "error": result["error"], "usage": usage})
        continue
    got, notes = result["extracted"], result["notes"]

    print("[추출 결과]")
    wd = f"  사용자가 말한 요일 {got['weekday_stated']}" if got["weekday_stated"] else ""
    print(f"  날짜  {got['date']}  ({INPUT_SOURCE_NAMES[got['date_source']]}){wd}")
    for s in got["stops"]:
        dwell = f"{s['dwell_minutes']}분" if "dwell_minutes" in s else "미입력"
        area = f"  ⚠ 동네 — {s.get('scope_note')}" if s["scope"] == "area" else ""
        print(f"  {s.get('start') or '--:--'}  {s['place']}  체류 {dwell}  ({INPUT_SOURCE_NAMES[s['start_source']]}){area}")
    for hc in got["hard_constraints"]:
        print(f"  [필수 일정] {hc.get('time') or '시각 모름'}  {hc['place']}  {EVENT_NAMES.get(hc['type'], hc['type'])}"
              + ("  (추정)" if hc.get("time_source") == "inferred" else ""))
    if got.get("avoid_taxi"):
        print("  택시 안 탐")
    for n in notes:
        print(f"  메모: {n}")

    ex_fields = grade_extraction(case, got, facts)
    ex_problems = [m for msgs in ex_fields.values() for m in msgs]
    print(f"\n[구조화 추출 채점]  {'✅ 정답과 일치' if not ex_problems else '❌'}")
    for p in ex_problems:
        print(f"  ❌ {p}")
    print(f"  토큰 in {usage['input_tokens']} / out {usage['output_tokens']}"
          f"  (프롬프트 캐시 읽기 {usage['cache_read']} / 쓰기 {usage['cache_write']})"
          + (f"  {usage['latency_ms']}ms" if usage["latency_ms"] is not None else "  배치")
          + (f"  — 받아 둔 응답 ({hit['run']})" if case["id"] in stored else ""))

    row = {"id": case["id"], "set": case["set"], "extraction_ok": not ex_problems,
           "extraction_problems": ex_problems,
           "extraction_fields": {k: not msgs for k, msgs in ex_fields.items()},
           "extracted": got, "usage": usage,
           "notes": notes}

    if not extract_only:
        # 정답을 확인한 뒤 데이터가 바뀐 문제는 판정 채점에서 뺀다.
        stale = stale_reason(case, suite, facts)
        row["label_current"], row["stale_reason"] = stale is None, stale
        # 사람이 고치지 않은 추출 결과를 그대로 판정한다.
        if got.get("date"):
            out = judge(got, case_facts(case, facts), policy)
            ok = out["summary"]["verdict"] == case["expect"]
            print(f"\n[판정]  {VERDICT_NAMES[out['summary']['verdict']]}  "
                  f"{'✅ 정답과 일치' if ok else '❌ 정답 ' + VERDICT_NAMES[case['expect']]}")
            n = out["summary"]["counts"]
            print(f"  통과 {n['pass']} / 위반 {n['fail']} / 미확인 {n['unknown']} / 해당 없음 {n['not_applicable']}")
            row["verdict"] = out["summary"]["verdict"]
            row["verdict_ok"] = ok
            row["end_to_end_ok"] = ok and not ex_problems
        else:
            print("\n[판정]  건너뜀 — 날짜를 추출하지 못했다")
            row["verdict"] = None
            row["end_to_end_ok"] = False

    results.append(row)

# 요약
print(f"\n{LINE}\n  요약\n{LINE}")
print(f"  {pad('문제', 26)}{pad('구조화 추출', 14)}{pad('판정', 8)}전체")
for r in results:
    ex_m = "✅" if r.get("extraction_ok") else "❌"
    v_m = "–" if extract_only else ("✅" if r.get("verdict_ok") else "❌")
    e2e = "–" if extract_only else ("✅" if r.get("end_to_end_ok") else "❌")
    print(f"  {pad(r['id'], 26)}{pad(ex_m, 14)}{pad(v_m, 8)}{e2e}")

n_ex = sum(1 for r in results if r.get("extraction_ok"))
print(f"\n  구조화 추출 {n_ex}/{len(results)}")
graded = [r for r in results if "extraction_fields" in r]  # 거부 · 형식 오류는 항목 채점이 없다
print("  항목별 " + " · ".join(
    f"{label} {sum(r['extraction_fields'][k] for r in graded)}/{len(results)}" for k, label in EXTRACTION_FIELDS.items()))
if not extract_only:
    scored = [r for r in results if r.get("label_current", True)]
    stale = [r for r in results if not r.get("label_current", True)]
    n_e2e = sum(1 for r in scored if r.get("end_to_end_ok"))
    n_v = sum(1 for r in scored if r.get("verdict_ok"))
    print(f"  판정 {n_v}/{len(scored)}")
    print(f"  전체 {n_e2e}/{len(scored)}  (구조화 추출과 판정이 모두 맞은 건수)")
    if stale:
        print(f"  ⚠ 정답 재확인 필요 {len(stale)}건은 판정 채점에서 뺐다: " + ", ".join(r["id"] for r in stale))

totals = {"in": sum(u["input_tokens"] for u in usages), "out": sum(u["output_tokens"] for u in usages),
          "cache_read": sum(u["cache_read"] for u in usages),
          "cache_write": sum(u["cache_write"] for u in usages)}
costs = [cost_usd(MODEL, u) for u in usages]
cost = None if None in costs else sum(costs)
# 응답을 받을 때 든 비용이다. 이번 실행에서 나간 돈은 spent_usd 에 따로 적는다.
print(f"\n  토큰 in {totals['in']:,} / out {totals['out']:,}"
      f" / 프롬프트 캐시 읽기 {totals['cache_read']:,} 쓰기 {totals['cache_write']:,}")
print(f"  비용 약 ${cost:.4f}   (건당 ${cost / max(len(results), 1):.4f})" if cost is not None
      else f"  비용 — {MODEL} 의 단가를 모른다(model_info.PRICES)")
lat = [r["usage"]["latency_ms"] for r in results if r.get("usage") and r["usage"]["latency_ms"] is not None]
if lat:
    print(f"  지연 중앙값 {sorted(lat)[len(lat) // 2]}ms / 최대 {max(lat)}ms")
print(f"  모델 {MODEL} / effort {EFFORT}")
print(f"  (위 비용은 응답을 받을 때 든 값이다. 받아 둔 응답 {len(stored)}건은 이번에 돈이 나가지 않았다)\n")
print(meter.summary())

run = {
    "_역할": "추출 실행 기록. 자연어에서 무엇을 뽑았는지, 기대 추출값과 어디가 "
            "달랐는지, 모델·비용·지연을 남긴다. run_extract.py 가 덮어쓴다.",
    "run_id": RUN_ID,
    "ran_at": datetime.now(timezone.utc).isoformat(),
    "model": MODEL,
    "effort": EFFORT,
    "facts_snapshot": facts["snapshot_id"],
    "conditions": CONDITIONS,
    "responses": {"new": new_calls, "reused": len(stored), "received_in": sorted(runs),
                  "spent_usd": round(meter.claude_usd, 4)},
    "tokens": totals,
    "pricing_per_mtok": PRICES.get(MODEL),
    "estimated_cost_usd": None if cost is None else round(cost, 4),
    "estimated_cost_per_case_usd": None if cost is None else round(cost / max(len(results), 1), 4),
    "results": results,
}
# 공식 기록은 전체 실행만 쓴다. 일부만 돌린 결과는 따로 남긴다.
RECORD = record_name("last-extract-run-subset.json" if only or extract_only else "last-extract-run.json", SUITE_FILE)
(HERE / RECORD).write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  기록: core/{RECORD}  ({run['run_id']})")
print(describe(CONDITIONS))
