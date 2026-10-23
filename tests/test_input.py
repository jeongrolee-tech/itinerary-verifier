"""
입력 검사 — 판정을 시작하기 전에 입력이 있는지, 읽을 수 있는 형식인지 본다.

빠졌거나 형식이 틀린 입력은 오류로 멈추지 않고 unknown 으로 돌려준다. 입력이 부족한 것도
검증 결과의 일부다. 추출(extract.py)은 형식을 검증하지만, 판정 코어는 tests.json 이나
사용자가 고친 일정(recheck.py)처럼 추출을 거치지 않은 입력도 받는다.
"""

from verdict import judge

THURSDAY = "2026-10-08"


def reasons(out):
    return [c.get("unknown_reason") for c in out["checks"]]


def test_unreadable_start_time_is_asked_not_crashed(facts, policy):
    """휴무일은 시각 없이 날짜로 확인하고(목요일 경복궁은 연다), 입장 · 체류는 시각을 다시 묻는다(2026-10-23 새 평가)."""
    it = {"date": THURSDAY, "stops": [{"place": "경복궁", "start": "1:00 PM", "dwell_minutes": 60}]}
    out = judge(it, facts, policy)
    assert out["summary"]["verdict"] == "undetermined"
    assert reasons(out) == [None, "INVALID_TIME", "INVALID_TIME"]


def test_unreadable_train_time_is_asked_not_crashed(facts, policy):
    it = {"date": THURSDAY, "stops": [{"place": "광장시장", "start": "19:00", "dwell_minutes": 30}],
          "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "8:30pm",
                                "buffer_rule": "rail_boarding", "source": "user_stated"}]}
    out = judge(it, facts, policy)
    assert out["summary"]["verdict"] == "undetermined"
    # 열차 시각만 묻고, 광장시장 검사는 계속한다(3차 피드백 — 확인할 수 있는 검사는 멈추지 않는다).
    # 열차는 필수 일정에 그대로 남는다 — 판정하지 못해도 사용자가 꼭 지킨다고 한 것이다
    (hc,) = out["hard_constraints"]
    assert hc["event"]["place"] == "서울역" and hc["policy"]["unknown_reason"] == "INVALID_TIME"
    assert any(c["type"] == "CLOSED_DAY" and c["target"] == "광장시장" for c in out["checks"])


def test_missing_start_time_is_asked(facts, policy):
    """대조군: 시각이 아예 없으면 MISSING_START_TIME 으로 묻는다. 휴무일은 그대로 확인한다."""
    it = {"date": THURSDAY, "stops": [{"place": "경복궁", "dwell_minutes": 60}]}
    assert reasons(judge(it, facts, policy)) == [None, "MISSING_START_TIME", "MISSING_START_TIME"]
