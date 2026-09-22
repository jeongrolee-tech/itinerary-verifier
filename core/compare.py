#!/usr/bin/env python3
"""
비교 실험 — 판정 방식 3종을 같은 케이스로 돌려 비교한다.

    python core/compare.py                  3종 전부
    python core/compare.py --dry-run        API 없이 배선만 확인
    python core/compare.py --arms code      코드만 (API 키 불필요)
    python core/compare.py --case T05       한 건만
    python core/compare.py --model claude-sonnet-5

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
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))
from verdict import judge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
facts, policy, suite = load("facts.json"), load("policy.json"), load("tests.json")

args = sys.argv[1:]
flag = lambda name, d: next(
    (args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), d)
only = flag("--case", None)
MODEL = flag("--model", "claude-opus-5")
EFFORT = flag("--effort", "low")
ARMS = flag("--arms", "llm_naive,llm_only,llm_with_facts,code").split(",")

PRICE = {"claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
         "claude-haiku-4-5": (1.0, 5.0)}
LINE = "═" * 78
CHECK_TYPES = ["CLOSED_DAY", "ADMISSION_NOT_POSSIBLE",
               "DWELL_NOT_COMPLETABLE", "INSUFFICIENT_TRAVEL_TIME"]

cases = [c for c in suite["cases"] if not only or c["id"].startswith(only)]


# ── 라벨 유효성 (run.py 와 같은 규칙) ────────────────────────────────
def label_is_current(case: dict) -> bool:
    if case.get("label_review_needed"):
        return False
    if case.get("labeled_against", suite.get("labeled_against_snapshot")) == facts["snapshot_id"]:
        return True
    hist = facts.get("snapshot_history") or []
    changed = (hist[-1].get("changed_keys") if hist else None) or {}
    places = [s["place"] for s in case["stops"]]
    n_legs = max(0, len(places) - 1) + len(case.get("hard_constraints", []))
    if any(p in (changed.get("places") or []) for p in places):
        return False
    return not (n_legs and changed.get("legs") == "ALL")


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

검사 종류는 CLOSED_DAY(휴무일), ADMISSION_NOT_POSSIBLE(도착 시각에 입장 불가),
DWELL_NOT_COMPLETABLE(입장은 되지만 체류를 마치기 전에 폐장),
INSUFFICIENT_TRAVEL_TIME(이동시간 + 버퍼 > 여유시간) 네 가지다."""

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
하한선으로도 늦으면 fail 로 확정할 수 있지만, 하한선으로 도착한다고 pass 를 낼 수는 없다.
체류시간이 system_default 면 사용자가 말하지 않은 값이다."""


def itinerary_for(case: dict) -> dict:
    return {"date": case["date"], "stops": case["stops"],
            "hard_constraints": case.get("hard_constraints", [])}


def facts_for_llm() -> dict:
    """
    arm 2 와 arm 3 은 **완전히 같은 사실 데이터**를 받아야 한다.
    그래야 둘의 차이가 판정 방식 차이만 남는다 — 그것이 이 실험의 목적이다.

    케이스별로 부분집합을 주면 arm 2 만 정보가 줄어들어, 차이가 판정 방식 때문인지
    정보량 때문인지 다시 구분할 수 없게 된다. 그래서 전체를 준다.
    """
    return {k: v for k, v in facts.items() if k != "snapshot_history"}


def run_llm(case, client, Judgment, system: str, payload: str):
    started = datetime.now(timezone.utc)
    r = client.messages.parse(
        model=MODEL, max_tokens=16000, output_config={"effort": EFFORT},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": payload}],
        output_format=Judgment,
    )
    u = r.usage
    return r.parsed_output.model_dump(), {
        "latency_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0),
        "cache_write": getattr(u, "cache_creation_input_tokens", 0),
    }


# ── 지표 ────────────────────────────────────────────────────────────
def metrics(scored: list[dict]) -> dict:
    """
    전체 정확도 하나로 끝내지 않는다. 다섯 갈래로 나눠 본다.
    모든 일정을 미확인으로 처리하면 잘못된 확신은 줄지만 사용자가 얻는 결과도 없어지므로,
    얼마나 맞혔는지와 함께 **얼마나 많은 요청에 유효한 판단을 제공했는지**도 본다.
    """
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
        # 유효한 판단(보류가 아닌 판단)을 제공한 비율
        "decisive_rate": round(sum(1 for r in scored if got(r) != "undetermined") / n, 3),
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

    class _Messages:
        calls = 0

        def parse(self, **kw):
            assert kw["messages"][0]["content"].strip(), "payload 가 비어 있다"
            assert kw["system"][0]["text"].strip(), "system 프롬프트가 비어 있다"
            StubClient._Messages.calls += 1
            fmt = kw["output_format"]
            return type("R", (), {"usage": StubClient._Usage(),
                                  "parsed_output": fmt(checks=[], verdict="undetermined",
                                                       message="dry-run")})()

    def __init__(self):
        self.messages = self._Messages()


DRY = "--dry-run" in args
need_llm = any(a.startswith("llm") for a in ARMS)
client = Judgment = None
if need_llm:
    Judgment = build_models()
    if DRY:
        client = StubClient()
        print("  [--dry-run] API 를 부르지 않는다. 배선만 확인한다.\n")
    else:
        import os
        from getpass import getpass

        import anthropic

        key = next((a[6:].strip() for a in args if a.startswith("--key=")), None) \
            or os.environ.get("ANTHROPIC_API_KEY") or getpass("Anthropic API 키 입력: ").strip()
        if not key:
            raise SystemExit("키가 비어 있다.")
        client = anthropic.Anthropic(api_key=key)

RUN_ID = f"cmp-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def save(payload: dict, name: str) -> bool:
    """
    LLM arm 은 건당 10초 넘게 걸리고 돈이 든다. 마지막 쓰기 한 번이 실패해서
    전체 결과가 날아가면 안 된다. 실제로 그렇게 15건 × 2 arm 을 잃었다 —
    실행 중에 작업 디렉터리가 사라져서 FileNotFoundError 가 났다.

    그래서 케이스마다 중간 저장하고, 실패하면 홈 디렉터리로 떨어진다.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    for target in (HERE / name, Path.home() / f"itinerary-verifier-{name}"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            if target.parent != HERE:
                print(f"  ⚠ {HERE} 에 쓸 수 없어 {target} 에 저장했다")
            return True
        except OSError as e:
            print(f"  ⚠ {target} 저장 실패: {e}")
    return False


FACTS_LLM = facts_for_llm()
rows, usage_total = [], {}
for case in cases:
    rec = {"id": case["id"], "set": case["set"], "expect": case["expect"],
           "label_current": label_is_current(case), "arms": {}}

    if "code" in ARMS:
        out = judge(itinerary_for(case), facts, policy)
        rec["arms"]["code"] = {"verdict": out["summary"]["verdict"],
                               "checks": [{"type": c["type"], "target": c["target"],
                                           "status": c["status"]} for c in out["checks"]]}

    # arm0 과 arm1 은 입력이 완전히 같다. 시스템 프롬프트만 다르다.
    raw_payload = f"오늘은 2026-09-22 이다.\n\n[일정]\n{case['raw']}"

    if "llm_naive" in ARMS:
        j, u = run_llm(case, client, Judgment, SYSTEM_LLM_NAIVE, raw_payload)
        rec["arms"]["llm_naive"] = j
        usage_total.setdefault("llm_naive", []).append(u)

    if "llm_only" in ARMS:
        j, u = run_llm(case, client, Judgment, SYSTEM_LLM_ONLY, raw_payload)
        rec["arms"]["llm_only"] = j
        usage_total.setdefault("llm_only", []).append(u)

    if "llm_with_facts" in ARMS:
        payload = (f"[구조화 일정]\n{json.dumps(itinerary_for(case), ensure_ascii=False, indent=1)}\n\n"
                   f"[사실 데이터]\n{json.dumps(FACTS_LLM, ensure_ascii=False, indent=1)}\n\n"
                   f"[정책]\n{json.dumps(policy, ensure_ascii=False, indent=1)}")
        j, u = run_llm(case, client, Judgment, SYSTEM_LLM_FACTS, payload)
        rec["arms"]["llm_with_facts"] = j
        usage_total.setdefault("llm_with_facts", []).append(u)

    rows.append(rec)
    print(f"  {case['id']:<24}" + "  ".join(
        f"{a}={rec['arms'][a]['verdict']:<13}" for a in ARMS if a in rec["arms"]))

    # 다음 케이스가 실패하거나 중단돼도 여기까지는 남는다.
    # LLM arm 은 건당 10초 넘고 돈이 드니 한 번의 쓰기 실패로 전체를 잃으면 안 된다.
    if need_llm and not DRY:
        save({"run_id": RUN_ID, "status": "in_progress", "model": MODEL,
              "facts_snapshot": facts["snapshot_id"], "arms": ARMS,
              "done": len(rows), "total": len(cases), "rows": rows},
             "last-compare.json")

# ── 채점 ────────────────────────────────────────────────────────────
scorable = [r for r in rows if r["label_current"]]
stale = [r for r in rows if not r["label_current"]]

print(f"\n{LINE}\n  지표   facts={facts['snapshot_id']}\n{LINE}")
print(f"  채점 대상 {len(scorable)}건 / 전체 {len(rows)}건"
      + (f"  (라벨 미검토 {len(stale)}건 제외)" if stale else ""))

report = {}
for arm in ARMS:
    sub = [{"expect": r["expect"], "verdict": r["arms"][arm]["verdict"]}
           for r in scorable if arm in r["arms"]]
    report[arm] = metrics(sub)

if scorable:
    LABELS = [
        ("verdict_accuracy", "전체 정확도"),
        ("precision_on_infeasible", "오류 표시 중 실제 오류"),
        ("recall_on_infeasible", "실제 오류 중 찾아낸 비율"),
        ("false_alarm_on_feasible", "정상 일정 오탐 (건)"),
        ("false_pass_on_infeasible", "잘못된 일정 정상 통과 (건)"),
        ("leaked_undetermined_as_feasible", "미확인을 통과로 (건)"),
        ("undetermined_rate", "판정 보류 비율"),
        ("decisive_rate", "유효 판단 제공 비율"),
    ]
    w = max(len(l) for _, l in LABELS) + 2
    print(f"\n  {'지표'.ljust(w)}" + "".join(f"{a:<18}" for a in ARMS))
    print("  " + "-" * (w + 18 * len(ARMS)))
    for key, label in LABELS:
        cells = []
        for a in ARMS:
            v = report[a].get(key)
            cells.append(f"{'-' if v is None else v:<18}")
        print(f"  {label.ljust(w)}" + "".join(cells))

    print(f"\n  라벨 분포: {report[ARMS[0]]['counts']}")
    print(f"\n  ⚠ 표본 {len(scorable)}건이다. 정밀도·재현율은 신뢰구간이 넓다.")
    print("    '잘못된 일정 정상 통과' 와 '미확인을 통과로' 는 0이냐 아니냐가 의미 있고,")
    print("    보류 비율과 유효 판단 비율은 비율로 읽을 수 있다.")

if stale:
    print(f"\n  라벨 미검토 {len(stale)}건 — 출력은 저장했다. "
          f"워크시트를 채우면 재실행 없이 채점된다.")

cost_report = {}
for arm, us in usage_total.items():
    pin, pout = PRICE.get(MODEL, (0, 0))
    cost = sum((u["input_tokens"] * pin + u["output_tokens"] * pout
                + u["cache_write"] * pin * 1.25 + u["cache_read"] * pin * 0.1) / 1e6 for u in us)
    lat = sorted(u["latency_ms"] for u in us)
    cost_report[arm] = {"n": len(us), "total_usd": round(cost, 4),
                        "per_case_usd": round(cost / len(us), 4),
                        "latency_median_ms": lat[len(lat) // 2]}
    print(f"\n  {arm}: {len(us)}건  ${cost:.4f}  건당 ${cost / len(us):.4f}  "
          f"지연 중앙값 {lat[len(lat) // 2]}ms")

out = {"run_id": RUN_ID, "ran_at": datetime.now(timezone.utc).isoformat(),
       "facts_snapshot": facts["snapshot_id"], "model": MODEL, "effort": EFFORT,
       "arms": ARMS, "scored": len(scorable), "total": len(rows),
       "metrics": report, "cost": cost_report, "rows": rows}
OUT_NAME = "last-compare-dryrun.json" if DRY else "last-compare.json"
if not save(out, OUT_NAME):
    # 여기까지 온 실행은 돈과 시간을 이미 썼다. 화면에라도 남긴다.
    print("\n  파일 저장에 실패했으므로 결과를 아래에 그대로 출력한다.\n")
    print(json.dumps(out, ensure_ascii=False, indent=2))
print(f"\n  기록: core/{OUT_NAME}  ({out['run_id']})")
