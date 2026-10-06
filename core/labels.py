"""
라벨 노후화 — 라벨을 검토한 뒤 데이터가 바뀌어, 그 라벨을 더는 정답으로 믿을 수 없는지 본다.

라벨은 판정 코드와 독립이어야 하지만 데이터 스냅샷과는 독립일 수 없다. 같은 입력의 정답이
facts.json 버전에 따라 달라지기 때문이다 — T10 이 그 증거다. run.py · run_extract.py · compare.py 가
이 규칙 하나로 미검토 라벨을 골라 채점에서 뺀다.
"""

from __future__ import annotations


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
        return f"라벨 기준 스냅샷 {labeled} 을 데이터 이력에서 찾지 못했다"
    if facts["snapshot_id"] not in ids:
        return f"지금 데이터 스냅샷 {facts['snapshot_id']} 이 데이터 이력에 없어 무엇이 바뀌었는지 모른다"

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
