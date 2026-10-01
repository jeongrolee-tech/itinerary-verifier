"""
수정 후 재검사 — 2차 피드백 2번.

"이미 예약한 열차나 반드시 방문해야 할 장소가 있다면, 수정안이 날짜/장소를 바꾸기
전에 사용자 확인을 받아야 한다."

서비스는 수정안을 만들지 않고 사용자가 고친 일정을 다시 검사한다. 그래도 고친 일정이
사용자가 꼭 지킨다고 한 것을 바꿨다면, 판정이 나아졌다는 것만으로 끝내면 안 된다.
날짜를 옮기면 예약한 열차도 새 날짜에 탈 수 있는 것처럼 판정되고, 장소를 빼면
확인할 것이 줄어 일정이 나아진 것처럼 보인다.

반례마다 대조군을 둔다. 대조군이 없으면 모든 변경에 확인을 붙이는 구현도 통과한다.
xfail(strict=True) 는 아직 고치지 않은 반례다. 고치면 XPASS 가 되어 스위트가
실패하므로 그 작업에서 표시를 지운다.
"""

import copy

import pytest

from recheck import recheck_revision

PALACE, MARKET, CHANGDEOK = "경복궁", "광장시장", "창덕궁"
THURSDAY, FRIDAY = "2026-10-08", "2026-10-09"
TODO = pytest.mark.xfail(strict=True, reason="2-4: 사용자 확인이 필요한 변경을 아직 표시하지 않는다")


def palace_market_ktx():
    """T04 와 같은 일정 — 경복궁 17:30, 광장시장 19:00, 20:30 서울역 KTX 는 꼭 타야 한다."""
    return {
        "date": THURSDAY,
        "stops": [{"place": PALACE, "start": "17:30", "dwell_minutes": 60},
                  {"place": MARKET, "start": "19:00"}],
        "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30",
                              "buffer_rule": "rail_boarding", "source": "user_stated"}],
    }


def confirmations(original, revised, facts, policy):
    result = recheck_revision(original, revised, facts, policy)
    return [(c["kind"], c["target"]) for c in result.get("needs_confirmation", [])]


def test_recheck_judges_the_whole_revised_itinerary(facts, policy):
    """시각 하나만 고쳐도 일정 전체를 다시 판정한다 — 지금도 되는 것."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"][0]["start"] = "16:00"
    result = recheck_revision(original, revised, facts, policy)
    assert result["rechecked_all"] is True
    assert result["changed_fields"] == ["stops[0].start"]
    assert result["before"]["summary"]["verdict"] == "infeasible"  # 17:30 은 입장마감(17:00) 뒤
    assert result["after"]["summary"]["verdict"] == "undetermined"  # 광장시장은 여전히 확인 못 한다
    assert len(result["after"]["checks"]) == len(result["before"]["checks"])
    assert len(result["after"]["hard_constraints"]) == 1


@TODO
def test_moving_date_with_train_needs_confirmation(facts, policy):
    """날짜를 옮기면 KTX 도 새 날짜에 탈 수 있는 것처럼 판정된다. 원래 날짜로 예약했을 수 있다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["date"] = FRIDAY
    assert ("DATE_MOVED", "일정 날짜") in confirmations(original, revised, facts, policy)


def test_moving_date_without_constraints_needs_no_confirmation(facts, policy):
    """대조군 — 꼭 지킨다고 한 조건이 없으면 날짜를 옮긴 것은 사용자가 고친 그대로다."""
    original = {"date": THURSDAY, "stops": [{"place": PALACE, "start": "13:00", "dwell_minutes": 60}]}
    revised = copy.deepcopy(original)
    revised["date"] = FRIDAY
    assert confirmations(original, revised, facts, policy) == []


@TODO
def test_changing_train_time_needs_confirmation(facts, policy):
    """꼭 타야 한다고 한 열차의 시각이 바뀌었다. 판정이 나아져도 사용자가 말한 조건이 아니다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["hard_constraints"][0]["time"] = "21:30"
    assert ("HARD_CONSTRAINT_CHANGED", "HC1") in confirmations(original, revised, facts, policy)


@TODO
def test_dropping_train_needs_confirmation(facts, policy):
    """필수 조건을 지우면 열차 검사가 사라져 일정이 쉬워진 것처럼 보인다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["hard_constraints"] = []
    assert ("HARD_CONSTRAINT_CHANGED", "HC1") in confirmations(original, revised, facts, policy)


@TODO
def test_dropping_stop_needs_confirmation(facts, policy):
    """광장시장을 빼면 확인 못 한 것이 줄어 판정이 나아진다. 장소를 지워서 푼 것이다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"] = revised["stops"][:1]
    assert ("STOP_DROPPED", MARKET) in confirmations(original, revised, facts, policy)


@TODO
def test_replacing_stop_needs_confirmation(facts, policy):
    """경복궁을 창덕궁으로 바꾸면 가려던 곳이 바뀐다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"][0]["place"] = CHANGDEOK
    assert ("STOP_DROPPED", PALACE) in confirmations(original, revised, facts, policy)


def test_retiming_and_reordering_need_no_confirmation(facts, policy):
    """대조군 — 시각 · 체류시간 · 방문 순서는 사용자가 고쳐도 되는 것이다. 확인을 붙이지 않는다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"] = [{"place": MARKET, "start": "15:00"},
                        {"place": PALACE, "start": "16:00", "dwell_minutes": 45}]
    assert confirmations(original, revised, facts, policy) == []
