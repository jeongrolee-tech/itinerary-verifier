"""
라벨 노후화 — 라벨을 검토한 뒤 데이터가 바뀌어 그 라벨을 더는 믿을 수 없는지 본다(core/labels.py).

데이터 이력은 테스트용으로 만든다. 판정 결과는 보지 않고 데이터끼리만 비교하는지 본다.
"""

from labels import stale_reason

SUITE = {"labeled_against_snapshot": "s1"}
MUSEUM, PALACE, MARKET = "서울시립미술관 서소문본관", "경복궁", "광장시장"


def facts(*changes):
    """s1 다음에 changes 가 차례로 쌓인 데이터. 마지막 것이 지금 스냅샷이다."""
    history = [{"id": "s1"}] + [{"id": f"s{i}", "changed_keys": c} for i, c in enumerate(changes, 2)]
    return {"snapshot_id": history[-1]["id"], "snapshot_history": history}


def case(*places, labeled=None, hc=None):
    c = {"stops": [{"place": p} for p in places], "hard_constraints": [{"place": hc}] if hc else []}
    if labeled:
        c["labeled_against"] = labeled
    return c


def test_label_on_current_data_is_current():
    assert stale_reason(case(PALACE, labeled="s2"), SUITE, facts({"places": [PALACE]})) is None


def test_change_to_its_place_makes_it_stale():
    assert "s2 장소 경복궁" in stale_reason(case(PALACE), SUITE, facts({"places": [PALACE]}))


def test_change_elsewhere_keeps_it_current():
    """데이터가 한 곳 바뀌었다고 전부 무효로 보면 지표가 사라진다. 쓰는 장소가 안 바뀌었으면 유효하다."""
    assert stale_reason(case(PALACE), SUITE, facts({"places": [MUSEUM]})) is None


def test_older_change_is_not_forgotten_after_a_newer_one():
    """s2 에서 미술관이 바뀌고 s3 에서 다른 곳이 바뀌어도, s1 에서 검토한 미술관 라벨은 여전히 미검토다.
    전에는 가장 최근 변경(s3)만 봐서 다시 유효로 보였다."""
    reason = stale_reason(case(MUSEUM), SUITE, facts({"places": [MUSEUM]}, {"places": [PALACE]}))
    assert reason == "s2 장소 서울시립미술관 서소문본관"


def test_listed_leg_change_makes_it_stale():
    """구간을 목록으로 적은 변경도 본다. 전에는 '전체' 일 때만 봤다. 열차 조건의 마지막 구간도 구간이다."""
    reason = stale_reason(case(PALACE, MARKET, hc="서울역"), SUITE, facts({"legs": [f"{MARKET}|서울역"]}))
    assert reason == f"s2 구간 {MARKET}|서울역"


def test_all_legs_change_spares_a_single_stop():
    """구간 전체가 바뀌어도 이동이 없는 한 곳짜리 일정은 상관없다."""
    assert stale_reason(case(PALACE), SUITE, facts({"legs": "ALL"})) is None
    assert stale_reason(case(PALACE, MARKET), SUITE, facts({"legs": "ALL"})) == "s2 구간 전체"


def test_unknown_labeled_snapshot_is_stale():
    """어느 데이터로 검토했는지 이력에 없으면 무엇이 바뀌었는지 알 수 없다. 유효하다고 보지 않는다."""
    assert "찾지 못했다" in stale_reason(case(PALACE, labeled="s0"), SUITE, facts({"places": []}))
