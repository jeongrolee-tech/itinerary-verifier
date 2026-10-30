"""정답 라벨로 채점하고, 데이터가 바뀌어 다시 확인할 라벨을 고른다."""

from __future__ import annotations

from report import SEVERITY_NAMES, STATUS_NAMES, VERDICT_NAMES
from verdict import UNKNOWN, support_scope, verdict_of

# 구조화 추출 채점 항목이다. 출처가 틀리면 물어야 할 값을 확정해 버려서 출처도 채점한다.
EXTRACTION_FIELDS = {"date": "날짜·요일", "date_source": "연도 출처", "place": "장소", "start": "시각",
                     "start_source": "시각 출처", "dwell": "체류시간", "support": "지원 범위 분류",
                     "hard_constraints": "필수 일정", "avoid_taxi": "택시 안 탐", "outside_only": "입장 안 함"}


def stale_reason(case: dict, suite: dict, facts: dict) -> str | None:
    """라벨 검토 뒤 케이스의 장소나 구간 데이터가 바뀌었으면 그 이유를 돌려준다."""
    # 판정 결과는 보지 않는다. 보면 코드 버그도 낡은 라벨로 넘어가는 순환 논리가 된다.
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

    # 검토 뒤의 변경을 모두 본다. 마지막 변경만 보면 앞서 건드린 라벨이 유효로 보인다.
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
    """테스트용 이동시간을 facts 에 얹는다. TMAP 값은 저장할 수 없어 평가에 못 쓴다."""
    legs = case.get("legs")
    if not legs:
        return facts
    # 실제 API 값이 섞이지 않게 테스트용 표시가 있는 구간만 받는다.
    wrong = [k for k, leg in legs.items() if leg.get("source") != "synthetic_fixture"]
    if wrong:
        raise ValueError(f"{case.get('id')} 의 legs 는 source 가 synthetic_fixture 여야 한다: {wrong}")
    return {**facts, "legs": {**facts["legs"], **legs}}


def check_mismatches(expected: list[dict], checks: list[dict]) -> list[dict]:
    """기대 검사와 상태가 다른 것만 실제 상태(got)를 붙여 돌려준다. LLM 의 답에도 쓴다."""
    # 같은 장소를 두 번 가면 같은 검사가 둘이라 앞에서부터 하나씩 짝짓는다.
    # LLM 이 'A→B' 와 'A → B' 를 섞어 써서 띄어쓰기는 보지 않는다.
    pending = list(checks)
    wrong = []
    for want in expected:
        got = next((c for c in pending if c["type"] == want["type"]
                    and c["target"].replace(" ", "") == want["target"].replace(" ", "")), None)
        if got is not None:
            pending.remove(got)
        if got is None or got["status"] != want["status"]:
            wrong.append({**want, "got": got and got["status"]})
    return wrong


def hard_constraint_mismatches(case: dict, ans: dict) -> list[dict]:
    """기대 필수 일정마다 상태를 맞춰 본다. 판정 코드는 id 로, LLM 은 마지막 장소에서 가는 이동 검사로 찾는다."""
    # LLM 은 compare.RULES 11 대로 필수 일정을 '마지막 장소 → 필수 일정 장소' 이동 검사로 적는다.
    places = {h["id"]: h.get("place") for h in case.get("hard_constraints", [])}
    last = case["stops"][-1]["place"] if case.get("stops") else None
    wrong = []
    for want in case.get("expect_hard_constraints", []):
        if "status" not in want:
            continue
        if "hard_constraints" in ans:
            got = next((h["status"] for h in ans["hard_constraints"] if h["id"] == want["id"]), None)
        else:
            target = f"{last}→{places.get(want['id'])}"
            got = next((c["status"] for c in ans["checks"] if c["type"] == "INSUFFICIENT_TRAVEL_TIME"
                        and c["target"].replace(" ", "") == target.replace(" ", "")), None)
        if got != want["status"]:
            wrong.append({**want, "got": got})
    return wrong


def grade_answer(case: dict, ans: dict) -> dict:
    """비교 실험의 답 하나를 채점한다. 검사의 이유 글은 정답에 없어 채점하지 않는다."""
    statuses = [c["status"] for c in ans["checks"]] + [h["status"] for h in ans.get("hard_constraints", [])]
    return {"verdict_ok": ans["verdict"] == case["expect"],
            "checks_expected": len(case.get("expect_checks", [])),
            "checks_wrong": check_mismatches(case.get("expect_checks", []), ans["checks"]),
            "hard_constraints_expected": sum("status" in w for w in case.get("expect_hard_constraints", [])),
            "hard_constraints_wrong": hard_constraint_mismatches(case, ans),
            "consistent": verdict_of(statuses) == ans["verdict"] if ans["verdict"] in VERDICT_NAMES else None}


def grade_extraction(case: dict, got: dict, facts: dict) -> dict[str, list[str]]:
    """기대 추출값과 어긋난 점을 항목마다 모은다."""
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
        # 라벨에 출처가 없으면 사용자가 말한 시각이다.
        want_src = want.get("start_source", "explicit" if want.get("start") else "missing")
        if want_src != g.get("start_source"):
            p["start_source"].append(f"stops[{i}].start_source 기대 {want_src} → 추출값 {g.get('start_source')}")
        # 입력에 없던 체류시간을 채워 넣었는지가 가장 중요하다.
        if "dwell_minutes" not in want and "dwell_minutes" in g:
            p["dwell"].append(f"stops[{i}] 입력에 없는 체류시간 {g['dwell_minutes']}분을 추가했다")
        elif want.get("dwell_minutes") != g.get("dwell_minutes"):
            p["dwell"].append(f"stops[{i}].dwell_minutes 기대 {want.get('dwell_minutes')} → 추출값 {g.get('dwell_minutes')}")
        # 밖에서만 머문다는 말을 놓치면 문 닫은 걸 알고 가는 일정을 휴무 위반으로 낸다.
        if want.get("outside_only", False) != g.get("outside_only", False):
            p["outside_only"].append(f"stops[{i}].outside_only 기대 {want.get('outside_only', False)} → "
                                     f"추출값 {g.get('outside_only')}")
        want_sup, got_sup = support_scope(want, facts), support_scope(g, facts)
        if want_sup != got_sup:
            p["support"].append(f"stops[{i}] 지원 범위 기대 {want_sup} → 추출값 {got_sup}")

    want_hc, got_hc = case.get("hard_constraints", []), got.get("hard_constraints", [])
    if len(want_hc) != len(got_hc):
        p["hard_constraints"].append(f"필수 일정 개수 기대 {len(want_hc)} → 추출값 {len(got_hc)}")
    for i, (want, g) in enumerate(zip(want_hc, got_hc)):
        # 라벨에 시각 출처가 없으면 장소처럼 사용자가 말한 시각으로 본다.
        want = {**want, "time_source": want.get("time_source", "explicit" if want.get("time") else "missing")}
        for k in ("type", "place", "time", "time_source"):
            if want.get(k) != g.get(k):
                p["hard_constraints"].append(f"HC[{i}].{k} 기대 {want.get(k)} → 추출값 {g.get(k)}")
    # 택시를 안 탄다는 말을 놓치면 사용자가 뺀 택시로 통과시킨다.
    if case.get("avoid_taxi", False) != got.get("avoid_taxi", False):
        p["avoid_taxi"].append(f"avoid_taxi 기대 {case.get('avoid_taxi', False)} → 추출값 {got.get('avoid_taxi')}")
    return p


def grade(case, out):
    """판정 결과를 라벨의 기대 판정과 기대 검사에 대조한다."""
    problems = []
    if out["summary"]["verdict"] != case["expect"]:
        problems.append(f"판정 기대 {VERDICT_NAMES[case['expect']]} → 실제 {VERDICT_NAMES[out['summary']['verdict']]}")
    for m in check_mismatches(case.get("expect_checks", []), out["checks"]):
        problems.append(f"{m['type']} / {m['target']} 검사가 아예 없다" if m["got"] is None else
                        f"{m['type']} / {m['target']} 기대 {STATUS_NAMES[m['status']]} → 실제 {STATUS_NAMES[m['got']]}")
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
    """expect_applied 는 사람이 읽는 글이라 결과에 흔적이 남았는지만 본다."""
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
