"""장소를 지원 · 지원 밖 · 구역으로 나눈다. 지원 밖은 우리가, 구역은 사용자가 푼다."""

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
    assert support_scope({"place": "북촌한옥마을"}, facts) == "area"  # facts 가 구역으로 안다.


def test_unsupported_place_is_not_asked_to_the_user(facts, policy):
    """지원 목록 밖 장소는 우리가 데이터를 확보해야 풀린다."""
    checks = place_checks(judge(itinerary("국립현대미술관 서울관"), facts, policy), "국립현대미술관 서울관")
    for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE"):
        assert checks[t]["status"] == "unknown"
        assert checks[t]["unknown_reason"] == "UNSUPPORTED_PLACE"
        assert set(checks[t]["how_to_resolve"]) == {"system"}


@pytest.mark.parametrize("place, scope", [("익선동", "area"), ("북촌한옥마을", "place")])
def test_area_asks_where_exactly(place, scope, facts, policy):
    """facts 가 구역으로 아는 곳은 추출이 place 로 적어도 구역이다."""
    checks = place_checks(judge(itinerary(place, scope), facts, policy), place)
    for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE"):
        assert checks[t]["unknown_reason"] == "PLACE_NOT_RESOLVED"
        assert place in checks[t]["how_to_resolve"]["user"]
