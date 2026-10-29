"""고친 일정을 다시 검사하고, 꼭 지킨다고 한 것을 바꾸면 확인을 받는다."""

import copy

import pytest

from helpers import MARKET, MUSEUM, PALACE, THURSDAY, estimate, ktx
from recheck import recheck_revision

FRIDAY = "2026-10-09"


def palace_market_ktx(**change):
    return {"date": THURSDAY,
            "stops": [{"place": PALACE, "start": "17:30", "dwell_minutes": 60}, {"place": MARKET, "start": "19:00"}],
            "hard_constraints": [ktx("20:30")], **change}


def confirmations(original, revised, facts, policy):
    return [(c["kind"], c["target"]) for c in recheck_revision(original, revised, facts, policy)["needs_confirmation"]]


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
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "15:00", "dwell_minutes": 120}]}
    later = copy.deepcopy(facts)
    later["legs"][f"{MUSEUM}|{PALACE}"] = estimate(20)
    result = recheck_revision(itinerary, copy.deepcopy(itinerary), facts, policy, revised_facts=later)
    assert result["before"]["summary"]["verdict"] == "undetermined"  # 저장해 둔 지하철 하한선뿐이다.
    assert result["after"]["summary"]["verdict"] == "feasible"


# 판정이 나아진 것만으로 받아들이지 않는다. 장소를 빼거나 필수 일정을 지우기만 해도 판정은 나아진다.
@pytest.mark.parametrize("revised, asked", [
    (palace_market_ktx(date=FRIDAY), ("DATE_MOVED", "일정 날짜")),  # KTX 는 원래 날짜로 예약했을 수 있다.
    (palace_market_ktx(hard_constraints=[ktx("21:30")]), ("HARD_CONSTRAINT_CHANGED", "HC1")),
    (palace_market_ktx(hard_constraints=[]), ("HARD_CONSTRAINT_CHANGED", "HC1")),
    (palace_market_ktx(stops=[{"place": PALACE, "start": "17:30", "dwell_minutes": 60}]), ("STOP_DROPPED", MARKET)),
    (palace_market_ktx(stops=[{"place": "창덕궁", "start": "17:30", "dwell_minutes": 60},
                              {"place": MARKET, "start": "19:00"}]), ("STOP_DROPPED", PALACE)),
], ids=["date-moved", "train-retimed", "train-dropped", "stop-dropped", "stop-replaced"])
def test_changing_what_was_promised_needs_confirmation(facts, policy, revised, asked):
    assert asked in confirmations(palace_market_ktx(), revised, facts, policy)


@pytest.mark.parametrize("original, revised", [
    # 원래 날짜가 없으면 예약을 옮겼는지 물을 근거가 없다.
    (palace_market_ktx(date=None), palace_market_ktx()),
    # 필수 일정이 없으면 날짜를 옮겨도 묻지 않는다.
    ({"date": THURSDAY, "stops": [{"place": PALACE, "start": "13:00", "dwell_minutes": 60}]},
     {"date": FRIDAY, "stops": [{"place": PALACE, "start": "13:00", "dwell_minutes": 60}]}),
    # 대조군: 시각 · 체류시간 · 순서는 사용자가 고쳐도 되는 것이다.
    (palace_market_ktx(), palace_market_ktx(stops=[{"place": MARKET, "start": "15:00"},
                                                   {"place": PALACE, "start": "16:00", "dwell_minutes": 45}])),
], ids=["date-filled-in", "no-constraints", "retimed-and-reordered"])
def test_free_changes_need_no_confirmation(facts, policy, original, revised):
    assert confirmations(original, revised, facts, policy) == []


def test_cleared_train_time_reads_as_missing_not_none(facts, policy):
    [c] = recheck_revision(palace_market_ktx(), palace_market_ktx(hard_constraints=[ktx(None)]),
                           facts, policy)["needs_confirmation"]
    assert "None" not in c["detail"] and "열차 출발(시각 없음 서울역)" in c["detail"]
