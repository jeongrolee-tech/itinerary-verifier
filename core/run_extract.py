#!/usr/bin/env python3
"""
추출 평가 + 전체 파이프라인 실행

    python core/run_extract.py            전체 케이스: 자연어 → 추출 → 판정
    python core/run_extract.py --case T05 한 건만
    python core/run_extract.py --extract-only   추출만 채점 (판정 생략)
    python core/run_extract.py --fresh    받아 둔 응답을 쓰지 않고 전부 새로 받는다 (돈이 든다)
    python core/run_extract.py --reference-date 2026-10-01   기준 날짜를 바꿔서 (기본은 tests.json)
    python core/run_extract.py --repeat 5   모호한 시각 · 필수 일정 문제를 다섯 번씩 새로 불러 답이 흔들리는지 잰다
                                            → last-extract-stability.json (돈이 든다. --case T22,T23 으로 문제를 고른다)

추출 문제와 판정 문제를 구분해서 재기 위해 두 단계를 따로 채점한다.
  · 추출 평가 — 날짜·장소·시각·체류시간·필수 조건을 정확히 옮겼는가. 연도와 시각을 말한 것과 추측한 것으로
                맞게 나눴는가. 장소를 지원 범위(정보 있는 장소 · 목록 밖 · 구역)에 맞게 나눴는가.
                입력에 없는 값을 추가했는가. 모호한 것을 미확인으로 남겼는가.
  · 판정 평가 — 사람이 확인한 구조화 일정을 넣었을 때 코드가 기대한 결과를 내는가 (run.py)

추출은 돈이 든다. 모델이 받는 것이 같은 요청은 응답 캐시(response_cache.py)에 받아 둔 응답을 쓰고,
새로 부를 것이 있으면 몇 건이고 얼마쯤인지 보여 준 뒤 y 를 받아야 부른다. 부를 때마다 그 호출 비용과
누적을 찍고, 끝에 이번 실행에서 나간 돈을 찍어 기록(responses.spent_usd)에도 남긴다(spend.py).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from extract import SYSTEM, Extraction, request, to_itinerary  # noqa: E402
from labels import EXTRACTION_FIELDS, case_facts, stale_reason  # noqa: E402
from llm import InvalidOutput, Refusal, ask  # noqa: E402
from model_info import DEFAULT_MODEL, PRICES, cost_usd, effort_for  # noqa: E402
from report import INPUT_SOURCE_NAMES, SET_NAMES, VERDICT_NAMES, pad  # noqa: E402
from response_cache import ResponseCache, confirm_spend, request_key  # noqa: E402
from runmeta import describe, fingerprint, library_versions, reference_date, run_meta  # noqa: E402
from spend import Spend  # noqa: E402
from verdict import EVENT_NAMES, judge, support_scope  # noqa: E402

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
REPEAT = int(flag("--repeat", "0"))
# 반복 실행의 기본 문제 — 3차 피드백 "새 호출은 모호한 시각이나 복합 조건 일부에 한정해도 된다"
#   T12 · T22 · T23  오전 · 오후가 없는 시각. 10월 1일과 6일 실행에서 이런 시각의 출처가 실행마다 뒤집혔다
#   T27 · T33        꼭 타야 하는 열차가 있는 일정. T33 은 장소 셋에 열차까지 있다
STABILITY_CASES = "T12,T22,T23,T27,T33"
only = only or (STABILITY_CASES if REPEAT else None)
MODEL = flag("--model", DEFAULT_MODEL)
EFFORT = effort_for(MODEL, flag("--effort", "low"))
cases = [c for c in suite["cases"] if not only or c["id"].startswith(tuple(only.split(",")))]

# 연도가 빠진 날짜를 푸는 기준 날짜. 실행한 날이 아니라 tests.json 의 값이다 (runmeta.reference_date).
REFERENCE_DATE = reference_date(suite, flag("--reference-date", None))
# 모델이 받는 것은 시스템 프롬프트와 출력 스키마다. 스키마도 지문으로 남기고, 스키마를 만드는 SDK 버전도 적는다
SCHEMA = json.dumps(anthropic.transform_schema(Extraction), ensure_ascii=False, sort_keys=True)
CONDITIONS = run_meta(reference_date=REFERENCE_DATE, libraries=library_versions(),
                      prompts={"extract": fingerprint(SYSTEM), "extract_schema": fingerprint(SCHEMA)})

LINE = "═" * 78
MARK = {"pass": "○", "fail": "✕", "unknown": "?", "not_applicable": "–"}


def grade_extraction(case, got: dict) -> dict[str, list[str]]:
    """tests.json 의 date / date_source / weekday_stated / stops / hard_constraints 가 기대 추출값이다.

    항목(labels.EXTRACTION_FIELDS)마다 어긋난 점을 모은다. 지원 범위 분류는 판정 코드와 같은 함수(verdict.support_scope)로
    기대값과 추출값을 각각 나눠 비교한다 — 장소 이름이나 구역 여부를 틀리면 여기서 드러난다.
    """
    p = {k: [] for k in EXTRACTION_FIELDS}
    if got.get("date") != case["date"]:
        p["date"].append(f"date 기대 {case['date']} → 추출값 {got.get('date')}")
    if got.get("weekday_stated") != case.get("weekday_stated"):
        p["date"].append(f"weekday_stated 기대 {case.get('weekday_stated')} → 추출값 {got.get('weekday_stated')}")
    if got.get("date_source") != case.get("date_source"):
        p["date_source"].append(f"date_source 기대 {case.get('date_source')} → 추출값 {got.get('date_source')}")

    want_stops, got_stops = case["stops"], got.get("stops", [])
    if len(want_stops) != len(got_stops):
        p["place"].append(f"장소 개수 기대 {len(want_stops)} → 추출값 {len(got_stops)}")
    for i, (want, g) in enumerate(zip(want_stops, got_stops)):
        if want["place"] != g.get("place"):
            p["place"].append(f"stops[{i}].place 기대 {want['place']} → 추출값 {g.get('place')}")
        if want.get("start") != g.get("start"):
            p["start"].append(f"stops[{i}].start 기대 {want.get('start')} → 추출값 {g.get('start')}")
        # 라벨에 출처가 없으면 사용자가 말한 시각이다 (tests.json time_rule)
        want_src = want.get("start_source", "explicit" if want.get("start") else "missing")
        if want_src != g.get("start_source"):
            p["start_source"].append(f"stops[{i}].start_source 기대 {want_src} → 추출값 {g.get('start_source')}")
        # 입력에 없던 체류시간을 채워 넣었는지 — 가장 중요한 항목
        if "dwell_minutes" not in want and "dwell_minutes" in g:
            p["dwell"].append(f"stops[{i}] 입력에 없는 체류시간 {g['dwell_minutes']}분을 추가했다")
        elif want.get("dwell_minutes") != g.get("dwell_minutes"):
            p["dwell"].append(f"stops[{i}].dwell_minutes 기대 {want.get('dwell_minutes')} → 추출값 {g.get('dwell_minutes')}")
        want_sup, got_sup = support_scope(want, facts), support_scope(g, facts)
        if want_sup != got_sup:
            p["support"].append(f"stops[{i}] 지원 범위 기대 {want_sup} → 추출값 {got_sup}")

    want_hc, got_hc = case.get("hard_constraints", []), got.get("hard_constraints", [])
    if len(want_hc) != len(got_hc):
        p["hard_constraints"].append(f"필수 일정 개수 기대 {len(want_hc)} → 추출값 {len(got_hc)}")
    for i, (want, g) in enumerate(zip(want_hc, got_hc)):
        for k in ("type", "place", "time"):
            if want[k] != g.get(k):
                p["hard_constraints"].append(f"HC[{i}].{k} 기대 {want[k]} → 추출값 {g.get(k)}")
    return p


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


RUN_ID = f"extract-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def repeat_runs(n: int) -> None:
    """같은 요청을 n 번 새로 불러 답이 흔들리는지 잰다. 응답 캐시는 읽지도 쓰지도 않는다.

    3차 피드백 — 받아 둔 응답을 지금 코드로 다시 채점한 숫자와, 모델을 새로 불렀을 때 같은 답이 나오는지는
    다른 질문이다. 공식 기록(last-extract-run.json)은 문제마다 응답 하나로 낸다. 그 응답은 바꾸지 않고 따로 부른다.
    """
    total = n * len(cases)
    confirm_spend({"구조화 추출 반복": total}, 0, {"구조화 추출 반복": cache.estimate_usd(MODEL, "extract", total)})
    key = ask_key()
    if not key:
        raise SystemExit("키가 비어 있다.")
    client, meter, rows = anthropic.Anthropic(api_key=key), Spend(), []
    for case in cases:
        runs = []
        for _ in range(n):
            try:
                ex, usage = ask(client, **requests[case["id"]])
                got = to_itinerary(ex)
                runs.append({"extracted": got,
                             "wrong_fields": [k for k, msgs in grade_extraction(case, got).items() if msgs]})
            except (Refusal, InvalidOutput) as e:
                usage = e.usage
                runs.append({"error": f"{type(e).__name__}: {e}"})
            except anthropic.APIError as e:
                raise SystemExit(f"❌ API 호출 실패: {type(e).__name__} {e}\n{meter.summary()}") from e
            meter.claude("구조화 추출 반복", usage)
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
    (HERE / "last-extract-stability.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  기록: core/last-extract-stability.json  ({out['run_id']})")
    print(describe(CONDITIONS))

# ⭐ API 를 부르기 전에 요청을 다 만들어 응답 캐시와 맞춰 본다. 모델이 받는 것이 같은 요청은 받아 둔 응답을 쓰고,
#    새로 부를 것이 있을 때만 비용을 보여 주고 물은 뒤에 키를 받는다. 받은 응답은 바로 캐시에 들어가므로
#    도중에 멈춰도 다시 돌리면 남은 것만 부른다.
cache = ResponseCache()
requests = {c["id"]: request(c["raw"], MODEL, today=REFERENCE_DATE, effort=EFFORT) for c in cases}
keys = {cid: request_key(**req) for cid, req in requests.items()}
if REPEAT:
    repeat_runs(REPEAT)
    raise SystemExit
stored = {}  # 문제 → 받아 둔 응답. --fresh 면 쓰지 않고 전부 새로 받아 바꿔 넣는다
if "--fresh" not in args:
    stored = {cid: hit for cid, key in keys.items() if (hit := cache.get(key))}
new_calls = len(cases) - len(stored)
confirm_spend({"구조화 추출": new_calls}, len(stored), {"구조화 추출": cache.estimate_usd(MODEL, "extract", new_calls)})

client = None
if new_calls:
    key = ask_key()
    if not key:
        raise SystemExit("키가 비어 있다.")
    client = anthropic.Anthropic(api_key=key)
results, usages, runs = [], [], set()  # usages — 응답마다 하나. 거부된 호출도 들어간다. runs — 응답을 받은 실행
meter = Spend()  # 이번 실행에서 새로 부른 호출만 센다 — 받아 둔 응답은 돈이 나가지 않는다

for case in cases:
    print(f"\n{LINE}\n  {case['id']}  [{SET_NAMES.get(case['set'], case['set'])}]\n{LINE}")
    print(f"입력: {case['raw']}\n")

    hit = stored.get(case["id"])
    if hit is None:
        try:
            ex, usage = ask(client, **requests[case["id"]])
            # 캐시에는 채점에 쓰는 모양(판정 코드가 읽는 일정과 메모)으로 둔다
            result = {"extracted": to_itinerary(ex), "notes": ex.notes}
        except Refusal as e:
            result, usage = {"error": f"refusal: {e}"}, e.usage  # 거부된 호출도 청구될 수 있다(model_info.billed)
        except InvalidOutput as e:
            result, usage = {"error": f"invalid_output: {e}"}, e.usage  # 끝까지 온 응답이라 청구된다
        except anthropic.APIError as e:
            # 잔액 부족 · 네트워크 오류. 남은 문제를 계속 부르지 않고 멈춘다 — 공식 기록은 건드리지 않는다
            raise SystemExit(f"❌ API 호출 실패: {type(e).__name__} {e}\n"
                             "  여기까지 받은 응답은 응답 캐시에 남아 있다. 다시 돌리면 남은 것만 부른다.\n"
                             f"{meter.summary()}") from e
        hit = cache.put(keys[case["id"]], run=RUN_ID, label="extract", result=result, usage=usage)
        cost = meter.claude("구조화 추출", usage)
        print(f"[비용] 새로 부름 {'단가 모름' if cost is None else f'${cost:.4f}'}"
              f" · 이번 실행 누적 ${meter.claude_usd:.4f}\n")
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
        print(f"  [필수 일정] {hc['time']}  {hc['place']}  {EVENT_NAMES.get(hc['type'], hc['type'])}")
    for n in notes:
        print(f"  메모: {n}")

    ex_fields = grade_extraction(case, got)
    ex_problems = [m for msgs in ex_fields.values() for m in msgs]
    print(f"\n[구조화 추출 채점]  {'✅ 정답과 일치' if not ex_problems else '❌'}")
    for p in ex_problems:
        print(f"  ❌ {p}")
    print(f"  토큰 in {usage['input_tokens']} / out {usage['output_tokens']}"
          f"  (프롬프트 캐시 읽기 {usage['cache_read']} / 쓰기 {usage['cache_write']})"
          f"  {usage['latency_ms']}ms" + (f"  — 받아 둔 응답 ({hit['run']})" if case["id"] in stored else ""))

    row = {"id": case["id"], "set": case["set"], "extraction_ok": not ex_problems,
           "extraction_problems": ex_problems,
           "extraction_fields": {k: not msgs for k, msgs in ex_fields.items()},
           "extracted": got, "usage": usage,
           "notes": notes}

    if not extract_only:
        # 라벨을 검토한 뒤 데이터가 바뀌었으면 판정 채점에서 뺀다 — run.py · compare.py 와 같은 규칙
        stale = stale_reason(case, suite, facts)
        row["label_current"], row["stale_reason"] = stale is None, stale
        # 추출 결과를 그대로 판정 코어에 넣는다. 사람이 고치지 않는다.
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

# ── 요약 ────────────────────────────────────────────────────────────
print(f"\n{LINE}\n  요약\n{LINE}")
print(f"  {pad('문제', 26)}{pad('구조화 추출', 14)}{pad('판정', 8)}전체")
for r in results:
    ex_m = "✅" if r.get("extraction_ok") else "❌"
    v_m = "–" if extract_only else ("✅" if r.get("verdict_ok") else "❌")
    e2e = "–" if extract_only else ("✅" if r.get("end_to_end_ok") else "❌")
    print(f"  {pad(r['id'], 26)}{pad(ex_m, 14)}{pad(v_m, 8)}{e2e}")

n_ex = sum(1 for r in results if r.get("extraction_ok"))
print(f"\n  구조화 추출 {n_ex}/{len(results)}")
graded = [r for r in results if "extraction_fields" in r]  # 거부 · 형식 오류는 항목을 채점하지 못했다
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
cost = None if None in costs else sum(costs)  # 단가를 모르는 모델이면 비용을 적지 않는다
# 토큰과 비용은 응답을 받을 때 잰 값이다. 받아 둔 응답을 다시 쓴 실행에서도 같은 값이 나온다
print(f"\n  토큰 in {totals['in']:,} / out {totals['out']:,}"
      f" / 프롬프트 캐시 읽기 {totals['cache_read']:,} 쓰기 {totals['cache_write']:,}")
print(f"  비용 약 ${cost:.4f}   (건당 ${cost / max(len(results), 1):.4f})" if cost is not None
      else f"  비용 — {MODEL} 의 단가를 모른다(model_info.PRICES)")
lat = [r["usage"]["latency_ms"] for r in results if r.get("usage")]
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
    # LLM 응답을 이번에 새로 받았는지, 받아 둔 것을 썼는지와 그 응답을 받은 실행
    # spent_usd 는 이번 실행에서 실제로 나간 돈이다. 아래 estimated_cost_usd 는 응답을 받을 때 든 값이다
    "responses": {"new": new_calls, "reused": len(stored), "received_in": sorted(runs),
                  "spent_usd": round(meter.claude_usd, 4)},
    "tokens": totals,
    "pricing_per_mtok": PRICES.get(MODEL),
    "estimated_cost_usd": None if cost is None else round(cost, 4),
    "estimated_cost_per_case_usd": None if cost is None else round(cost / max(len(results), 1), 4),
    "results": results,
}
# 일부만 돌린 결과(--case · --extract-only)는 공식 기록을 덮어쓰지 않는다 — compare.py 와 같은 규칙
RECORD = "last-extract-run-subset.json" if only or extract_only else "last-extract-run.json"
(HERE / RECORD).write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  기록: core/{RECORD}  ({run['run_id']})")
print(describe(CONDITIONS))
