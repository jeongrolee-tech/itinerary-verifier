"""라벨 노후화 · 케이스 구간 · 검사 대조를 본다. 노후화는 데이터끼리만 비교한다."""

import pytest

from labels import case_facts, check_mismatches, stale_reason

SUITE = {"labeled_against_snapshot": "s1"}
MUSEUM, PALACE, MARKET = "서울시립미술관 서소문본관", "경복궁", "광장시장"


def facts(*changes):
    """s1 뒤로 changes 를 차례로 쌓는다. 마지막 것이 지금 스냅샷이다."""
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
    """한 곳 바뀌었다고 전부 무효로 보면 지표가 사라진다."""
    assert stale_reason(case(PALACE), SUITE, facts({"places": [MUSEUM]})) is None


def test_older_change_is_not_forgotten_after_a_newer_one():
    """s2 에서 미술관이 바뀌면 s3 에서 다른 곳이 바뀌어도 미술관 라벨은 미검토다."""
    reason = stale_reason(case(MUSEUM), SUITE, facts({"places": [MUSEUM]}, {"places": [PALACE]}))
    assert reason == "s2 장소 서울시립미술관 서소문본관"


def test_listed_leg_change_makes_it_stale():
    """열차 조건의 마지막 구간도 구간 변경으로 본다."""
    reason = stale_reason(case(PALACE, MARKET, hc="서울역"), SUITE, facts({"legs": [f"{MARKET}|서울역"]}))
    assert reason == f"s2 구간 {MARKET}|서울역"


def test_all_legs_change_spares_a_single_stop():
    assert stale_reason(case(PALACE), SUITE, facts({"legs": "ALL"})) is None
    assert stale_reason(case(PALACE, MARKET), SUITE, facts({"legs": "ALL"})) == "s2 구간 전체"


def test_unknown_labeled_snapshot_is_stale():
    """검토한 스냅샷이 이력에 없으면 무엇이 바뀌었는지 알 수 없다."""
    assert "찾지 못했다" in stale_reason(case(PALACE, labeled="s0"), SUITE, facts({"places": []}))


# 케이스별 테스트용 이동시간

def leg(minutes, source="synthetic_fixture"):
    return {"minutes": minutes, "mode": "transit", "is_lower_bound": False, "source": source, "method": source}


def test_case_legs_are_laid_over_the_stored_legs():
    stored = {"legs": {f"{PALACE}|{MARKET}": leg(4.5, "public_data")}}
    merged = case_facts({"legs": {f"{MUSEUM}|{PALACE}": leg(20)}}, stored)
    assert set(merged["legs"]) == {f"{PALACE}|{MARKET}", f"{MUSEUM}|{PALACE}"}
    assert set(stored["legs"]) == {f"{PALACE}|{MARKET}"}


def test_case_without_legs_uses_the_stored_data():
    stored = {"legs": {}}
    assert case_facts({}, stored) is stored


def test_case_legs_must_be_marked_as_test_values():
    """실제 API 값이 평가셋에 섞이지 않게 테스트용 표시가 없는 구간은 받지 않는다."""
    with pytest.raises(ValueError, match="synthetic_fixture"):
        case_facts({"id": "T99", "legs": {f"{MUSEUM}|{PALACE}": leg(20, "api_runtime")}}, {"legs": {}})


# 검사별 대조
# 판정 코드의 검사와 LLM 의 답을 같은 규칙으로 맞춘다.

def adm(target, status):
    return {"type": "ADMISSION_NOT_POSSIBLE", "target": target, "status": status}


def test_repeat_visit_checks_pair_in_order():
    """두 번째 기대를 첫 번째 검사와 맞추면 맞는 답을 틀렸다고 한다."""
    want = [adm(PALACE, "pass"), adm(PALACE, "fail")]
    assert check_mismatches(want, [adm(PALACE, "pass"), adm(PALACE, "fail")]) == []
    assert check_mismatches(want, [adm(PALACE, "pass"), adm(PALACE, "pass")]) == [{**want[1], "got": "pass"}]


def test_spacing_in_target_does_not_matter():
    """LLM 이 화살표 앞뒤 띄어쓰기를 섞어 써도 같은 검사로 본다."""
    want = [{"type": "INSUFFICIENT_TRAVEL_TIME", "target": "삼청동 → 경복궁", "status": "unknown"}]
    assert check_mismatches(want, [{**want[0], "target": "삼청동→경복궁"}]) == []


def test_missing_check_is_reported_with_no_status():
    assert check_mismatches([adm(PALACE, "pass")], []) == [{**adm(PALACE, "pass"), "got": None}]
