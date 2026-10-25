"""
시각이 빠진 장소에 들어갈 시간대 — 2026-10-25 사용자 결정.

사용자는 장소마다 시각을 정해 두지 않는다("그다음 창덕궁에서 한 시간 반"). 그래서 시각이 빠진 장소는 앞 장소를
나와 바로 갔을 때(이동시간 + 권장 여유 10분)를 가장 이른 입장 시각으로 본다. 앞 장소가 없으면 문 여는 시각이다.
그때 입장 · 체류 · 앞뒤 이동이 다 되면 통과로 보고, 묻지 않고 들어갈 시간대를 알린다. 이동 0분으로 쳐도 안 되는
검사만 위반이다. 그 밖에는 지금처럼 시각을 묻는다 — 앞뒤 장소도 시각이 빠졌거나, 체류시간을 말하지 않았거나,
쓸 수 있는 이동시간이 없거나, 필수 일정이 걸린 경우다.

기대 결과는 판정 코드를 돌리기 전에 facts.json 운영시간과 테스트용 이동시간으로 손으로 계산했다.
xfail(strict=True) 는 "지금 코드가 틀렸고, 고칠 작업이 정해져 있다" 는 표시다. 고치면 XPASS 가 되어 스위트가
실패하므로, 그 작업에서 표시를 지운다.
"""

import pytest

from report import questions, timeline
from routes import live_legs
from test_routes import FakeTmap, poi, transit  # noqa: F401 — tmap 픽스처가 쓰는 가짜 TMAP
from test_routes import tmap  # noqa: F401 — pytest 픽스처
from test_travel_estimate import estimate
from verdict import FAIL, PASS, UNKNOWN, judge

# 11월 18일 수요일 — 덕수궁 · 창덕궁(월요일 휴무)과 경복궁(화요일 휴무)이 모두 연다. 11월 운영시간은
# 덕수궁 09:00~21:00(입장마감 20:00), 창덕궁 09:00~17:30(16:30), 경복궁 09:00~17:00(16:00)
WEDNESDAY = "2026-11-18"
THURSDAY = "2026-10-08"  # 경복궁 10월 09:00~18:00(입장마감 17:00)
PLACE = ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE", "DWELL_NOT_COMPLETABLE")
pending = pytest.mark.xfail(strict=True, reason="시각이 빠진 장소의 시간대 계산을 아직 하지 않는다")


def add_legs(facts):
    """테스트용 이동시간 — 덕수궁 → 창덕궁 20분, 창덕궁 → 경복궁 15분. 실제 이동시간이 아니다."""
    facts["legs"]["덕수궁|창덕궁"] = estimate(20)
    facts["legs"]["창덕궁|경복궁"] = estimate(15)


def three_palaces(changdeok_dwell=90, palace_start="15:30", deoksu_dwell=60):
    """N02 와 같은 모양 — 덕수궁 10:00, 창덕궁(시각 없음), 경복궁."""
    return {"date": WEDNESDAY, "stops": [
        {"place": "덕수궁", "start": "10:00", "dwell_minutes": deoksu_dwell},
        {"place": "창덕궁", "dwell_minutes": changdeok_dwell},
        {"place": "경복궁", "start": palace_start, "dwell_minutes": 60}]}


def place_checks(result, name):
    return {c["type"]: c for c in result["checks"] if c["target"] == name}


def moves(result):
    return [c for c in result["checks"] if c["type"] == "INSUFFICIENT_TRAVEL_TIME" and "(없음)" not in c["target"]]


# ── 시간대를 찾는다 ─────────────────────────────────────────────────

def test_gap_is_found_and_told(facts, policy):
    """T36 — 덕수궁을 11:00 에 나와 20분 + 여유 10분이면 창덕궁에 11:30. 90분 보고 13:00 에 나와 15분 + 10분이면
    13:25 라 경복궁 15:30 전이다. 늦어도 15:30 − 10 − 15 − 90 = 13:35 에는 들어가야 한다. 묻지 않는다."""
    add_legs(facts)
    result = judge(three_palaces(), facts, policy)
    c = place_checks(result, "창덕궁")
    assert [c[t]["status"] for t in PLACE] == [PASS, PASS, PASS]
    assert [m["status"] for m in moves(result)] == [PASS, PASS]
    assert "창덕궁에는 11:30~13:35 사이에 들어가면 돼요" in c["ADMISSION_NOT_POSSIBLE"].get("notice", "")
    assert questions(result) == []
    assert result["summary"]["verdict"] == "feasible"


def test_first_stop_starts_at_opening(facts, policy):
    """앞 장소가 없으면 문 여는 09:00 부터다. 10:30 에 나와 15분 + 10분이면 10:55 라 경복궁 13:00 전이다.
    늦어도 13:00 − 10 − 15 − 90 = 11:05."""
    add_legs(facts)
    result = judge({"date": WEDNESDAY, "stops": [{"place": "창덕궁", "dwell_minutes": 90},
                                                 {"place": "경복궁", "start": "13:00", "dwell_minutes": 60}]},
                   facts, policy)
    assert "창덕궁에는 09:00~11:05 사이에 들어가면 돼요" in \
        place_checks(result, "창덕궁")["ADMISSION_NOT_POSSIBLE"].get("notice", "")
    assert result["summary"]["verdict"] == "feasible"


def test_last_stop_is_bounded_by_its_hours(facts, policy):
    """뒤 장소가 없으면 운영시간만 본다. 11:30 부터, 17:30 폐장 전에 90분을 마치려면 늦어도 16:00."""
    add_legs(facts)
    result = judge({"date": WEDNESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                                 {"place": "창덕궁", "dwell_minutes": 90}]}, facts, policy)
    assert "창덕궁에는 11:30~16:00 사이에 들어가면 돼요" in \
        place_checks(result, "창덕궁")["ADMISSION_NOT_POSSIBLE"].get("notice", "")
    assert result["summary"]["verdict"] == "feasible"


def test_only_stop_is_bounded_by_its_hours(facts, policy):
    """장소가 하나뿐이어도 같다. 10월 8일 경복궁 한 시간 — 입장마감 17:00 정각은 공식 안내가 말하지 않아 그 전이다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "경복궁", "dwell_minutes": 60}]}, facts, policy)
    assert "경복궁에는 09:00부터 17:00 전에 들어가면 돼요" in \
        place_checks(result, "경복궁")["ADMISSION_NOT_POSSIBLE"].get("notice", "")
    assert result["summary"]["verdict"] == "feasible"


def test_timeline_shows_the_window(facts, policy):
    """화면 일정 줄의 시각 칸에 시간대를 적는다 — 시각을 말하지 않은 장소라 빈칸이었다."""
    add_legs(facts)
    itinerary = three_palaces()
    (row,) = [r for r in timeline(itinerary, judge(itinerary, facts, policy)) if r["title"] == "창덕궁"]
    assert row["time"] == "11:30~13:35"


# ── 시간대가 없다 ───────────────────────────────────────────────────

def test_no_gap_even_without_travel_is_a_violation(facts, policy):
    """T37 — 덕수궁 10:00 부터 세 시간이면 13:00 에 나온다. 이동 0분으로 쳐도 창덕궁 두 시간이면 15:00 이라
    경복궁 14:00 에 늦는다 — 창덕궁 → 경복궁 위반. 창덕궁 입장 · 체류와 앞 이동은 시각을 몰라 지금처럼 묻는다."""
    add_legs(facts)
    result = judge(three_palaces(changdeok_dwell=120, palace_start="14:00", deoksu_dwell=180), facts, policy)
    before, after = moves(result)
    assert after["status"] == FAIL and before["status"] == UNKNOWN
    assert place_checks(result, "창덕궁")["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"
    assert result["summary"]["verdict"] == "infeasible"


def test_gap_too_small_by_estimate_is_asked(facts, policy):
    """대조군: 이동 0분이면 된다(11:00 에 들어가 12:30 에 나오면 경복궁 12:50 전). 예상 이동시간으로는 안 된다
    (11:30 에 들어가면 13:00 에 나온다). 예상치로는 위반을 확정하지 않으니 지금처럼 시각을 묻는다."""
    add_legs(facts)
    result = judge(three_palaces(palace_start="12:50"), facts, policy)
    assert [m["status"] for m in moves(result)] == [UNKNOWN, UNKNOWN]
    assert "창덕궁에 몇 시에 도착할 예정인가요?" in questions(result)
    assert result["summary"]["verdict"] == "undetermined"


# ── 지금처럼 묻는다 ──────────────────────────────────────────────────

def test_two_missing_in_a_row_are_asked(facts, policy):
    """대조군: 앞뒤 장소도 시각이 빠지면 가장 이른 시각을 정할 수 없다."""
    add_legs(facts)
    result = judge({"date": WEDNESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                                 {"place": "창덕궁", "dwell_minutes": 90},
                                                 {"place": "경복궁", "dwell_minutes": 60}]}, facts, policy)
    for name in ("창덕궁", "경복궁"):
        assert place_checks(result, name)["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"


def test_missing_dwell_is_asked(facts, policy):
    """대조군: 체류시간을 말하지 않았으면 다음 장소로 언제 나서는지 몰라 시각을 묻는다."""
    add_legs(facts)
    result = judge(three_palaces(changdeok_dwell=None), facts, policy)
    assert place_checks(result, "창덕궁")["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"


def test_lower_bound_travel_is_asked(facts, policy):
    """대조군: 저장해 둔 미술관 → 경복궁은 지하철 최소 이동시간(6.5분)이라 언제 닿는지 확정할 수 없다."""
    result = judge({"date": WEDNESDAY, "stops": [{"place": "서울시립미술관 서소문본관", "start": "11:00",
                                                  "dwell_minutes": 60},
                                                 {"place": "경복궁", "dwell_minutes": 60}]}, facts, policy)
    assert place_checks(result, "경복궁")["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"


def test_train_after_a_missing_last_stop_is_asked(facts, policy):
    """대조군: 마지막 장소의 시각이 빠지고 그 뒤에 열차가 있으면 열차에 닿는지는 그 장소를 나서는 시각에 달렸다."""
    add_legs(facts)
    result = judge({"date": WEDNESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                                 {"place": "창덕궁", "dwell_minutes": 90}],
                    "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "19:00",
                                          "buffer_rule": "rail_boarding", "source": "user_stated"}]}, facts, policy)
    assert place_checks(result, "창덕궁")["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"
    assert result["hard_constraints"][0]["policy"]["status"] == UNKNOWN


# ── 길찾기 ──────────────────────────────────────────────────────────

@pending
def test_routes_ask_both_sides_of_a_missing_stop(tmap, facts, policy):
    """시각이 빠진 장소 앞뒤 구간도 TMAP 에 묻는다. 앞 구간은 덕수궁을 나서는 11:00, 뒤 구간은 가장 이르게
    들어가는 11:30(11:00 + 20분 + 여유 10분)에 90분을 더한 13:00 출발이다 — 받은 출발 시각에만 그 값을 쓴다."""
    fake = tmap(transit(1200), places={"덕수궁": poi(("덕수궁", 37.566, 126.975)),
                                       "창덕궁": poi(("창덕궁", 37.579, 126.991)),
                                       "경복궁": poi(("경복궁", 37.578, 126.977))})
    itinerary = three_palaces()
    legs, misses = live_legs(itinerary, "key", facts, policy)
    assert misses == {}
    assert [b["searchDttm"] for u, b in fake.sent if "/transit/" in u] == ["202611181100", "202611181300"]
    result = judge(itinerary, {**facts, "legs": {**facts["legs"], **legs}}, policy)
    assert [m["status"] for m in moves(result)] == [PASS, PASS]
