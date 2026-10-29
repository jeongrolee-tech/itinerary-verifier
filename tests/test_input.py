"""판정 코어는 추출을 안 거친 입력도 받아 틀린 입력을 unknown 으로 돌려준다."""

from verdict import judge

THURSDAY = "2026-10-08"


def reasons(out):
    return [c.get("unknown_reason") for c in out["checks"]]


def test_unreadable_start_time_is_asked_not_crashed(facts, policy):
    """휴무일은 날짜로 확인하고 입장 · 체류만 시각을 다시 묻는다."""
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
    # 열차는 판정하지 못해도 꼭 지킬 일정이라 남기고, 광장시장 검사는 계속한다.
    (hc,) = out["hard_constraints"]
    assert hc["event"]["place"] == "서울역" and hc["policy"]["unknown_reason"] == "INVALID_TIME"
    assert any(c["type"] == "CLOSED_DAY" and c["target"] == "광장시장" for c in out["checks"])


def test_missing_start_time_is_asked(facts, policy):
    """대조군: 시각도 체류시간도 없으면 시간대를 계산할 수 없어 시각을 묻는다."""
    it = {"date": THURSDAY, "stops": [{"place": "경복궁"}]}
    assert reasons(judge(it, facts, policy)) == [None, "MISSING_START_TIME", "MISSING_START_TIME"]
