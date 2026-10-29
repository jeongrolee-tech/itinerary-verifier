"""fail 은 사용자가 말한 제약과 확인된 근거만으로 낸다. 그 반례와 대조군이다."""

import pytest

from verdict import FAIL, PASS, UNKNOWN, judge

MUSEUM = "서울시립미술관 서소문본관"
PALACE = "경복궁"
MARKET = "광장시장"
THURSDAY = "2026-10-08"  # 미술관과 경복궁이 모두 열고 공휴일이 아니다.


def closed_day(result, name):
    return next(c for c in result["checks"] if c["type"] == "CLOSED_DAY" and c["target"] == name)


@pytest.mark.parametrize("date, name, why", [
    ("2026-11-17", PALACE, "매주 화요일"),
    ("2026-10-06", "창덕궁", "월요일 정기휴일이 공휴일과 겹쳐 밀린 날"),  # 10월 5일 대체공휴일에서 하루 밀렸다.
    ("2026-01-01", MUSEUM, "매년 1월 1일"),
])
def test_closed_day_tells_why_and_what_to_do(facts, policy, date, name, why):
    """휴무 위반에는 왜 쉬는지와 무엇을 하면 되는지 알리고, 판정은 위반 그대로 둔다."""
    c = closed_day(judge({"date": date, "stops": [{"place": name, "start": "14:00", "dwell_minutes": 60}]},
                         facts, policy), name)
    assert c["status"] == FAIL
    assert c["notice"] == (f"이날은 {name} 휴무일이에요({why}). 다른 날로 옮기거나 다른 곳을 고르세요. "
                           "입장하지 않고 밖에서만 머물 거라면 그렇게 알려 주세요")


def travel(result, frm, to):
    hits = [c for c in result["checks"]
            if c["type"] == "INSUFFICIENT_TRAVEL_TIME" and c["target"] == f"{frm} → {to}"]
    assert len(hits) == 1, f"{frm} → {to} 이동 검사가 {len(hits)}개다"
    return hits[0]


def closed_day(result, name):
    hits = [c for c in result["checks"] if c["type"] == "CLOSED_DAY" and c["target"] == name]
    assert len(hits) == 1
    return hits[0]


def train(result):
    assert len(result["hard_constraints"]) == 1
    return result["hard_constraints"][0]["policy"]


def admission(result, name):
    hits = [c for c in result["checks"]
            if c["type"] == "ADMISSION_NOT_POSSIBLE" and c["target"] == name]
    assert len(hits) == 1
    return hits[0]


def ktx_at(time):
    return {"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": time,
            "buffer_rule": "rail_boarding", "source": "user_stated"}


# A. 권장 여유
# 미술관을 14:00 에 나오면 하한선 6.5분으로 14:06.5, 여유 10분까지 더하면 14:16.5 다.

def museum_then_palace(palace_start):
    return {"date": THURSDAY, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": palace_start, "dwell_minutes": 60},
    ]}


def test_buffer_shortfall_alone_is_not_fail(facts, policy):
    """14:15 시작은 권장 여유만 모자랄 뿐 15분 안에 못 간다는 증명이 아니다."""
    c = travel(judge(museum_then_palace("14:15"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "BUFFER_NOT_MET"


def test_ride_alone_late_is_not_buffer_shortfall(facts, policy):
    """14:05 시작은 하한선만으로 늦지만 택시로는 될 수 있어 fail 이 아니다."""
    c = travel(judge(museum_then_palace("14:05"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "LATE_BY_LOWER_BOUND"


@pytest.mark.parametrize("buffer", [10, 30, 120])
def test_raising_travel_buffer_never_creates_fail(facts, policy, buffer):
    """권장 여유는 사실이 아니라 설정이라 늘려도 fail 이 생기지 않는다."""
    policy["buffer_minutes"]["transit_leg"] = buffer
    c = travel(judge(museum_then_palace("14:30"), facts, policy), MUSEUM, PALACE)
    assert c["status"] != FAIL


@pytest.mark.parametrize("buffer", [15, 60, 120])
def test_raising_rail_buffer_never_creates_fail(facts, policy, buffer):
    policy["buffer_minutes"]["rail_boarding"] = buffer
    it = {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00", "dwell_minutes": 30}],
          "hard_constraints": [ktx_at("19:45")]}
    assert train(judge(it, facts, policy))["status"] != FAIL


# B. 시스템 기본 체류시간 (이동)
# 경복궁 체류를 말하지 않으면 기본값 90분이다. 광장시장까지 하한선은 4.5분이다.

def palace_then_market(market_start):
    return {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "10:00"},
        {"place": MARKET, "start": market_start},
    ]}


def test_default_dwell_alone_is_not_fail(facts, policy):
    """10:30 광장시장은 경복궁에서 얼마나 머물지 몰라 판정할 수 없다."""
    c = travel(judge(palace_then_market("10:30"), facts, policy), PALACE, MARKET)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "NO_USER_DWELL"


def test_late_even_with_zero_dwell_and_zero_travel_is_fail(facts, policy):
    """대조군: 09:55 광장시장은 경복궁에 10:00 에 들어가자마자 나와도 늦는다."""
    c = travel(judge(palace_then_market("09:55"), facts, policy), PALACE, MARKET)
    assert c["status"] == FAIL


@pytest.mark.parametrize("palace_default", [90, 240, 400])
def test_raising_default_dwell_never_creates_fail(facts, policy, palace_default):
    """기본 체류시간도 사실이 아니라 설정이라 늘려도 fail 이 생기지 않는다."""
    policy["default_dwell_minutes"]["palace"] = palace_default
    c = travel(judge(palace_then_market("12:00"), facts, policy), PALACE, MARKET)
    assert c["status"] != FAIL


# D. 시스템 기본 체류시간 (열차 조건)
# 광장시장 체류를 말하지 않으면 기본값 60분이다. 서울역까지 하한선은 7.0분이다.

def market_then_ktx(time):
    return {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00"}],
            "hard_constraints": [ktx_at(time)]}


def test_default_dwell_alone_is_not_train_fail(facts, policy):
    """19:20 KTX 는 광장시장에서 바로 나오면 19:07 에 닿는다."""
    p = train(judge(market_then_ktx("19:20"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "NO_USER_DWELL"
    assert p["severity"] == "blocking"  # 정보가 부족해도 열차 조건의 중요도는 낮추지 않는다.


@pytest.mark.parametrize("market_default", [60, 200, 400])
def test_raising_default_dwell_never_creates_train_fail(facts, policy, market_default):
    policy["default_dwell_minutes"]["market_meal"] = market_default
    assert train(judge(market_then_ktx("20:30"), facts, policy))["status"] != FAIL


def test_train_late_even_with_zero_dwell_and_zero_travel_is_fail(facts, policy):
    """대조군: 19:00 KTX 는 광장시장에 19:00 에 들어가자마자 나와도 이미 떠났다."""
    assert train(judge(market_then_ktx("19:00"), facts, policy))["status"] == FAIL


# E. 이동수단
# 근거가 지하철 주행시간뿐이라 지하철로 늦어도 못 간다고 확정하지 않는다.

def test_subway_late_with_zero_dwell_is_not_fail(facts, policy):
    """10:03 광장시장은 바로 나와도 주행만으로 10:04.5 지만 택시로는 될 수 있다."""
    c = travel(judge(palace_then_market("10:03"), facts, policy), PALACE, MARKET)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "LATE_BY_LOWER_BOUND"


def test_subway_late_is_not_train_fail(facts, policy):
    """19:05 KTX 는 바로 나와도 주행만으로 19:07 이지만 확정하지 않는다."""
    p = train(judge(market_then_ktx("19:05"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "LATE_BY_LOWER_BOUND"
    assert p["severity"] == "blocking"


def test_late_even_with_zero_travel_is_fail(facts, policy):
    """13:00 미술관에서 한 시간 보고 13:50 경복궁이면 이동 0분이어도 늦는다."""
    c = travel(judge(museum_then_palace("13:50"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == FAIL


def test_train_late_even_with_zero_travel_is_fail(facts, policy):
    """광장시장 19:00 에 한 시간 먹고 19:30 KTX 면 이동 0분이어도 늦는다."""
    it = {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00", "dwell_minutes": 60}],
          "hard_constraints": [ktx_at("19:30")]}
    assert train(judge(it, facts, policy))["status"] == FAIL


# C. 고정일 휴관
# 미술관 규칙은 "휴관 1월 1일, 매주 월요일 / 월요일이 공휴일인 경우 정상 개관" 이다.

def museum_on(date):
    return {"date": date, "stops": [{"place": MUSEUM, "start": "11:00", "dwell_minutes": 60}]}


def test_fixed_date_closure(facts, policy):
    """2026-01-01 은 목요일이지만 휴관일이다."""
    assert closed_day(judge(museum_on("2026-01-01"), facts, policy), MUSEUM)["status"] == FAIL


def test_day_after_fixed_closure_is_open(facts, policy):
    """대조군: 2026-01-02 금요일은 휴관 규칙 어디에도 걸리지 않는다."""
    assert closed_day(judge(museum_on("2026-01-02"), facts, policy), MUSEUM)["status"] != FAIL


def test_fixed_date_on_holiday_monday_is_rule_conflict(facts, policy):
    """2029-01-01 은 신정 월요일이라 두 규칙이 부딪치고, 공식 페이지에 우선순위가 없다."""
    # 근거가 2026년까지라 기간을 늘리지 않으면 EVIDENCE_EXPIRED 가 된다.
    facts["places"][MUSEUM]["closed_days"]["valid_until"] = "2029-12-31"
    facts["holidays"]["valid_until"] = "2029-12-31"
    facts["holidays"]["dates"].append("2029-01-01")
    c = closed_day(judge(museum_on("2029-01-01"), facts, policy), MUSEUM)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "RULE_CONFLICT"


def test_no_exception_rule_closes_even_on_a_holiday(facts, policy):
    """예외 규칙이 없으면 대체공휴일인 10월 5일 월요일도 쉬고 6일로 밀지 않는다."""
    facts["places"][MUSEUM]["closed_days"]["exception_rule"] = "none"
    assert closed_day(judge(museum_on("2026-10-05"), facts, policy), MUSEUM)["status"] == FAIL
    assert closed_day(judge(museum_on("2026-10-06"), facts, policy), MUSEUM)["status"] == PASS


# F. LLM 이 추정한 입력
# 추출이 시각을 짐작하거나 연도를 채우면 출처를 inferred 로 표시한다.

def palace_at(start, source="explicit"):
    return {"date": THURSDAY, "weekday_stated": "THU", "stops": [
        {"place": PALACE, "start": start, "start_source": source, "dwell_minutes": 60}]}


def test_inferred_start_does_not_confirm_fail(facts, policy):
    """추정한 17:30 으로는 입장마감 뒤라고 확정하지 않고 시각을 묻는다."""
    c = admission(judge(palace_at("17:30", "inferred"), facts, policy), PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "INFERRED_INPUT"
    assert "17:30에 도착하는 것이 맞나요" in c["how_to_resolve"]["user"]


def test_explicit_start_still_confirms_fail(facts, policy):
    """대조군: 사용자가 17시 30분이라고 적었으면 입장마감 17:00 위반이 확정된다."""
    assert admission(judge(palace_at("17:30"), facts, policy), PALACE)["status"] == FAIL


def test_inferred_start_does_not_confirm_pass(facts, policy):
    """추정한 10:00 으로는 입장할 수 있다고도 단정하지 않는다."""
    out = judge(palace_at("10:00", "inferred"), facts, policy)
    assert admission(out, PALACE)["status"] == UNKNOWN
    assert out["summary"]["verdict"] == "undetermined"


def test_inferred_year_without_weekday_does_not_confirm_fail(facts, policy):
    """요일 없이 연도를 채운 10월 6일은 휴궁일이어도 확정하지 않는다."""
    it = {"date": "2026-10-06", "date_source": "inferred",
          "stops": [{"place": "창덕궁", "start": "10:00", "dwell_minutes": 60}]}
    c = closed_day(judge(it, facts, policy), "창덕궁")
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "INFERRED_INPUT"


def test_inferred_year_confirmed_by_stated_weekday(facts, policy):
    """대조군: 10월 6일 화요일이라고 말했으니 날짜가 확인돼 휴궁이 확정된다."""
    it = {"date": "2026-10-06", "date_source": "inferred", "weekday_stated": "TUE",
          "stops": [{"place": "창덕궁", "start": "10:00", "dwell_minutes": 60}]}
    assert closed_day(judge(it, facts, policy), "창덕궁")["status"] == FAIL


def test_inferred_start_does_not_confirm_train_fail(facts, policy):
    it = {"date": THURSDAY, "stops": [
        {"place": MARKET, "start": "19:00", "start_source": "inferred", "dwell_minutes": 60}],
          "hard_constraints": [ktx_at("19:30")]}
    p = train(judge(it, facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "INFERRED_INPUT"
    assert p["severity"] == "blocking"


def test_mismatched_weekday_is_not_infeasible(facts, policy):
    """10월 8일 수요일이라 했지만 8일은 목요일이다. 어느 쪽인지 몰라 보류한다."""
    it = {"date": THURSDAY, "date_source": "inferred", "weekday_stated": "WED",
          "stops": [{"place": PALACE, "start": "10:00", "dwell_minutes": 60}]}
    out = judge(it, facts, policy)
    assert out["summary"]["verdict"] == "undetermined"
    assert out["checks"][0]["status"] == UNKNOWN
    assert out["checks"][0]["unknown_reason"] == "MISMATCHED_WEEKDAY"


# G. 개장 전 도착
# 개장 전에 오면 기다렸다 들어가고, 체류와 다음 이동은 개장 시각부터 센다.

def test_early_arrival_is_not_fail(facts, policy):
    """창덕궁 08:30 도착은 09:00 개장까지 기다리면 되니 안내만 붙인다."""
    it = {"date": THURSDAY, "stops": [{"place": "창덕궁", "start": "08:30", "dwell_minutes": 60}]}
    out = judge(it, facts, policy)
    c = admission(out, "창덕궁")
    assert c["status"] == PASS
    assert "기다려야" in c["notice"]
    assert out["summary"]["verdict"] == "feasible"


def test_early_arrival_shifts_departure(facts, policy):
    """경복궁 08:30 에 와도 09:00 에 들어가 10:00 에 나오니 창덕궁 09:50 에 못 간다."""
    it = {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "08:30", "dwell_minutes": 60},
        {"place": "창덕궁", "start": "09:50", "dwell_minutes": 60}]}
    assert travel(judge(it, facts, policy), PALACE, "창덕궁")["status"] == FAIL


def test_arriving_by_next_opening_is_enough(facts, policy):
    """미술관은 10:00 에 열어 09:45 계획이어도 10:00 까지만 가면 된다."""
    it = {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "08:30", "dwell_minutes": 50},
        {"place": MUSEUM, "start": "09:45", "dwell_minutes": 60}]}
    c = travel(judge(it, facts, policy), PALACE, MUSEUM)
    # 09:56 도착이라 10:00 전에 닿지만 권장 여유 10분이 모자란다.
    assert c["unknown_reason"] == "BUFFER_NOT_MET"


# H. 필수 조건의 종류
# 열차 · 항공편은 출발 시각이라 그때 닿으면 놓치고, "까지 도착"은 그때 닿아도 된다.

def market_until(time, type_, place="김포공항"):
    rule = "flight_boarding" if type_ == "FLIGHT_DEPARTURE" else "transit_leg"  # extract.BUFFER_RULES 와 같다.
    return {"date": THURSDAY, "stops": [{"place": MARKET, "start": "18:00", "dwell_minutes": 60}],
            "hard_constraints": [{"id": "HC1", "type": type_, "place": place, "time": time,
                                  "buffer_rule": rule, "source": "user_stated"}]}


def to_airport(facts, minutes=20):
    """광장시장에서 김포공항까지 테스트용 예상치 구간을 넣는다."""
    facts["legs"][f"{MARKET}|김포공항"] = {"minutes": minutes, "mode": "transit", "is_lower_bound": False,
                                       "source": "synthetic_fixture", "method": "synthetic_fixture"}


def test_flight_message_does_not_say_train(facts, policy):
    p = train(judge(market_until("18:30", "FLIGHT_DEPARTURE"), facts, policy))
    assert p["status"] == FAIL
    assert "항공편 출발" in p["detail"] and "열차" not in p["detail"]


def test_leaving_at_departure_time_misses_the_flight(facts, policy):
    """19:00 에 나와 19:00 항공편이면 이동 0분이어도 놓친다."""
    assert train(judge(market_until("19:00", "FLIGHT_DEPARTURE"), facts, policy))["status"] == FAIL


def test_leaving_at_deadline_is_not_late_for_arrive_by(facts, policy):
    """19:00 에 나와 19:00 까지 도착이면 이동 0분일 때 제시간이라 fail 이 아니다."""
    assert train(judge(market_until("19:00", "ARRIVE_BY"), facts, policy))["status"] != FAIL


def test_flight_is_never_passed_without_a_boarding_rule(facts, policy):
    """19:20 에 공항에 닿아도 탑승 수속 기준이 없어 pass 를 내지 않는다."""
    to_airport(facts)
    p = train(judge(market_until("21:00", "FLIGHT_DEPARTURE"), facts, policy))
    assert p["status"] == UNKNOWN and p["unknown_reason"] == "NO_BUFFER_RULE"
    assert p["severity"] == "blocking"


def test_arrive_by_still_passes_with_its_rule(facts, policy):
    """대조군: 21:00 까지 도착은 권장 여유 기준이 있어 pass 다."""
    to_airport(facts)
    assert train(judge(market_until("21:00", "ARRIVE_BY"), facts, policy))["status"] == PASS


# I. 입장마감 정각
# 궁궐 표는 입장마감 시각만 적어 정각에 들어가는지 알 수 없다.

def one_stop(place, date, start):
    return {"date": date, "stops": [{"place": place, "start": start, "dwell_minutes": 30}]}


def test_exact_last_admission_without_rule_is_unknown(facts, policy):
    """한글날 17:00 종묘는 실제로 막힌 후기가 있지만 공식 표로는 알 수 없어 보류한다."""
    a = admission(judge(one_stop("종묘", "2026-10-09", "17:00"), facts, policy), "종묘")
    assert a["status"] == UNKNOWN and a["unknown_reason"] == "AT_LAST_ADMISSION"
    assert a["how_to_resolve"]["user"]


def test_exact_last_admission_at_palace_is_unknown(facts, policy):
    """10월 경복궁 입장마감은 17:00 이다."""
    a = admission(judge(one_stop(PALACE, THURSDAY, "17:00"), facts, policy), PALACE)
    assert a["status"] == UNKNOWN and a["unknown_reason"] == "AT_LAST_ADMISSION"


def test_one_minute_before_last_admission_passes(facts, policy):
    assert admission(judge(one_stop(PALACE, THURSDAY, "16:59"), facts, policy), PALACE)["status"] == PASS


def test_one_minute_after_last_admission_fails(facts, policy):
    assert admission(judge(one_stop(PALACE, THURSDAY, "17:01"), facts, policy), PALACE)["status"] == FAIL


def test_until_rule_lets_exact_time_in(facts, policy):
    """대조군: 미술관은 공식 안내가 '관람 종료 1시간 전까지 입장'이라 정각도 들어간다."""
    assert admission(judge(one_stop(MUSEUM, "2026-10-14", "19:00"), facts, policy), MUSEUM)["status"] == PASS
