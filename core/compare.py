#!/usr/bin/env python3
"""비교 실험. 판정 방식 네 가지를 같은 문제로 돌려 차이가 어디서 오는지 가른다."""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))
from labels import case_facts, grade_answer, stale_reason  # noqa: E402
from model_info import DEFAULT_MODEL, PRICES, effort_for  # noqa: E402
from report import ARM_NAMES, VERDICT_NAMES, pad, width  # noqa: E402
from runmeta import describe, fingerprint, library_versions, record_name, reference_date, run_meta  # noqa: E402
from spend import Spend  # noqa: E402
from verdict import judge  # noqa: E402

ANSWER_NAMES = {**VERDICT_NAMES, "refused": "거부", "invalid": "형식 오류"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
facts, policy = load("facts.json"), load("policy.json")

args = sys.argv[1:]
flag = lambda name, d: next(
    (args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), d)
SUITE_FILE = flag("--suite", "tests.json")
suite = load(SUITE_FILE)
only = flag("--case", None)
REPEAT = int(flag("--repeat", "0"))
# 반복은 판정 규칙이 여럿 겹치는 문제만 부른다.
STABILITY_CASES = "T15,T17,T27,T28"
only = only or (STABILITY_CASES if REPEAT else None)
MODEL = flag("--model", DEFAULT_MODEL)
EFFORT = effort_for(MODEL, flag("--effort", "low"))
ALL_ARMS = ["llm_naive", "llm_only", "llm_with_facts", "code"]
# 반복은 판정 코드와 입력이 같은 llm_with_facts 만 부른다.
ARMS = flag("--arms", "llm_with_facts" if REPEAT else ",".join(ALL_ARMS)).split(",")

LINE = "═" * 78

cases = [c for c in suite["cases"] if not only or c["id"].startswith(tuple(only.split(",")))]


def label_is_current(case: dict) -> bool:
    return stale_reason(case, suite, facts) is None


# 코드 출력과 같은 모양으로 받아야 같은 기준으로 채점한다.
def build_models():
    from pydantic import BaseModel, Field

    class CheckJudgment(BaseModel):
        type: Literal["CLOSED_DAY", "ADMISSION_NOT_POSSIBLE",
                      "DWELL_NOT_COMPLETABLE", "INSUFFICIENT_TRAVEL_TIME"]
        target: str = Field(description="장소명, 또는 이동 구간이면 'A → B'")
        status: Literal["pass", "fail", "unknown", "not_applicable"]
        reason: str = Field(description="이 상태로 판단한 근거. 어떤 값을 썼는지 적는다")

    class Judgment(BaseModel):
        checks: list[CheckJudgment]
        verdict: Literal["feasible", "infeasible", "undetermined"]
        message: str = Field(description="사용자에게 보여줄 한 문장")

    return Judgment


# 프롬프트
# STATES 는 정의, GUIDANCE 는 지시다. 지시를 따라 낸 unknown 은 발견이 아니다.

STATES = """검사 하나당 네 상태 중 하나를 고른다.

  pass            조건을 만족한다
  fail            조건 위반이 있다
  unknown         판단하지 못했다
  not_applicable  그 검사가 적용되지 않는다 (검사 자체가 성립하지 않는다)

전체 판정:
  infeasible     실행할 수 없는 부분이 있다
  undetermined   판단을 보류한다
  feasible       실행할 수 있다

검사 종류는 CLOSED_DAY(휴무일), ADMISSION_NOT_POSSIBLE(입장마감 이후 도착이라 입장 불가. 개장 전 도착은
기다리면 들어가므로 위반이 아니고, 체류와 이동을 개장 시각부터 계산한다),
DWELL_NOT_COMPLETABLE(입장은 되지만 체류를 마치기 전에 폐장),
INSUFFICIENT_TRAVEL_TIME(이동시간 > 다음 장소 시작까지 남은 시간. 권장 여유(buffer_minutes)를 못 채운 것만으로는
위반이 아니다. 이동수단은 정해져 있지 않아서 어떤 수단으로든 갈 수 있으면 되므로, 이동 0분으로도 늦을 때만
위반이다) 네 가지다."""

GUIDANCE = """검사가 **불필요**하면 not_applicable, 검사가 **필요한데 못 끝냈으면** unknown 이다.
영업시간을 모르는 것은 not_applicable 이 아니라 unknown 이다.

전체 판정은 이 기준으로 계산한다.
  infeasible     확인된 위반이 하나라도 있다
  undetermined   위반은 없지만 필수 검사가 미확인이다
  feasible       필요한 검사가 모두 통과했다"""

# 지시 없이 물어 근거가 없을 때 무엇을 하는지 본다.
SYSTEM_LLM_NAIVE = f"""너는 여행 일정의 실행 가능성을 검증한다.

{STATES}"""

SYSTEM_LLM_ONLY = f"""너는 여행 일정의 실행 가능성을 검증한다.

{STATES}

{GUIDANCE}

**사실 데이터는 주어지지 않는다.** 운영시간·휴무일·이동시간을 확보하지 못한 상태다.
근거 없이 추측해서 pass 나 fail 을 내지 말고, 확보하지 못한 근거가 필요한 검사는 unknown 으로 둔다."""

# 판정 코드와 같은 규칙을 줘야 방식 차이만 남는다. verdict.py 를 고치면 여기도 고친다.
RULES = """판정 규칙 — 판정 코드가 쓰는 규칙과 같다. 이 규칙대로 판정한다.

시간표와 휴무일
  1. 방문일이 공휴일(holidays.dates)이면 holiday 가 true 인 시간표를 먼저 쓴다. 요일 시간표보다 우선한다.
     예: 공휴일인 금요일은 금요일 시간표가 아니라 공휴일 시간표다. 그 밖에는 months · weekdays 가 맞는 시간표를 쓴다.
  2. 정기휴일(closed_days.weekly)이 공휴일과 겹치면 exception_rule 을 따른다.
     shift_to_next_nonholiday — 그날은 열고, 그다음 첫 비공휴일에 쉰다.
     open_if_holiday_no_shift — 그날은 열고, 쉬는 날을 밀지 않는다.
     값이 없으면 공휴일이어도 쉰다. fixed_dates(MM-DD)는 그날 쉰다. 두 규칙이 반대 결론이면 unknown 이다.
  3. 쉬는 날이면 휴무 검사는 fail 이고, 그 장소의 입장 · 체류 검사는 not_applicable 이다.

입장
  4. 입장마감(last_admission) 뒤에 도착하면 fail 이다. 입장마감 정각 도착은 last_admission_rule 이 '…까지 입장'
     인 곳만 pass 이고, 그 문구가 없으면 unknown 이다. 개장 전에 도착하면 개장 시각에 들어간다.
  5. 회차 입장인 날(admission.type 이 timed_entry 이고 방문 요일이 timed_weekdays)은 도착 뒤 첫 한국어 회차
     (entry_times_ko 에서 방문 월이 속한 구간)에 들어간다. 마지막 한국어 회차 뒤에 도착하면 fail 이다. 그보다 늦은
     외국어 회차가 있으면 fail 이 아니다. 마지막 회차 정각 도착은 unknown 이다.

체류
  5-1. 장소의 outside_only 가 true 면 들어가지 않고 밖에서만 머문다고 한 것이다. 그 장소의 휴무 · 입장 · 체류는
     not_applicable 이고, 이동은 다른 장소처럼 본다(그 장소에 닿아야 하는 시각은 말한 시각 그대로다).
  6. 입장이 fail 이거나 not_applicable 이면 체류는 not_applicable 이다. 입장이 unknown 이면 들어갈 수도 있으니
     체류도 unknown 이다. 체류시간을 사용자가 말하지 않았으면 unknown 이다.
     말했으면 입장 시각에 체류를 더한 시각이 폐장보다 늦을 때 fail 이다. 회차 입장인 날은 해설 시간
     (dwell_fixed_minutes)보다 긴 체류가 unknown 이다.

이동
  7. 앞 장소를 나서는 시각은 입장 시각에 사용자가 말한 체류를 더한 것이다. 체류를 모르면 0분으로 본다.
     다음 장소에 닿아야 하는 시각은 그 장소에 실제로 들어가는 시각(개장 · 회차)이다.
  8. 이동 0분으로도 늦을 때만 fail 이다. 이동시간으로 늦으면 unknown, 닿지만 권장 여유
     (policy.buffer_minutes.transit_leg)를 못 채우면 unknown, 여유까지 채우면 pass 다. 앞 장소의 체류를 사용자가
     말하지 않았으면 바로 나와도 늦을 때만 늦다고 보고, 그 밖에는 unknown 이다.
  9. 대중교통으로 pass 가 안 나오고 구간에 택시 대안(alternatives)이 있으면 택시 시간으로 같은 규칙을 적용한다.
     일정의 avoid_taxi 가 true 면(사용자가 택시를 안 탄다고 했다) 택시 대안을 보지 않는다.
  10. 휴무일인 곳이 끼어도 이동은 다른 이동처럼 본다 — 휴무일인 곳이라도 일부러 갈 수 있고, 휴무는 휴무일 검사가 알린다.
      구역(area)이 끼거나 구간 이동시간(legs)이 없으면
      unknown 이다. 마지막 장소 뒤에는 이동이 없다. 구간 이동시간은 사실 데이터의 legs 와 [이 일정의 테스트용 이동시간]에서 '출발|도착' 으로 찾는다.
      '번호:출발|도착' 키(번호는 0부터 센 출발 장소 순번)는 그 구간에만 쓰고, depart_at 이 그 구간을 나서는 시각과
      같을 때만 쓴다. 같은 장소 사이라도 출발 시각이 다르면 이동시간이 다르다.

시각이 빠진 장소
  10-1. 장소에 start 가 없으면(들어갈 시각을 말하지 않았다) 앞 장소를 나서는 시각에 그 구간 이동시간과 권장 여유를
      더한 시각을 가장 이른 입장 시각으로 본다. 앞 장소가 없으면 그날 문 여는 시각이다. 그 시각에 들어간다고 보고
      휴무 · 입장 · 체류 · 앞뒤 이동이 모두 pass 면 모두 pass 다. 아니면 앞 장소를 나서는 바로 그 시각(이동 0분)에
      들어간다고 보고 fail 이 나오는 입장 · 체류 · 뒤 이동만 fail 이고, 나머지는 unknown 이다. 쉬는 날이면 규칙 3 이다.
      다음이면 계산하지 않고 그 장소의 입장 · 체류와 앞뒤 이동을 unknown 으로 둔다 — 체류시간을 말하지 않았다, 앞이나
      뒤 장소도 start 가 없다, 앞 장소의 start 가 inferred 이거나 체류를 말하지 않았다, 뒤 장소의 start 가 inferred
      다, 구역이거나 운영시간이 없는 장소 · 회차 입장인 날 · outside_only 다, 앞 장소와 뒤 장소 시각 사이에 필수
      일정이 있다, 쓸 수 있는 이동시간이 없거나 최소 이동시간(is_lower_bound)뿐이다.

필수 일정(hard_constraints)
  11. 마지막 장소에서 필수 일정 장소로 가는 이동으로 보고, 그 구간의 INSUFFICIENT_TRAVEL_TIME 검사로 적는다.
      출발 시각(열차 · 항공편)은 그 시각에 닿아도 놓친 것이다. 권장 여유는 buffer_rule 이 가리키는 policy 값이다.
      이동 0분으로도 놓치면 fail, 이동시간으로 놓치거나 여유가 모자라면 unknown 이다. buffer_rule 의 값이 policy 에
      없으면(항공편) pass 를 내지 않는다. 필수 일정 시각보다 늦게 시작하는 장소가 있으면 하루 중간의 일정이라
      unknown 이다. 하루 중간의 필수 일정이 두 장소 사이 시각이면 그 두 장소 사이 이동도 unknown 이다 — 실제 동선은
      필수 일정을 거친다. 필수 일정에 시각이 없으면 unknown 이고, time_source 가 inferred 인 시각에 기대는 결과는
      pass 도 fail 도 확정하지 않는다."""

SYSTEM_LLM_FACTS = f"""너는 여행 일정의 실행 가능성을 검증한다.

{STATES}

{GUIDANCE}

사실 데이터(facts)와 정책(policy)이 주어진다. **주어진 값만 쓴다.**
facts 에 없는 운영시간·휴무일·이동시간을 네 지식으로 메우지 않는다 — 그 검사는 unknown 이다.

근거에는 적용 기간(valid_until)이 있다. 방문일이 그 기간을 벗어나면 쓸 수 없는 근거다.
이동시간에 is_lower_bound 가 참이면 도보·환승·대기가 빠진 하한선이다.
이 하한선은 지하철로 갈 때의 하한선이다. 하한선으로 도착한다고 pass 를 낼 수 없고,
하한선으로도 늦어도 다른 수단으로는 갈 수 있으니 fail 이 아니다.
체류시간이 system_default 면 사용자가 말하지 않은 값이다.
시각의 start_source 가 inferred 면 사용자가 정확히 말하지 않아 추측한 시각이다(예: 앞에 기댈 시각 없이 "4시" 라고만 했다).
그 시각에 기대는 검사는 pass 도 fail 도 확정하지 않는다.
장소의 scope 가 area 면 구역만 정해진 일정이라 확인할 시설이 없다.

{RULES}"""


def itinerary_for(case: dict) -> dict:
    return {"date": case["date"], "stops": case["stops"],
            "hard_constraints": case.get("hard_constraints", []), "avoid_taxi": case.get("avoid_taxi", False)}


def same_as_run(case: dict, case_f: dict) -> bool:
    """itinerary_for 가 빼는 날짜 출처 · 말한 요일이 판정을 바꾸지 않는지 본다."""
    a, b = judge(case, case_f, policy), judge(itinerary_for(case), case_f, policy)
    return a["summary"] == b["summary"] and [c["status"] for c in a["checks"]] == [c["status"] for c in b["checks"]]


def facts_for_llm(f: dict) -> dict:
    """code 방식과 같은 사실 데이터 전체다. 입력이 같아야 판정 방식 차이만 남는다."""
    return {k: v for k, v in f.items() if k != "snapshot_history"}


SYSTEMS = {"llm_naive": SYSTEM_LLM_NAIVE, "llm_only": SYSTEM_LLM_ONLY, "llm_with_facts": SYSTEM_LLM_FACTS}


def llm_requests(case: dict) -> dict[str, dict]:
    """방식별 llm.ask 인자. 이 값으로 요청 지문을 만들고 그대로 부른다."""
    raw = f"오늘은 {REFERENCE_DATE} 이다.\n\n[일정]\n{case['raw']}"
    # 문제마다 같은 사실 데이터와 정책은 앞에 두고 프롬프트 캐시에 올려 값을 줄인다.
    # 문제마다 다른 테스트용 이동시간은 일정과 함께 뒤에 둔다.
    shared = (f"[사실 데이터]\n{json.dumps(facts_for_llm(facts), ensure_ascii=False, indent=1)}\n\n"
              f"[정책]\n{json.dumps(policy, ensure_ascii=False, indent=1)}")
    own = f"[구조화 일정]\n{json.dumps(itinerary_for(case), ensure_ascii=False, indent=1)}"
    if case.get("legs"):
        own += f"\n\n[이 일정의 테스트용 이동시간]\n{json.dumps(case['legs'], ensure_ascii=False, indent=1)}"
    with_facts = [{"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": own}]
    content = {"llm_naive": raw, "llm_only": raw, "llm_with_facts": with_facts}
    return {arm: {"model": MODEL, "effort": EFFORT, "system": SYSTEMS[arm], "content": content[arm],
                  "schema": Judgment} for arm in ARMS if arm in SYSTEMS}


def as_answer(got) -> tuple[dict, dict]:
    """거부와 형식 오류도 답으로 바꾼다. 지표는 따로 세고 사용량은 비용에 넣는다."""
    if isinstance(got, Refusal):
        return {"verdict": "refused", "checks": [], "message": "",
                "stop_details": {"category": got.usage["refusal_category"],
                                 "explanation": got.usage["refusal_explanation"]}}, got.usage
    if isinstance(got, InvalidOutput):
        return {"verdict": "invalid", "checks": [], "message": str(got)}, got.usage
    j, usage = got
    return j.model_dump(), usage


def ask_fresh(req: dict) -> tuple[dict, dict]:
    """--dry-run 의 가짜 응답만 쓴다. 실제 호출은 call_all 이 모아서 한다."""
    try:
        return as_answer(ask(client, **req))
    except (Refusal, InvalidOutput) as e:
        return as_answer(e)


def call_all(todo: dict[str, tuple[str, dict]], keep=None) -> tuple[dict, list]:
    """첫 요청만 바로, 나머지는 배치로 불러 값을 절반으로 줄인다."""
    # 첫 요청이 캐시를 데우므로 앞부분이 가장 긴 llm_with_facts 를 맨 앞에 둔다.
    order = sorted(todo, key=lambda name: todo[name][0] != "llm_with_facts")
    out, missed = {}, []
    if DRY:  # StubClient 에는 배치가 없다
        for name in order:
            out[name] = ask_fresh(todo[name][1])
            if keep:
                keep(name, *out[name])
        return out, missed

    def paid(name, ans):
        # 받는 즉시 세고 넘긴다. 뒤에서 배치가 실패해도 그때까지 나간 돈을 찍는다.
        arm = todo[name][0]
        if isinstance(ans, BatchMiss):
            missed.append(f"{ARM_NAMES[arm]} — {ans}")
            return
        out[name] = as_answer(ans)
        cost = meter.claude(ARM_NAMES[arm], out[name][1])
        print(f"    [비용] {ARM_NAMES[arm]} {'배치' if out[name][1].get('batch') else '바로'} "
              f"{'단가 모름' if cost is None else f'${cost:.4f}'} · 이번 실행 누적 ${meter.claude_usd:.4f}")
        if keep:
            keep(name, *out[name])

    try:
        ask_many(client, {name: todo[name][1] for name in order}, on_answer=paid)
    except APIError as e:
        raise SystemExit(f"\n  ❌ API 호출 실패: {type(e).__name__} {e}\n{meter.summary()}") from e
    if missed:
        print(f"\n  ❌ 배치에서 {len(missed)}건의 답을 받지 못했다(청구되지 않는다): " + " / ".join(missed[:3]))
    return out, missed


def fetch_new() -> None:
    """캐시에 없는 요청만 불러 바로 캐시에 넣는다. 못 받은 것이 있으면 기록 전에 멈춘다."""
    todo = {}
    for _, _, reqs, keys in plan:
        for arm, req in reqs.items():
            if keys[arm] not in stored:
                todo.setdefault(keys[arm], (arm, req))
    if not todo:
        return

    def keep(key, result, usage):
        arm = todo[key][0]
        fetched[key] = ({"label": arm, "run": RUN_ID, "result": result, "usage": usage} if DRY  # 가짜 응답은 캐시에 넣지 않는다
                        else cache.put(key, run=RUN_ID, label=arm, result=result, usage=usage))

    _, missed = call_all(todo, keep)
    if missed:
        raise SystemExit("  여기까지 받은 응답은 응답 캐시에 남아 있다. 다시 돌리면 남은 것만 부른다.\n"
                         f"{meter.summary()}")


def answer(key: str) -> dict:
    return stored[key] if key in stored else fetched[key]


# 지표
def metrics(scored: list[dict]) -> dict:
    """모두 보류로 내면 틀린 확신이 0 이 되므로 답을 준 비율도 같이 본다."""
    n = len(scored)
    if not n:
        return {"n": 0}
    got = lambda r: r["verdict"]
    want = lambda r: r["expect"]

    flagged = [r for r in scored if got(r) == "infeasible"]
    actual_bad = [r for r in scored if want(r) == "infeasible"]
    actual_ok = [r for r in scored if want(r) == "feasible"]

    tp = sum(1 for r in flagged if want(r) == "infeasible")
    expected = sum(r["checks_expected"] for r in scored)
    hc_expected = sum(r["hard_constraints_expected"] for r in scored)
    return {
        "n": n,
        "verdict_accuracy": round(sum(1 for r in scored if got(r) == want(r)) / n, 3),
        "check_accuracy": round(1 - sum(len(r["checks_wrong"]) for r in scored) / expected, 3) if expected else None,
        "hard_constraint_accuracy": round(1 - sum(len(r["hard_constraints_wrong"]) for r in scored) / hc_expected, 3)
        if hc_expected else None,
        "checks_missing": sum(1 for r in scored for w in r["checks_wrong"] if w["got"] is None),
        "verdict_inconsistent": sum(1 for r in scored if r["consistent"] is False),
        "precision_on_infeasible": round(tp / len(flagged), 3) if flagged else None,
        "recall_on_infeasible": round(tp / len(actual_bad), 3) if actual_bad else None,
        "false_alarm_on_feasible": sum(1 for r in actual_ok if got(r) == "infeasible"),
        "false_pass_on_infeasible": sum(1 for r in actual_bad if got(r) == "feasible"),
        "leaked_undetermined_as_feasible": sum(
            1 for r in scored if want(r) == "undetermined" and got(r) == "feasible"),
        "undetermined_rate": round(sum(1 for r in scored if got(r) == "undetermined") / n, 3),
        "decisive_rate": round(sum(1 for r in scored if got(r) in ("feasible", "infeasible")) / n, 3),
        "refused": sum(1 for r in scored if got(r) == "refused"),
        "invalid_output": sum(1 for r in scored if got(r) == "invalid"),
        "counts": {"actual_infeasible": len(actual_bad), "actual_feasible": len(actual_ok),
                   "actual_undetermined": n - len(actual_bad) - len(actual_ok)},
    }


def repeat_runs(n: int) -> None:
    """같은 요청을 n 번 새로 불러 답이 흔들리는지 잰다. 공식 기록의 응답은 건드리지 않는다."""
    rows = []
    todo = {f"{i}|{arm}|{r}": (arm, req) for i, (_, _, reqs, _) in enumerate(plan)
            for arm, req in reqs.items() for r in range(n)}
    got, missed = call_all(todo)
    if missed:
        raise SystemExit(f"  반복 기록을 남기지 않는다. 다시 돌려 주세요.\n{meter.summary()}")
    for i, (case, _, reqs, _) in enumerate(plan):
        for arm in reqs:
            answers = []
            for r in range(n):
                result, _ = got[f"{i}|{arm}|{r}"]
                g = grade_answer(case, result)
                answers.append({"verdict": result["verdict"], "checks_wrong": g["checks_wrong"],
                                "hard_constraints_wrong": g["hard_constraints_wrong"],
                                "consistent": g["consistent"], "result": result})
            verdicts = Counter(a["verdict"] for a in answers)
            rows.append({"id": case["id"], "arm": arm, "expect": case["expect"], "verdicts": dict(verdicts),
                         "answers": answers})
            print(f"  {pad(case['id'], 26)}{ARM_NAMES[arm]}  정답 {ANSWER_NAMES[case['expect']]} → "
                  + " · ".join(f"{ANSWER_NAMES.get(v, v)} {k}번" for v, k in verdicts.most_common())
                  + f" · 틀린 검사 수 {'/'.join(str(len(a['checks_wrong'])) for a in answers)}"
                  + f" · 검사와 판정이 어긋남 {sum(a['consistent'] is False for a in answers)}번")
    name = record_name("last-compare-stability-dryrun.json" if DRY else "last-compare-stability.json", SUITE_FILE)
    out = {"_역할": "비교 실험 반복 기록. 같은 요청을 여러 번 새로 불러 답이 흔들리는지 잰다. "
                    "공식 기록(last-compare.json)과 따로 둔다. compare.py --repeat 이 덮어쓴다.",
           "run_id": RUN_ID.replace("cmp-", "cmp-repeat-"), "ran_at": datetime.now(timezone.utc).isoformat(),
           "facts_snapshot": facts["snapshot_id"], "model": MODEL, "effort": EFFORT, "repeat": n,
           "conditions": CONDITIONS, "spent_usd": round(meter.claude_usd, 4), "rows": rows}
    (HERE / name).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  기록: core/{name}  ({out['run_id']})")
    print(meter.summary())


# 실행
class StubClient:
    """--dry-run 용 가짜 클라이언트. 키 없이 LLM 방식의 코드 경로를 끝까지 돌려 본다."""

    class _Usage:
        input_tokens = output_tokens = 0
        cache_read_input_tokens = cache_creation_input_tokens = 0

    class _Text:
        type = "text"
        text = json.dumps({"checks": [], "verdict": "undetermined", "message": "dry-run"})

    class _Messages:
        calls = 0

        def create(self, **kw):
            content = kw["messages"][0]["content"]
            # 앞부분을 프롬프트 캐시에 올린 요청은 본문이 조각 목록이다.
            text = content if isinstance(content, str) else "".join(block["text"] for block in content)
            assert text.strip(), "payload 가 비어 있다"
            assert kw["system"][0]["text"].strip(), "system 프롬프트가 비어 있다"
            assert kw["output_config"]["format"]["schema"], "출력 스키마가 비어 있다"
            StubClient._Messages.calls += 1
            # 이 본문도 Judgment 로 검증하므로 스키마가 어긋나면 dry-run 에서 걸린다.
            return type("R", (), {"usage": StubClient._Usage(), "stop_reason": "end_turn",
                                  "stop_details": None, "content": [StubClient._Text()]})()

    def __init__(self):
        self.messages = self._Messages()


DRY = "--dry-run" in args
need_llm = any(a.startswith("llm") for a in ARMS)
RUN_ID = f"cmp-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
REFERENCE_DATE = reference_date(suite, flag("--reference-date", None))

client = cache = Judgment = JUDGMENT_SCHEMA = None
stored = {}
fetched = {}
meter = Spend()
if need_llm:
    # 코드 방식만 돌릴 때는 anthropic 없이도 돌아간다.
    from anthropic import APIError, transform_schema  # noqa: E402
    from llm import BatchMiss, InvalidOutput, Refusal, ask, ask_many  # noqa: E402
    from response_cache import ResponseCache, confirm_spend, cost_summary, request_key  # noqa: E402
    Judgment = build_models()
    JUDGMENT_SCHEMA = fingerprint(json.dumps(transform_schema(Judgment), ensure_ascii=False, sort_keys=True))

plan = []
for case in cases:
    case_f = case_facts(case, facts)
    if not same_as_run(case, case_f):
        raise SystemExit(f"  ❌ {case['id']} — 비교 실험이 넘기지 않는 날짜 출처 · 말한 요일이 판정을 바꾼다. "
                         "itinerary_for 에 두 칸을 넣고 LLM 응답을 다시 받아야 한다(돈이 든다)")
    reqs = llm_requests(case) if need_llm else {}
    plan.append((case, case_f, reqs, {arm: request_key(**req) for arm, req in reqs.items()}))

new_calls = 0
if need_llm and DRY:
    client = StubClient()
    print("  [--dry-run] API 를 부르지 않는다. 배선만 확인한다. 응답 캐시도 쓰지 않는다.\n")
elif need_llm:
    # 부르기 전에 응답 캐시와 맞춰 보고, 새 호출이 있으면 건수와 예상 비용을 보여 주고 묻는다.
    cache = ResponseCache()
    if "--fresh" not in args and not REPEAT:
        stored = {key: hit for _, _, _, keys in plan for key in keys.values() if (hit := cache.get(key))}
    counts = {arm: (REPEAT or 1) * sum(1 for _, _, _, keys in plan if arm in keys and keys[arm] not in stored)
              for arm in SYSTEMS}
    new = {ARM_NAMES[arm]: n for arm, n in counts.items()}
    new_calls = sum(new.values())
    confirm_spend(new, len(stored),
                  {ARM_NAMES[arm]: cache.estimate_usd(MODEL, arm, n, batch=True) for arm, n in counts.items()})
    if new_calls:
        import os
        from getpass import getpass

        import anthropic

        key = os.environ.get("ANTHROPIC_API_KEY") or getpass("Anthropic API 키 입력: ").strip()
        if not key:
            raise SystemExit("키가 비어 있다.")
        client = anthropic.Anthropic(api_key=key)

# RULES 는 llm_with_facts 프롬프트 지문에 들어 있다.
CONDITIONS = run_meta(SUITE_FILE, reference_date=REFERENCE_DATE, libraries=library_versions(), prompts={
    "llm_naive": fingerprint(SYSTEM_LLM_NAIVE), "llm_only": fingerprint(SYSTEM_LLM_ONLY),
    "llm_with_facts": fingerprint(SYSTEM_LLM_FACTS), "judgment_schema": JUDGMENT_SCHEMA})
# 공식 기록은 전체 실행만 쓴다. 일부를 얹으면 다른 코드로 낸 판정이 섞인다.
FULL_RUN = set(ARMS) == set(ALL_ARMS) and not only
OUT_NAME = record_name("last-compare-dryrun.json" if DRY else
                       "last-compare.json" if FULL_RUN else "last-compare-subset.json", SUITE_FILE)

if REPEAT:
    repeat_runs(REPEAT)
    raise SystemExit

if need_llm:
    fetch_new()
rows, used = [], {}
for case, case_f, reqs, keys in plan:
    rec = {"id": case["id"], "set": case["set"], "expect": case["expect"],
           "label_current": label_is_current(case), "arms": {}}

    if "code" in ARMS:
        out = judge(itinerary_for(case), case_f, policy)
        rec["arms"]["code"] = {"verdict": out["summary"]["verdict"],
                               "checks": [{"type": c["type"], "target": c["target"],
                                           "status": c["status"]} for c in out["checks"]],
                               "hard_constraints": [{"id": h["id"], "status": h["policy"]["status"]}
                                                    for h in out["hard_constraints"]]}

    for arm in reqs:
        hit = answer(keys[arm])
        rec["arms"][arm] = hit["result"]
        used.setdefault(arm, []).append(hit)

    rec["graded"] = {arm: grade_answer(case, ans) for arm, ans in rec["arms"].items()}
    rows.append(rec)
    print(f"  {pad(case['id'], 26)}" + "  ".join(
        f"{ARM_NAMES.get(a, a)} {ANSWER_NAMES.get(rec['arms'][a]['verdict'], rec['arms'][a]['verdict'])}"
        for a in ARMS if a in rec["arms"]))

# 채점
scorable = [r for r in rows if r["label_current"]]
stale = [r for r in rows if not r["label_current"]]

print(f"\n{LINE}\n  지표   데이터 버전 {facts['snapshot_id']}\n{LINE}")
print(f"  채점 대상 {len(scorable)}건 / 전체 {len(rows)}건"
      + (f"  (정답 재확인 필요 {len(stale)}건 제외)" if stale else ""))

report = {}
for arm in ARMS:
    sub = [{"expect": r["expect"], "verdict": r["arms"][arm]["verdict"], **r["graded"][arm]}
           for r in scorable if arm in r["arms"]]
    report[arm] = metrics(sub)

if scorable:
    LABELS = [
        ("verdict_accuracy", "전체 정확도"),
        ("check_accuracy", "검사별 정확도 — 기대 검사 중 상태가 맞은 비율"),
        ("hard_constraint_accuracy", "필수 일정 정확도 — 기대 필수 일정 중 상태가 맞은 비율"),
        ("checks_missing", "기대 검사를 답하지 않음 (개)"),
        ("verdict_inconsistent", "검사와 전체 판정이 어긋남 (건)"),
        ("precision_on_infeasible", "정밀도 — 불가능이라고 한 것 중 맞은 비율"),
        ("recall_on_infeasible", "재현율 — 정답이 불가능인 것 중 찾은 비율"),
        ("false_alarm_on_feasible", "가능인데 불가능 (건)"),
        ("false_pass_on_infeasible", "불가능인데 가능 (건)"),
        ("leaked_undetermined_as_feasible", "보류인데 가능 (건)"),
        ("undetermined_rate", "보류 비율"),
        ("decisive_rate", "답을 준 비율"),
        ("refused", "모델 거부 (건)"),
        ("invalid_output", "형식 오류 응답 (건)"),
    ]
    w = max(width(l) for _, l in LABELS) + 2
    print(f"\n  {pad('지표', w)}" + "".join(pad(ARM_NAMES.get(a, a), 28) for a in ARMS))
    print("  " + "-" * (w + 28 * len(ARMS)))
    for key, label in LABELS:
        cells = []
        for a in ARMS:
            v = report[a].get(key)
            cells.append(pad('-' if v is None else v, 28))
        print(f"  {pad(label, w)}" + "".join(cells))

    c = report[ARMS[0]]['counts']
    print(f"\n  정답 분포: 불가능 {c['actual_infeasible']} · 가능 {c['actual_feasible']} · 보류 {c['actual_undetermined']}")
    for a in ARMS:
        odd = [r["id"].split("-")[0] for r in scorable if a in r["graded"] and r["graded"][a]["consistent"] is False]
        if odd:
            print(f"  검사와 전체 판정이 어긋난 답 — {ARM_NAMES.get(a, a)}: {' · '.join(odd)}")
    print(f"\n  ⚠ 표본 {len(scorable)}건이다. 정밀도·재현율은 신뢰구간이 넓다.")
    print("    '불가능인데 가능' 과 '보류인데 가능' 은 0이냐 아니냐가 의미 있고,")
    print("    보류 비율과 답을 준 비율은 비율로 읽을 수 있다.")

if stale:
    print(f"\n  정답 재확인 필요 {len(stale)}건 — 출력은 저장했다. "
          f"워크시트를 채우면 재실행 없이 채점된다.")

# 응답을 받을 때 든 비용이다. 이번 실행에서 나간 돈은 spent_usd 에 따로 적는다.
cost_report = {}
for arm, hits in used.items():
    cost_report[arm] = c = cost_summary(MODEL, hits, cache.totals if cache else {})
    money = "비용 모름" if c["total_usd"] is None else f"${c['total_usd']:.4f}  건당 ${c['per_case_usd']:.4f}"
    lat = "지연 모름" if c["latency_median_ms"] is None else f"지연 중앙값 {c['latency_median_ms']}ms"
    print(f"\n  {ARM_NAMES.get(arm, arm)}: {c['n']}건  {money}  {lat}")

hits = [h for arm_hits in used.values() for h in arm_hits]
responses = {"new": sum(h["run"] == RUN_ID for h in hits), "reused": sum(h["run"] != RUN_ID for h in hits),
             "received_in": sorted({h["run"] for h in hits}), "spent_usd": round(meter.claude_usd, 4)}
if need_llm and not DRY:
    print(f"\n  (위 비용은 응답을 받을 때 든 값이다. 받아 둔 응답 {responses['reused']}건은 이번에 돈이 나가지 않았다)\n")
    print(meter.summary())
elif DRY:
    print("\n  [--dry-run] 가짜 응답으로 돌렸다 — 돈이 나가지 않았다.")

out = {"_역할": "비교 실험 기록. arm 별 판정과 지표 5종, 비용·지연을 남긴다. "
                "compare.py 가 덮어쓴다.",
       "run_id": RUN_ID, "ran_at": datetime.now(timezone.utc).isoformat(),
       "facts_snapshot": facts["snapshot_id"], "model": MODEL, "effort": EFFORT,
       "pricing_per_mtok": PRICES.get(MODEL), "conditions": CONDITIONS,
       "arms": ARMS, "scored": len(scorable), "total": len(rows),
       "responses": responses,
       "metrics": report, "cost": cost_report, "rows": rows}
(HERE / OUT_NAME).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  기록: core/{OUT_NAME}  ({out['run_id']})")
print(describe(CONDITIONS))
