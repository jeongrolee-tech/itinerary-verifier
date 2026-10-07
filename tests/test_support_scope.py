"""
지원 범위 분류 — 2차 피드백 3번 "추출 정확도도 … 지원 범위 분류까지 채점".

장소는 셋 중 하나다. 정보가 있는 장소는 검사하고, 지원 목록 밖 장소는 확인하지 않았다고 알리고, 구역은 어디로
가는지 묻는다. 뒤의 둘은 둘 다 unknown 이지만 푸는 쪽이 다르다 — 지원 목록 밖은 우리가, 구역은 사용자가 푼다.
"""

import pytest

from verdict import judge, support_scope


def itinerary(place, scope="place"):
    return {"date": "2026-10-08",
            "stops": [{"place": place, "start": "14:00", "dwell_minutes": 60, "scope": scope}]}


def place_checks(out, name):
    return {c["type"]: c for c in out["checks"] if c["target"] == name}


def test_places_fall_into_three_kinds(facts):
    assert support_scope({"place": "경복궁"}, facts) == "supported"
    assert support_scope({"place": "국립현대미술관 서울관"}, facts) == "unsupported"
    assert support_scope({"place": "익선동", "scope": "area"}, facts) == "area"
    assert support_scope({"place": "북촌한옥마을"}, facts) == "area"  # facts 가 구역으로 안다


def test_unsupported_place_is_not_asked_to_the_user(facts, policy):
    """지원 목록 밖 장소는 우리가 데이터를 확보해야 풀린다. 사용자에게 어디로 가는지 묻지 않는다."""
    checks = place_checks(judge(itinerary("국립현대미술관 서울관"), facts, policy), "국립현대미술관 서울관")
    for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE"):
        assert checks[t]["status"] == "unknown"
        assert checks[t]["unknown_reason"] == "UNSUPPORTED_PLACE"
        assert set(checks[t]["how_to_resolve"]) == {"system"}


@pytest.mark.parametrize("place, scope", [("익선동", "area"), ("북촌한옥마을", "place")])
def test_area_asks_where_exactly(place, scope, facts, policy):
    """구역은 사용자가 갈 곳을 알려 주면 풀린다. facts 가 구역으로 아는 곳은 추출이 place 로 적어도 구역이다."""
    checks = place_checks(judge(itinerary(place, scope), facts, policy), place)
    for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE"):
        assert checks[t]["unknown_reason"] == "PLACE_NOT_RESOLVED"
        assert place in checks[t]["how_to_resolve"]["user"]
