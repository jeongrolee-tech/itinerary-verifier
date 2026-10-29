"""고친 일정을 다시 검사하고, 꼭 지킨다고 한 것을 바꾸면 확인을 받는다."""

import copy

from recheck import recheck_revision

PALACE, MARKET, CHANGDEOK = "경복궁", "광장시장", "창덕궁"
THURSDAY, FRIDAY = "2026-10-08", "2026-10-09"


def palace_market_ktx():
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
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"][0]["start"] = "16:00"
    result = recheck_revision(original, revised, facts, policy)
    assert result["changed_fields"] == ["stops[0].start"]
    assert result["before"]["summary"]["verdict"] == "infeasible"  # 17:30 은 입장마감 17:00 뒤다.
    assert result["after"]["summary"]["verdict"] == "undetermined"  # 광장시장은 여전히 확인 못 한다.
    assert len(result["after"]["checks"]) == len(result["before"]["checks"])
    assert len(result["after"]["hard_constraints"]) == 1


def test_revised_facts_judge_only_the_revised_itinerary(facts, policy):
    """원래 일정은 처음 사실로 봐야 수정 전 판정이 처음 본 것과 같다."""
    itinerary = {"date": THURSDAY, "stops": [
        {"place": "서울시립미술관 서소문본관", "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "15:00", "dwell_minutes": 120}]}
    later = copy.deepcopy(facts)
    later["legs"]["서울시립미술관 서소문본관|경복궁"] = {"minutes": 20, "mode": "transit", "is_lower_bound": False,
                                                   "source": "synthetic_fixture", "method": "synthetic_fixture"}
    result = recheck_revision(itinerary, copy.deepcopy(itinerary), facts, policy, revised_facts=later)
    assert result["before"]["summary"]["verdict"] == "undetermined"  # 저장해 둔 지하철 하한선뿐이다.
    assert result["after"]["summary"]["verdict"] == "feasible"


def test_moving_date_with_train_needs_confirmation(facts, policy):
    """KTX 는 원래 날짜로 예약했을 수 있어 날짜를 옮기면 확인을 받는다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["date"] = FRIDAY
    assert ("DATE_MOVED", "일정 날짜") in confirmations(original, revised, facts, policy)


def test_filling_in_a_missing_date_is_not_a_move(facts, policy):
    """원래 날짜가 없으면 예약을 옮겼는지 물을 근거가 없다."""
    original = palace_market_ktx()
    original["date"] = None
    revised = copy.deepcopy(original)
    revised["date"] = THURSDAY
    assert confirmations(original, revised, facts, policy) == []


def test_cleared_train_time_reads_as_missing_not_none(facts, policy):
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["hard_constraints"][0]["time"] = None
    [c] = recheck_revision(original, revised, facts, policy)["needs_confirmation"]
    assert "None" not in c["detail"] and "열차 출발(시각 없음 서울역)" in c["detail"]


def test_moving_date_without_constraints_needs_no_confirmation(facts, policy):
    original = {"date": THURSDAY, "stops": [{"place": PALACE, "start": "13:00", "dwell_minutes": 60}]}
    revised = copy.deepcopy(original)
    revised["date"] = FRIDAY
    assert confirmations(original, revised, facts, policy) == []


def test_changing_train_time_needs_confirmation(facts, policy):
    """판정이 나아져도 바뀐 열차 시각은 사용자가 말한 조건이 아니다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["hard_constraints"][0]["time"] = "21:30"
    assert ("HARD_CONSTRAINT_CHANGED", "HC1") in confirmations(original, revised, facts, policy)


def test_dropping_train_needs_confirmation(facts, policy):
    """필수 조건을 지우면 열차 검사가 사라져 일정이 쉬워진 것처럼 보인다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["hard_constraints"] = []
    assert ("HARD_CONSTRAINT_CHANGED", "HC1") in confirmations(original, revised, facts, policy)


def test_dropping_stop_needs_confirmation(facts, policy):
    """광장시장을 빼면 확인 못 한 것이 줄어 판정이 나아 보인다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"] = revised["stops"][:1]
    assert ("STOP_DROPPED", MARKET) in confirmations(original, revised, facts, policy)


def test_replacing_stop_needs_confirmation(facts, policy):
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"][0]["place"] = CHANGDEOK
    assert ("STOP_DROPPED", PALACE) in confirmations(original, revised, facts, policy)


def test_retiming_and_reordering_need_no_confirmation(facts, policy):
    """대조군: 시각 · 체류시간 · 순서는 사용자가 고쳐도 되는 것이다."""
    original = palace_market_ktx()
    revised = copy.deepcopy(original)
    revised["stops"] = [{"place": MARKET, "start": "15:00"},
                        {"place": PALACE, "start": "16:00", "dwell_minutes": 45}]
    assert confirmations(original, revised, facts, policy) == []
