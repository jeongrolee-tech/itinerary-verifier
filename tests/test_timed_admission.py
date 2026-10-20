"""
종묘 회차 입장 — 월 · 목 · 금은 정해진 회차에만 들어가 해설사를 따라 50분 정도 관람한다(일반관람 불가).

회차 시간표를 연속 운영시간으로 바꾸지 않는다. 도착 뒤 첫 한국어 회차에 들어가고, 기다리는 것은 개장 전 도착과
같이 그 회차 시각부터 체류와 다음 이동을 센다. 근거: 공식 페이지 R702000000 (facts 의 종묘 admission).
"""

from verdict import FAIL, PASS, UNKNOWN, judge

JONGMYO = "종묘"
THURSDAY_OCT = "2026-10-08"  # 10~2월 회차 09:20 … 16:20
THURSDAY_MAY = "2026-05-07"  # 3~9월 회차 09:20 … 16:20, 16:40


def check_of(result, type_, target=JONGMYO):
    hits = [c for c in result["checks"] if c["type"] == type_ and c["target"] == target]
    assert len(hits) == 1
    return hits[0]


def visit(date, start, dwell=None, then=None):
    stop = {"place": JONGMYO, "start": start}
    if dwell is not None:
        stop["dwell_minutes"] = dwell
    stops = [stop] + ([{"place": then[0], "start": then[1], "dwell_minutes": 30}] if then else [])
    return {"date": date, "stops": stops}


def test_waits_for_the_next_session(facts, policy):
    """목요일 10:00 도착 — 10:20 회차에 들어간다. 체류는 10:20 부터 센다."""
    a = check_of(judge(visit(THURSDAY_OCT, "10:00", 40), facts, policy), "ADMISSION_NOT_POSSIBLE")
    assert a["status"] == PASS and a["evidence"]["entry_at"] == "10:20"
    assert "20분 기다린다" in a["notice"]


def test_after_the_last_session_is_fail(facts, policy):
    """10월 목요일 16:30 — 마지막 한국어 회차 16:20 뒤다. 외국어 회차(가장 늦은 영어 16:00)도 그보다 이르다."""
    a = check_of(judge(visit(THURSDAY_OCT, "16:30", 40), facts, policy), "ADMISSION_NOT_POSSIBLE")
    assert a["status"] == FAIL


def test_exactly_at_the_last_session_is_unknown(facts, policy):
    """16:20 정각 — 마지막 회차에 들어가는지 공식 안내가 말하지 않는다(입장마감 정각과 같은 규칙)."""
    a = check_of(judge(visit(THURSDAY_OCT, "16:20", 40), facts, policy), "ADMISSION_NOT_POSSIBLE")
    assert a["status"] == UNKNOWN and a["unknown_reason"] == "AT_LAST_ADMISSION"


def test_summer_has_a_later_session(facts, policy):
    """대조군: 5월 목요일 16:30 — 3~9월에는 16:40 회차가 더 있다."""
    a = check_of(judge(visit(THURSDAY_MAY, "16:30", 40), facts, policy), "ADMISSION_NOT_POSSIBLE")
    assert a["status"] == PASS and a["evidence"]["entry_at"] == "16:40"


def test_holiday_is_general_admission(facts, policy):
    """대조군: 2026-10-09 한글날(금) — 공휴일은 일반관람이라 회차를 기다리지 않는다."""
    a = check_of(judge(visit("2026-10-09", "10:00", 40), facts, policy), "ADMISSION_NOT_POSSIBLE")
    assert a["status"] == PASS and a["evidence"]["entry_at"] == "10:00"


def test_dwell_within_the_tour_passes(facts, policy):
    """해설 50분 정도 안의 체류는 회차가 끝나기 전에 마친다."""
    assert check_of(judge(visit(THURSDAY_OCT, "10:00", 50), facts, policy), "DWELL_NOT_COMPLETABLE")["status"] == PASS


def test_dwell_beyond_the_tour_is_unknown(facts, policy):
    """한 시간 — 일반관람이 안 되는 날이라 50분 정도를 넘겨 머물 수 있는지 공식 안내가 없다(정답 문제 T13)."""
    d = check_of(judge(visit(THURSDAY_OCT, "10:00", 60), facts, policy), "DWELL_NOT_COMPLETABLE")
    assert d["status"] == UNKNOWN and d["unknown_reason"] == "DWELL_BEYOND_TOUR"
    assert d["how_to_resolve"]["user"]


def test_dwell_not_stated_is_asked_as_usual(facts, policy):
    """체류를 말하지 않으면 다른 장소와 같이 묻는다 — '50분 정도' 로 채우지 않는다."""
    d = check_of(judge(visit(THURSDAY_OCT, "10:00"), facts, policy), "DWELL_NOT_COMPLETABLE")
    assert d["status"] == UNKNOWN and d["unknown_reason"] == "NO_USER_DWELL"


def test_next_stop_counts_from_the_session(facts, policy):
    """10:00 도착 · 10:20 회차 · 40분 → 11:00 에 나온다. 11:00 에 시작하는 창덕궁은 이동 0분이어야 닿는다 —
    10:00 부터 세면 10:40 에 나와 20분 여유가 있는 것처럼 보인다. 10:55 시작이면 순간이동해도 늦다."""
    late = judge(visit(THURSDAY_OCT, "10:00", 40, then=("창덕궁", "10:55")), facts, policy)
    assert check_of(late, "INSUFFICIENT_TRAVEL_TIME", "종묘 → 창덕궁")["status"] == FAIL
    ok = judge(visit(THURSDAY_OCT, "10:00", 40, then=("창덕궁", "11:30")), facts, policy)
    assert check_of(ok, "INSUFFICIENT_TRAVEL_TIME", "종묘 → 창덕궁")["status"] != FAIL
