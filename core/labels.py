"""
평가셋 케이스와 라벨 — 라벨 노후화, 케이스별 테스트용 이동시간, 라벨과 판정 결과 대조, 기대 추출값의 채점 항목.

라벨 노후화: 라벨을 검토한 뒤 데이터가 바뀌어, 그 라벨을 더는 정답으로 믿을 수 없는지 본다.

라벨은 판정 코드와 독립이어야 하지만 데이터 스냅샷과는 독립일 수 없다. 같은 입력의 정답이
facts.json 버전에 따라 달라지기 때문이다 — T10 이 그 증거다. run.py · run_extract.py · compare.py 가
이 규칙 하나로 미검토 라벨을 골라 채점에서 뺀다.
"""

from __future__ import annotations

from report import SEVERITY_NAMES, STATUS_NAMES, VERDICT_NAMES
from verdict import UNKNOWN

# 기대 추출값을 채점하는 항목 — run_extract.py 가 채점하고 eval_report.py 가 보고한다.
# 2차 피드백 3번 "날짜/장소/시각뿐 아니라 추론한 연도의 출처와 지원 범위 분류까지". 시각 출처는 같은 이유로
# 더했다(2026-10-07 사용자 결정): 출처가 틀리면 판정이 물어야 할 시각을 확정해 버린다
EXTRACTION_FIELDS = {"date": "날짜·요일", "date_source": "연도 출처", "place": "장소", "start": "시각",
                     "start_source": "시각 출처", "dwell": "체류시간", "support": "지원 범위 분류",
                     "hard_constraints": "필수 일정"}


def stale_reason(case: dict, suite: dict, facts: dict) -> str | None:
    """미검토면 이유를, 지금 데이터에서도 유효하면 None 을 돌려준다.

    스냅샷 id 만 비교하면 너무 거칠다. 데이터가 한 곳 바뀌었다고 전부 무효로 보면 지표가 사라진다.
    라벨을 검토한 스냅샷 뒤의 변경 중 하나라도 케이스가 쓰는 장소나 구간을 건드렸을 때만 미검토로 본다.
    """
    # ⭐ 여기서 판정 결과(out["summary"]["verdict"])를 쓰고 싶어지는데, 쓰면 순환 논리가 된다.
    #    "코드 출력이 라벨과 다르면 라벨이 낡은 것" 으로 판단하면, 코드에 진짜 버그가 있을 때도
    #    "라벨이 낡았네" 로 넘어가 버려 버그를 영원히 못 잡는다. 그래서 데이터끼리만
    #    (케이스의 장소 · 구간 ↔ 바뀐 키 목록) 비교한다.
    if case.get("label_review_needed"):
        return case["label_review_needed"]
    labeled = case.get("labeled_against", suite.get("labeled_against_snapshot"))
    if labeled == facts["snapshot_id"]:
        return None
    history = facts.get("snapshot_history") or []
    ids = [h["id"] for h in history]
    if labeled not in ids:
        return f"정답 기준 데이터 버전 {labeled} 을 데이터 이력에서 찾지 못했다"
    if facts["snapshot_id"] not in ids:
        return f"지금 데이터 버전 {facts['snapshot_id']} 이 데이터 이력에 없어 무엇이 바뀌었는지 모른다"

    places = [s["place"] for s in case["stops"]]
    legs = [f"{a}|{b}" for a, b in zip(places, places[1:])]
    if places:
        legs += [f"{places[-1]}|{hc['place']}" for hc in case.get("hard_constraints", [])]

    # ⭐ 가장 최근 변경 하나만 보면, 그 전 변경이 건드린 라벨이 다음 변경 뒤에 다시 '유효' 로 보인다.
    #    라벨을 검토한 스냅샷 뒤의 변경을 모두 본다. 구간은 전체("ALL") 또는 "A|B" 목록으로 적는다.
    hits = []
    for h in history[ids.index(labeled) + 1:]:
        changed = h.get("changed_keys") or {}
        hits += [f"{h['id']} 장소 {p}" for p in places if p in (changed.get("places") or [])]
        changed_legs = changed.get("legs")
        if changed_legs == "ALL" and legs:
            hits.append(f"{h['id']} 구간 전체")
        elif isinstance(changed_legs, list):
            hits += [f"{h['id']} 구간 {k}" for k in legs if k in changed_legs]
    return " / ".join(hits) if hits else None


def case_facts(case: dict, facts: dict) -> dict:
    """케이스에 적어 둔 테스트용 이동시간(legs)을 사실 데이터에 얹은 사본. 없으면 facts 를 그대로 돌려준다.

    3~6곳 일정은 저장해 둔 이동시간(지하철 하한선 4구간)만으로는 판정할 수 없다. 판정할 때 받는 TMAP 값은
    저장할 수 없어 같은 결과를 다시 만들 수 없다(2차 피드백 3번). 그래서 평가셋 케이스는 구간마다 테스트용으로
    정한 이동시간을 적을 수 있고, 라벨도 그 값을 기준으로 정한다. 실제 이동시간이 아니다.
    """
    legs = case.get("legs")
    if not legs:
        return facts
    # 실제 API 값이 섞여 들어오지 않게, 테스트용 표시가 없는 구간은 받지 않는다
    wrong = [k for k, leg in legs.items() if leg.get("source") != "synthetic_fixture"]
    if wrong:
        raise ValueError(f"{case.get('id')} 의 legs 는 source 가 synthetic_fixture 여야 한다: {wrong}")
    return {**facts, "legs": {**facts["legs"], **legs}}


def grade(case, out):
    """기대 판정과 기대 검사 결과를 라벨과 대조한다. run.py 와 pytest(tests/test_golden.py)가 같이 쓴다."""
    problems = []
    if out["summary"]["verdict"] != case["expect"]:
        problems.append(f"판정 기대 {VERDICT_NAMES[case['expect']]} → 실제 {VERDICT_NAMES[out['summary']['verdict']]}")
    for want in case.get("expect_checks", []):
        got = next((c for c in out["checks"]
                    if c["type"] == want["type"] and c["target"] == want["target"]), None)
        if got is None:
            problems.append(f"{want['type']} / {want['target']} 검사가 아예 없다")
        elif got["status"] != want["status"]:
            problems.append(f"{want['type']} / {want['target']} 기대 {STATUS_NAMES[want['status']]} "
                            f"→ 실제 {STATUS_NAMES[got['status']]}")
    for want in case.get("expect_hard_constraints", []):
        got = next((h for h in out["hard_constraints"] if h["id"] == want["id"]), None)
        if got is None:
            problems.append(f"필수 일정 {want['id']} 이 없다")
            continue
        for k in ("status", "severity"):
            if k in want and got["policy"].get(k) != want[k]:
                names = STATUS_NAMES if k == "status" else SEVERITY_NAMES
                problems.append(f"{want['id']}.{k} 기대 {names.get(want[k], want[k])} "
                                f"→ 실제 {names.get(got['policy'].get(k), got['policy'].get(k))}")
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
        problems.append("expect_applied.missing 은 있지만 미확인 결과가 없다")

    return problems
