#!/usr/bin/env python3
"""
비교 실험 — 판정 방식 4종을 같은 케이스로 돌려 비교한다.

    python core/compare.py                  4종 전부        → last-compare.json
    python core/compare.py --fresh          받아 둔 응답을 쓰지 않고 전부 새로 받는다 (돈이 든다)
    python core/compare.py --dry-run        API 없이 배선만 확인 → last-compare-dryrun.json
    python core/compare.py --arms code      코드만 (API 키 불필요) → last-compare-subset.json
    python core/compare.py --case T05       한 건만         → last-compare-subset.json
    python core/compare.py --model claude-sonnet-5-5
    python core/compare.py --reference-date 2026-10-01   기준 날짜를 바꿔서 (기본은 tests.json)

arm 을 나눈 이유는 **개선이 어디서 왔는지 구분**하기 위해서다.

    arm 0  llm_naive       자연어 일정만. 상태 정의만 주고 지시는 주지 않는다
    arm 1  llm_only        같은 입력 + "근거 없으면 unknown 으로 둬라" 지시
    arm 2  llm_with_facts  구조화 일정 + 사실 + 정책. 판정은 LLM
    arm 3  code            같은 입력.                  판정은 verdict.py

    arm 0 → 1   지시 효과      프롬프트로 보류를 유도하면 달라지는가
    arm 1 → 2   정보 제공 효과
    arm 2 → 3   판정 방식 효과  입력이 같으므로 그 차이만 남는다

arm 0 이 따로 있는 이유: 1차 실험에서 arm1 이 15건 전부 undetermined 였는데,
그건 "근거 없이 추측하지 말라"고 **지시했기 때문**이다. 지시를 따른 결과를
발견으로 읽으면 안 된다. 근거가 없을 때 실제로 무엇을 하는지는 arm0 이 본다.

채점은 라벨이 현재 데이터 스냅샷 기준으로 검토된 케이스에만 한다.
미검토 라벨로 점수를 내면 세 arm 전부 틀린 점수가 나온다.

LLM arm 은 돈이 든다. 모델이 받는 것이 같은 요청은 응답 캐시(response_cache.py)에 받아 둔 응답을 쓰고,
새로 부를 것이 있으면 몇 건이고 얼마쯤인지 보여 준 뒤 y 를 받아야 부른다.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))
from labels import case_facts, stale_reason  # noqa: E402
from model_info import DEFAULT_MODEL, PRICES, effort_for  # noqa: E402
from report import ARM_NAMES, VERDICT_NAMES, pad, width  # noqa: E402
from runmeta import describe, fingerprint, library_versions, reference_date, run_meta  # noqa: E402
from verdict import judge  # noqa: E402

# 비교 방식의 답 — 판정 세 가지 밖에 거부 · 형식 오류가 있다
ANSWER_NAMES = {**VERDICT_NAMES, "refused": "거부", "invalid": "형식 오류"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
facts, policy, suite = load("facts.json"), load("policy.json"), load("tests.json")

args = sys.argv[1:]
flag = lambda name, d: next(
    (args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), d)
only = flag("--case", None)
MODEL = flag("--model", DEFAULT_MODEL)
EFFORT = effort_for(MODEL, flag("--effort", "low"))
ALL_ARMS = ["llm_naive", "llm_only", "llm_with_facts", "code"]
ARMS = flag("--arms", ",".join(ALL_ARMS)).split(",")

LINE = "═" * 78
CHECK_TYPES = ["CLOSED_DAY", "ADMISSION_NOT_POSSIBLE",
               "DWELL_NOT_COMPLETABLE", "INSUFFICIENT_TRAVEL_TIME"]

cases = [c for c in suite["cases"] if not only or c["id"].startswith(only)]


# ── 라벨 유효성 (run.py 와 같은 규칙) ────────────────────────────────
def label_is_current(case: dict) -> bool:
    return stale_reason(case, suite, facts) is None


# ── LLM arm 의 출력 스키마 — 코드 출력과 같은 모양이어야 비교가 된다 ──
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


# ── 프롬프트 ────────────────────────────────────────────────────────
# ⭐ 비교 실험을 설계할 때 제일 쉽게 틀리는 지점이다.
#    1차 실험에서 arm1 이 15건 전부 undetermined 였다. "LLM 도 보류를 잘 하네"
#    라고 읽을 뻔했는데, 사실은 내가 프롬프트에 "추측하지 말고 unknown 으로
#    둬라" 고 **지시**했기 때문이었다. 지시를 따른 결과는 발견이 아니다.
#
# 정의와 지시를 분리해 둔다.
#
# STATES 는 **라벨의 정의**다. 이게 없으면 모델이 unknown 이라는 선택지가
# 있다는 것조차 모르므로, 네 arm 전부에 준다.
#
# GUIDANCE 는 **우리 정책의 지시**다. "근거가 없으면 unknown 으로 둬라" 는
# 지시이고, 이것을 준 arm 이 unknown 을 많이 내는 것은 발견이 아니라 순응이다.
# 그래서 llm_naive 에는 주지 않는다.

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

# arm 0 — 지시 없이 검토만 요청한다. 근거가 없을 때 무엇을 하는지 본다.
SYSTEM_LLM_NAIVE = f"""너는 여행 일정의 실행 가능성을 검증한다.

{STATES}"""

SYSTEM_LLM_ONLY = f"""너는 여행 일정의 실행 가능성을 검증한다.

{STATES}

{GUIDANCE}

**사실 데이터는 주어지지 않는다.** 운영시간·휴무일·이동시간을 확보하지 못한 상태다.
근거 없이 추측해서 pass 나 fail 을 내지 말고, 확보하지 못한 근거가 필요한 검사는 unknown 으로 둔다."""

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
장소의 scope 가 area 면 구역만 정해진 일정이라 확인할 시설이 없다."""


def itinerary_for(case: dict) -> dict:
    return {"date": case["date"], "stops": case["stops"],
            "hard_constraints": case.get("hard_constraints", [])}


def facts_for_llm(f: dict) -> dict:
    """
    arm 2 와 arm 3 은 **완전히 같은 사실 데이터**를 받아야 한다.
    그래야 둘의 차이가 판정 방식 차이만 남는다 — 그것이 이 실험의 목적이다.

    케이스별로 부분집합을 주면 arm 2 만 정보가 줄어들어, 차이가 판정 방식 때문인지
    정보량 때문인지 다시 구분할 수 없게 된다. 그래서 전체를 준다. 케이스에 테스트용 이동시간이
    있으면 그것을 얹은 사실 데이터(labels.case_facts)를 arm 2 와 arm 3 에 똑같이 준다.
    """
    # ⭐ 실제로 이 버그를 한 번 냈다. arm2 에는 케이스별 부분집합을, arm3 에는
    #    전체 facts 를 주고 있었다. 그러면 arm2 가 못 맞힌 게 판정 능력 때문인지
    #    정보가 적어서인지 알 수 없다. 비교 실험은 **바꾸는 변수 하나만** 달라야 한다.
    return {k: v for k, v in f.items() if k != "snapshot_history"}


SYSTEMS = {"llm_naive": SYSTEM_LLM_NAIVE, "llm_only": SYSTEM_LLM_ONLY, "llm_with_facts": SYSTEM_LLM_FACTS}


def llm_requests(case: dict, case_f: dict) -> dict[str, dict]:
    """LLM 방식마다 llm.ask 가 받는 인자. 이것으로 요청 지문을 만들고 그대로 부른다."""
    # arm0 과 arm1 은 입력이 완전히 같다. 시스템 프롬프트만 다르다.
    raw = f"오늘은 {REFERENCE_DATE} 이다.\n\n[일정]\n{case['raw']}"
    with_facts = (f"[구조화 일정]\n{json.dumps(itinerary_for(case), ensure_ascii=False, indent=1)}\n\n"
                  f"[사실 데이터]\n{json.dumps(facts_for_llm(case_f), ensure_ascii=False, indent=1)}\n\n"
                  f"[정책]\n{json.dumps(policy, ensure_ascii=False, indent=1)}")
    content = {"llm_naive": raw, "llm_only": raw, "llm_with_facts": with_facts}
    return {arm: {"model": MODEL, "effort": EFFORT, "system": SYSTEMS[arm], "content": content[arm],
                  "schema": Judgment} for arm in ARMS if arm in SYSTEMS}


def answer(arm: str, req: dict, key: str) -> dict:
    """LLM 방식 하나의 답. 받아 둔 응답이 있으면 그것을 쓰고, 없으면 불러서 받는 즉시 응답 캐시에 넣는다."""
    if key in stored:
        return stored[key]
    try:
        j, usage = ask(client, **req)
        result = j.model_dump()
    except Refusal as e:
        # 거부는 판정이 아니다. 지표가 따로 세도록 verdict 를 "refused" 로 남긴다
        result, usage = {"verdict": "refused", "checks": [], "message": "",
                         "stop_details": {"category": e.usage["refusal_category"],
                                          "explanation": e.usage["refusal_explanation"]}}, e.usage
    except InvalidOutput as e:
        # 형식이 틀린 응답도 판정이 아니다. 따로 세고, 청구된 사용량은 비용에 넣는다
        result, usage = {"verdict": "invalid", "checks": [], "message": str(e)}, e.usage
    if DRY:  # 가짜 응답은 응답 캐시에 넣지 않는다
        return {"label": arm, "run": RUN_ID, "result": result, "usage": usage}
    return cache.put(key, run=RUN_ID, label=arm, result=result, usage=usage)


def last_cost_per_case() -> dict:
    """지난 공식 기록의 방식별 건당 비용 — 새로 부를 호출의 비용을 어림하는 데 쓴다. 모델이 다르면 견줄 수 없다."""
    path = HERE / "last-compare.json"
    last = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    costs = last.get("cost", {}) if last.get("model") == MODEL else {}
    return {ARM_NAMES[arm]: costs.get(arm, {}).get("per_case_usd") for arm in SYSTEMS}


# ── 지표 ────────────────────────────────────────────────────────────
def metrics(scored: list[dict]) -> dict:
    """
    전체 정확도 하나로 끝내지 않는다. 다섯 갈래로 나눠 본다.
    모든 일정을 미확인으로 처리하면 잘못된 확신은 줄지만 사용자가 얻는 결과도 없어지므로,
    얼마나 맞혔는지와 함께 **얼마나 많은 요청에 유효한 판단을 제공했는지**도 본다.
    """
    # ⭐ 정확도 하나만 보면 안 되는 이유:
    #    모든 일정을 undetermined 로 내는 검증기는 "틀린 확신" 이 0 이다.
    #    누출 0, 오탐 0 — 지표가 완벽해 보이는데 사용자는 아무 답도 못 받는다.
    #    그래서 decisive_rate(유효 판단 제공 비율)를 같이 본다. 이 둘의 트레이드오프가
    #    실제로 드러났다 — 10월 2일 기록에서 llm_with_facts 는 유효 판단 0.6 에 누출 1,
    #    code 는 0.533 에 누출 0 이었다.
    n = len(scored)
    if not n:
        return {"n": 0}
    got = lambda r: r["verdict"]
    want = lambda r: r["expect"]

    flagged = [r for r in scored if got(r) == "infeasible"]
    actual_bad = [r for r in scored if want(r) == "infeasible"]
    actual_ok = [r for r in scored if want(r) == "feasible"]

    tp = sum(1 for r in flagged if want(r) == "infeasible")
    return {
        "n": n,
        "verdict_accuracy": round(sum(1 for r in scored if got(r) == want(r)) / n, 3),
        # 오류로 표시한 것 중 실제 오류 비율
        "precision_on_infeasible": round(tp / len(flagged), 3) if flagged else None,
        # 실제 오류 중 찾아낸 비율
        "recall_on_infeasible": round(tp / len(actual_bad), 3) if actual_bad else None,
        # 정상 일정의 오탐
        "false_alarm_on_feasible": sum(1 for r in actual_ok if got(r) == "infeasible"),
        # 잘못된 일정의 정상 통과 — 가장 치명적
        "false_pass_on_infeasible": sum(1 for r in actual_bad if got(r) == "feasible"),
        # 미확인이어야 하는데 통과로 낸 건수
        "leaked_undetermined_as_feasible": sum(
            1 for r in scored if want(r) == "undetermined" and got(r) == "feasible"),
        # 판정 보류 비율
        "undetermined_rate": round(sum(1 for r in scored if got(r) == "undetermined") / n, 3),
        # 유효한 판단(feasible · infeasible)을 제공한 비율. 거부(refused)는 판단이 아니다
        "decisive_rate": round(sum(1 for r in scored if got(r) in ("feasible", "infeasible")) / n, 3),
        # 모델이 응답을 거부한 건수
        "refused": sum(1 for r in scored if got(r) == "refused"),
        # 형식이 틀린 응답 건수. 보류와 섞지 않는다
        "invalid_output": sum(1 for r in scored if got(r) == "invalid"),
        "counts": {"actual_infeasible": len(actual_bad), "actual_feasible": len(actual_ok),
                   "actual_undetermined": n - len(actual_bad) - len(actual_ok)},
    }


# ── 실행 ────────────────────────────────────────────────────────────
class StubClient:
    """
    --dry-run 용. API 를 부르지 않고 배선만 통과시킨다.

    이게 있는 이유: LLM arm 은 키가 있어야 돌아가서, 키 없이 편집하면
    NameError 같은 것도 발견되지 않는다. 실제로 그렇게 두 번 깨진 파일을 넘겼다.
    키를 넣기 전에 `--dry-run` 으로 세 arm 의 경로를 전부 지나가 볼 수 있다.
    """

    class _Usage:
        input_tokens = output_tokens = 0
        cache_read_input_tokens = cache_creation_input_tokens = 0

    class _Text:
        type = "text"
        text = json.dumps({"checks": [], "verdict": "undetermined", "message": "dry-run"})

    class _Messages:
        calls = 0

        def create(self, **kw):
            assert kw["messages"][0]["content"].strip(), "payload 가 비어 있다"
            assert kw["system"][0]["text"].strip(), "system 프롬프트가 비어 있다"
            assert kw["output_config"]["format"]["schema"], "출력 스키마가 비어 있다"
            StubClient._Messages.calls += 1
            # 응답 본문은 llm.ask 가 Judgment 로 검증한다 — 스키마와 어긋나면 dry-run 에서 걸린다
            return type("R", (), {"usage": StubClient._Usage(), "stop_reason": "end_turn",
                                  "stop_details": None, "content": [StubClient._Text()]})()

    def __init__(self):
        self.messages = self._Messages()


DRY = "--dry-run" in args
need_llm = any(a.startswith("llm") for a in ARMS)
RUN_ID = f"cmp-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
# LLM arm 이 연도 없는 날짜를 푸는 기준. 고정해 둬야 언제 돌려도 같은 입력이 된다.
# 추출 채점과 같은 값을 tests.json 에서 읽는다 (runmeta.reference_date).
REFERENCE_DATE = reference_date(suite, flag("--reference-date", None))

client = cache = Judgment = JUDGMENT_SCHEMA = None
stored = {}  # 요청 지문 → 받아 둔 응답. 이번 실행에서 다시 쓸 것만 담는다
if need_llm:
    # anthropic 이 있어야 읽힌다. 코드 arm 만 돌릴 때는 읽지 않는다
    from anthropic import APIError, transform_schema  # noqa: E402
    from llm import InvalidOutput, Refusal, ask  # noqa: E402
    from response_cache import ResponseCache, confirm_spend, cost_summary, request_key  # noqa: E402
    Judgment = build_models()
    JUDGMENT_SCHEMA = fingerprint(json.dumps(transform_schema(Judgment), ensure_ascii=False, sort_keys=True))

# 문제마다 그 문제의 사실 데이터(테스트용 이동시간이 있으면 얹은 것 — arm 2 와 arm 3 이 같이 쓴다),
# LLM 방식별 요청, 요청 지문을 먼저 만든다
plan = []
for case in cases:
    case_f = case_facts(case, facts)
    reqs = llm_requests(case, case_f) if need_llm else {}
    plan.append((case, case_f, reqs, {arm: request_key(**req) for arm, req in reqs.items()}))

new_calls = 0
if need_llm and DRY:
    client = StubClient()
    print("  [--dry-run] API 를 부르지 않는다. 배선만 확인한다. 응답 캐시도 쓰지 않는다.\n")
elif need_llm:
    # ⭐ API 를 부르기 전에 응답 캐시와 맞춰 본다. 모델이 받는 것이 같은 요청은 받아 둔 응답을 쓰고,
    #    새로 부를 것이 있을 때만 비용을 보여 주고 물은 뒤에 키를 받는다. 받은 응답은 바로 캐시에 들어가므로
    #    도중에 멈춰도 다시 돌리면 남은 것만 부른다.
    cache = ResponseCache()
    if "--fresh" not in args:  # --fresh 면 받아 둔 응답을 쓰지 않고 전부 새로 받아 바꿔 넣는다
        stored = {key: hit for _, _, _, keys in plan for key in keys.values() if (hit := cache.get(key))}
    new = {ARM_NAMES[arm]: sum(1 for _, _, _, keys in plan if arm in keys and keys[arm] not in stored)
           for arm in SYSTEMS}
    new_calls = sum(new.values())
    confirm_spend(new, sum(len(keys) for _, _, _, keys in plan) - new_calls, last_cost_per_case())
    if new_calls:
        import os
        from getpass import getpass

        import anthropic

        key = next((a[6:].strip() for a in args if a.startswith("--key=")), None) \
            or os.environ.get("ANTHROPIC_API_KEY") or getpass("Anthropic API 키 입력: ").strip()
        if not key:
            raise SystemExit("키가 비어 있다.")
        client = anthropic.Anthropic(api_key=key)

CONDITIONS = run_meta(reference_date=REFERENCE_DATE, libraries=library_versions(), prompts={
    "llm_naive": fingerprint(SYSTEM_LLM_NAIVE), "llm_only": fingerprint(SYSTEM_LLM_ONLY),
    "llm_with_facts": fingerprint(SYSTEM_LLM_FACTS), "judgment_schema": JUDGMENT_SCHEMA})
# ⭐ last-compare.json 에는 모든 arm · 모든 케이스를 한 조건에서 돌린 결과만 둔다.
#    일부만 돌린 결과를 이전 기록에 얹었더니 한 기록 안에 다른 라벨 · 다른 코드로 낸
#    판정이 섞였다 — 옛 기대값이 남은 행이 멘토 피드백 3번에서 지적됐다. 일부 실행은
#    따로 적는다.
FULL_RUN = set(ARMS) == set(ALL_ARMS) and not only
OUT_NAME = ("last-compare-dryrun.json" if DRY else
            "last-compare.json" if FULL_RUN else "last-compare-subset.json")

rows, used = [], {}  # used — LLM 방식마다 이번 기록에 쓴 응답
for case, case_f, reqs, keys in plan:
    rec = {"id": case["id"], "set": case["set"], "expect": case["expect"],
           "label_current": label_is_current(case), "arms": {}}

    if "code" in ARMS:
        out = judge(itinerary_for(case), case_f, policy)
        rec["arms"]["code"] = {"verdict": out["summary"]["verdict"],
                               "checks": [{"type": c["type"], "target": c["target"],
                                           "status": c["status"]} for c in out["checks"]]}

    for arm, req in reqs.items():
        try:
            hit = answer(arm, req, keys[arm])
        except APIError as e:
            # 잔액 부족 · 네트워크 오류. 남은 문제를 계속 부르지 않고 멈춘다 — 공식 기록은 건드리지 않는다
            raise SystemExit(f"\n  ❌ API 호출 실패: {type(e).__name__} {e}\n"
                             "  여기까지 받은 응답은 응답 캐시에 남아 있다. 다시 돌리면 남은 것만 부른다.") from e
        rec["arms"][arm] = hit["result"]
        used.setdefault(arm, []).append(hit)

    rows.append(rec)
    print(f"  {pad(case['id'], 26)}" + "  ".join(
        f"{ARM_NAMES.get(a, a)} {ANSWER_NAMES.get(rec['arms'][a]['verdict'], rec['arms'][a]['verdict'])}"
        for a in ARMS if a in rec["arms"]))

# ── 채점 ────────────────────────────────────────────────────────────
scorable = [r for r in rows if r["label_current"]]
stale = [r for r in rows if not r["label_current"]]

print(f"\n{LINE}\n  지표   데이터 버전 {facts['snapshot_id']}\n{LINE}")
print(f"  채점 대상 {len(scorable)}건 / 전체 {len(rows)}건"
      + (f"  (정답 재확인 필요 {len(stale)}건 제외)" if stale else ""))

report = {}
for arm in ARMS:
    sub = [{"expect": r["expect"], "verdict": r["arms"][arm]["verdict"]}
           for r in scorable if arm in r["arms"]]
    report[arm] = metrics(sub)

if scorable:
    LABELS = [
        ("verdict_accuracy", "전체 정확도"),
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
    print(f"\n  ⚠ 표본 {len(scorable)}건이다. 정밀도·재현율은 신뢰구간이 넓다.")
    print("    '불가능인데 가능' 과 '보류인데 가능' 은 0이냐 아니냐가 의미 있고,")
    print("    보류 비율과 답을 준 비율은 비율로 읽을 수 있다.")

if stale:
    print(f"\n  정답 재확인 필요 {len(stale)}건 — 출력은 저장했다. "
          f"워크시트를 채우면 재실행 없이 채점된다.")

# 비용과 지연은 응답을 받을 때 잰 값이다. 받아 둔 응답을 다시 쓴 실행에서도 같은 값이 나온다
cost_report = {}
for arm, hits in used.items():
    cost_report[arm] = c = cost_summary(MODEL, hits, cache.totals if cache else {})
    money = "비용 모름" if c["total_usd"] is None else f"${c['total_usd']:.4f}  건당 ${c['per_case_usd']:.4f}"
    lat = "지연 모름" if c["latency_median_ms"] is None else f"지연 중앙값 {c['latency_median_ms']}ms"
    print(f"\n  {ARM_NAMES.get(arm, arm)}: {c['n']}건  {money}  {lat}")

hits = [h for arm_hits in used.values() for h in arm_hits]
responses = {"new": sum(h["run"] == RUN_ID for h in hits), "reused": sum(h["run"] != RUN_ID for h in hits),
             "received_in": sorted({h["run"] for h in hits})}
if need_llm and not DRY:
    print(f"\n  이번 실행에서 새로 부른 호출 {responses['new']}건 · 받아 둔 응답 {responses['reused']}건"
          f"  (위 비용은 응답을 받을 때 든 값이다)")

out = {"_역할": "비교 실험 기록. arm 별 판정과 지표 5종, 비용·지연을 남긴다. "
                "compare.py 가 덮어쓴다.",
       "run_id": RUN_ID, "ran_at": datetime.now(timezone.utc).isoformat(),
       "facts_snapshot": facts["snapshot_id"], "model": MODEL, "effort": EFFORT,
       "pricing_per_mtok": PRICES.get(MODEL), "conditions": CONDITIONS,
       "arms": ARMS, "scored": len(scorable), "total": len(rows),
       # LLM 응답을 이번에 새로 받았는지, 받아 둔 것을 썼는지와 그 응답을 받은 실행
       "responses": responses,
       "metrics": report, "cost": cost_report, "rows": rows}
(HERE / OUT_NAME).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n  기록: core/{OUT_NAME}  ({out['run_id']})")
print(describe(CONDITIONS))
