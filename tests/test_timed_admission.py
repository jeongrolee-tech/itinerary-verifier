"""종묘 월 · 목 · 금 회차 입장을 본다. 도착 뒤 첫 한국어 회차부터 체류를 센다."""

import pytest

from helpers import check
from verdict import FAIL, PASS, UNKNOWN, judge

JONGMYO = "종묘"
THURSDAY_OCT = "2026-10-08"  # 10~2월 회차는 09:20 부터 16:20 까지다.
THURSDAY_MAY = "2026-05-07"  # 3~9월에는 16:40 회차가 더 있다.


def visit(date, start, dwell=None, then=None):
    stop = {"place": JONGMYO, "start": start}
    if dwell is not None:
        stop["dwell_minutes"] = dwell
    stops = [stop] + ([{"place": then[0], "start": then[1], "dwell_minutes": 30}] if then else [])
    return {"date": date, "stops": stops}


def test_waits_for_the_next_session(facts, policy):
    """10:00 에 와서 10:20 회차에 들어가고 체류는 그때부터 센다."""
    a = check(judge(visit(THURSDAY_OCT, "10:00", 40), facts, policy), "ADMISSION_NOT_POSSIBLE", JONGMYO)
    assert a["status"] == PASS and a["evidence"]["entry_at"] == "10:20"
    assert "20분 기다린다" in a["notice"]


@pytest.mark.parametrize("date, start, status, entry_at", [
    (THURSDAY_OCT, "16:30", FAIL, None),  # 마지막 한국어 회차 16:20 뒤고 외국어 회차는 더 이르다.
    (THURSDAY_OCT, "16:20", UNKNOWN, None),  # 마지막 회차 정각에 들어가는지 공식 안내가 말하지 않는다.
    (THURSDAY_MAY, "16:30", PASS, "16:40"),  # 대조군: 3~9월에는 16:40 회차가 더 있다.
    ("2026-10-09", "10:00", PASS, "10:00"),  # 대조군: 한글날 같은 공휴일은 일반관람이라 회차를 기다리지 않는다.
])
def test_admission_follows_the_sessions(facts, policy, date, start, status, entry_at):
    a = check(judge(visit(date, start, 40), facts, policy), "ADMISSION_NOT_POSSIBLE", JONGMYO)
    assert a["status"] == status
    assert a.get("evidence", {}).get("entry_at") == entry_at
    if status == UNKNOWN:
        assert a["unknown_reason"] == "AT_LAST_ADMISSION"


@pytest.mark.parametrize("dwell, status, reason", [
    (50, PASS, None),  # 해설 50분 정도 안의 체류는 회차가 끝나기 전에 마친다.
    (60, UNKNOWN, "DWELL_BEYOND_TOUR"),  # 일반관람이 안 되는 날 50분을 넘겨 머물 수 있는지 공식 안내가 없다.
    (None, UNKNOWN, "NO_USER_DWELL"),  # 체류를 말하지 않으면 50분으로 채우지 않고 묻는다.
])
def test_dwell_on_a_session_day(facts, policy, dwell, status, reason):
    d = check(judge(visit(THURSDAY_OCT, "10:00", dwell), facts, policy), "DWELL_NOT_COMPLETABLE", JONGMYO)
    assert d["status"] == status and d.get("unknown_reason") == reason
    if reason:
        assert d["how_to_resolve"]["user"]


def test_next_stop_counts_from_the_session(facts, policy):
    """10:20 회차부터 40분이라 11:00 에 나오니 10:55 창덕궁은 이동 0분이어도 늦다."""
    late = judge(visit(THURSDAY_OCT, "10:00", 40, then=("창덕궁", "10:55")), facts, policy)
    assert check(late, "INSUFFICIENT_TRAVEL_TIME", "종묘 → 창덕궁")["status"] == FAIL
    ok = judge(visit(THURSDAY_OCT, "10:00", 40, then=("창덕궁", "11:30")), facts, policy)
    assert check(ok, "INSUFFICIENT_TRAVEL_TIME", "종묘 → 창덕궁")["status"] != FAIL
