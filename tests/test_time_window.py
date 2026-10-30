"""시각이 빠진 장소의 시간대를 본다. 기대 결과는 판정 코드를 보지 않고 손으로 계산했다."""

import pytest

from helpers import THURSDAY, estimate, ktx, place_checks, poi, transit
from report import PLACE_CHECKS, questions, timeline
from routes import live_legs
from verdict import FAIL, PASS, UNKNOWN, judge

# 세 궁이 모두 여는 수요일이다. 모두 09:00 에 열고 창덕궁은 17:30 에 닫는다. 경복궁은 10월에 입장마감이 17:00 이다.
WEDNESDAY = "2026-11-18"


def add_legs(facts):
    facts["legs"]["덕수궁|창덕궁"] = estimate(20)
    facts["legs"]["창덕궁|경복궁"] = estimate(15)


def three_palaces(changdeok_dwell=90, palace_start="15:30", deoksu_dwell=60):
    return {"date": WEDNESDAY, "stops": [
        {"place": "덕수궁", "start": "10:00", "dwell_minutes": deoksu_dwell},
        {"place": "창덕궁", "dwell_minutes": changdeok_dwell},
        {"place": "경복궁", "start": palace_start, "dwell_minutes": 60}]}


def moves(result):
    return [c for c in result["checks"] if c["type"] == "INSUFFICIENT_TRAVEL_TIME" and "(없음)" not in c["target"]]


def asked_time(result, name):
    return place_checks(result, name)["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"


# 시간대가 있는 경우

def test_gap_is_found_and_told(facts, policy):
    """T36: 11:00 + 20 + 10 = 11:30 에 들어가면 다 된다. 뒤 이동은 11:30 입장 기준만 확인해 그 시각만 알린다."""
    add_legs(facts)
    result = judge(three_palaces(), facts, policy)
    c = place_checks(result, "창덕궁")
    assert [c[t]["status"] for t in PLACE_CHECKS] == [PASS, PASS, PASS]
    assert [m["status"] for m in moves(result)] == [PASS, PASS]
    notice = c["ADMISSION_NOT_POSSIBLE"].get("notice", "")
    assert "창덕궁에는 11:30에 들어가면 돼요" in notice and "13:35" not in notice
    assert questions(result) == []
    assert result["summary"]["verdict"] == "feasible"


@pytest.mark.parametrize("date, stops, name, told", [
    # 앞 장소가 없으면 09:00 개장에 들어갈 때의 뒤 이동만 확인했다.
    pytest.param(WEDNESDAY, [{"place": "창덕궁", "dwell_minutes": 90},
                             {"place": "경복궁", "start": "13:00", "dwell_minutes": 60}],
                 "창덕궁", "창덕궁에는 09:00에 들어가면 돼요", id="first-stop"),
    # 뒤 장소가 없으면 끝이 폐장 같은 사실로만 정해진다. 17:30 폐장 전에 90분을 마치도록 16:00 까지다.
    pytest.param(WEDNESDAY, [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                             {"place": "창덕궁", "dwell_minutes": 90}],
                 "창덕궁", "창덕궁에는 11:30~16:00 사이에 들어가면 돼요", id="last-stop"),
    # 입장마감 17:00 정각은 공식 안내가 말하지 않아 그 전까지로 알린다.
    pytest.param(THURSDAY, [{"place": "경복궁", "dwell_minutes": 60}], "경복궁",
                 "경복궁에는 09:00부터 17:00 전에 들어가면 돼요", id="only-stop"),
])
def test_window_is_bounded_by_the_next_stop_or_the_hours(facts, policy, date, stops, name, told):
    add_legs(facts)
    result = judge({"date": date, "stops": stops}, facts, policy)
    assert told in place_checks(result, name)["ADMISSION_NOT_POSSIBLE"].get("notice", "")
    assert result["summary"]["verdict"] == "feasible"


def test_timeline_shows_the_checked_entry(facts, policy):
    add_legs(facts)
    itinerary = three_palaces()
    (row,) = [r for r in timeline(itinerary, judge(itinerary, facts, policy)) if r["title"] == "창덕궁"]
    assert row["time"] == "11:30"


# 시간대가 없는 경우

def test_no_gap_even_without_travel_is_a_violation(facts, policy):
    """T37: 13:00 에 나와 이동 0분이어도 창덕궁 두 시간이면 15:00 이라 14:00 에 늦는다."""
    add_legs(facts)
    result = judge(three_palaces(changdeok_dwell=120, palace_start="14:00", deoksu_dwell=180), facts, policy)
    before, after = moves(result)
    assert after["status"] == FAIL and before["status"] == UNKNOWN
    assert asked_time(result, "창덕궁")
    assert result["summary"]["verdict"] == "infeasible"


def test_gap_too_small_by_estimate_is_asked(facts, policy):
    """대조군: 이동 0분이면 되고 예상 이동시간으로는 안 되니 위반 대신 시각을 묻는다."""
    add_legs(facts)
    result = judge(three_palaces(palace_start="12:50"), facts, policy)
    assert [m["status"] for m in moves(result)] == [UNKNOWN, UNKNOWN]
    assert "창덕궁에 몇 시에 도착할 예정인가요?" in questions(result)
    assert result["summary"]["verdict"] == "undetermined"


# 시각을 묻는 경우 (대조군)

@pytest.mark.parametrize("stops, names", [
    # 앞뒤 장소도 시각이 빠지면 가장 이른 시각을 정할 수 없다.
    ([{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60}, {"place": "창덕궁", "dwell_minutes": 90},
      {"place": "경복궁", "dwell_minutes": 60}], ["창덕궁", "경복궁"]),
    # 체류시간을 말하지 않았으면 다음 장소로 언제 나서는지 모른다.
    (three_palaces(changdeok_dwell=None)["stops"], ["창덕궁"]),
    # 저장된 이동시간은 지하철 하한선이라 언제 닿는지 확정할 수 없다.
    ([{"place": "서울시립미술관 서소문본관", "start": "11:00", "dwell_minutes": 60}, {"place": "경복궁", "dwell_minutes": 60}],
     ["경복궁"]),
], ids=["two-missing-in-a-row", "missing-dwell", "lower-bound-travel"])
def test_missing_time_is_asked_when_no_window_can_be_found(facts, policy, stops, names):
    add_legs(facts)
    result = judge({"date": WEDNESDAY, "stops": stops}, facts, policy)
    assert all(asked_time(result, name) for name in names)


def test_train_after_a_missing_last_stop_is_asked(facts, policy):
    """대조군: 열차에 닿는지는 시각이 빠진 마지막 장소를 나서는 시각에 달렸다."""
    add_legs(facts)
    result = judge({"date": WEDNESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                                 {"place": "창덕궁", "dwell_minutes": 90}],
                    "hard_constraints": [ktx("19:00")]}, facts, policy)
    assert asked_time(result, "창덕궁")
    assert result["hard_constraints"][0]["policy"]["status"] == UNKNOWN


# 길찾기

def test_routes_ask_both_sides_of_a_missing_stop(tmap, facts, policy):
    """앞 구간은 11:00, 뒤 구간은 11:30 입장 + 90분 = 13:00 출발로 묻는다."""
    fake = tmap(transit(1200), places={"덕수궁": poi(("덕수궁", 37.566, 126.975)),
                                       "창덕궁": poi(("창덕궁", 37.579, 126.991)),
                                       "경복궁": poi(("경복궁", 37.578, 126.977))})
    itinerary = three_palaces()
    legs, misses = live_legs(itinerary, "key", facts, policy)
    assert misses == {}
    assert [b["searchDttm"] for u, b in fake.sent if "/transit/" in u] == ["202611181100", "202611181300"]
    result = judge(itinerary, {**facts, "legs": {**facts["legs"], **legs}}, policy)
    assert [m["status"] for m in moves(result)] == [PASS, PASS]
