"""
확정 fail 의 반례 — 2차 피드백 1번.

fail 은 사용자가 말한 제약과 확인된 근거만으로 이미 충돌할 때만 낸다.
권장 여유(buffer), 시스템 기본 체류시간, 지하철 하나의 이동시간(택시로는 될 수 있다), LLM 이
추정한 입력에 따라 결론이 달라지면 fail 이 아니라 unknown 이다. 가능하다고 단정해서도 안 되지만, 불가능하다고 단정해서도 안 된다.

반례마다 대조군을 둔다. 반례만 있으면 fail 을 아예 안 내는 구현도 통과한다.

xfail(strict=True) 는 "지금 코드가 틀렸고, 고칠 작업이 정해져 있다" 는 표시다.
고치면 XPASS 가 되어 스위트가 실패하므로, 그 작업에서 표시를 지워야 한다.
"""

import pytest

from verdict import FAIL, PASS, UNKNOWN, judge

MUSEUM = "서울시립미술관 서소문본관"
PALACE = "경복궁"
MARKET = "광장시장"
THURSDAY = "2026-10-08"  # 미술관·경복궁 모두 개관, 공휴일 아님


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


# ── A. 권장 여유 ──────────────────────────────────────────────────────
# 미술관 → 경복궁 주행시간 하한선 6.5분, 권장 여유 10분.
# 14:00 출발이면 주행만으로 14:06.5 도착이고, 권장 여유까지 더해야 14:16.5 가 된다.

def museum_then_palace(palace_start):
    return {"date": THURSDAY, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": palace_start, "dwell_minutes": 60},
    ]}


def test_buffer_shortfall_alone_is_not_fail(facts, policy):
    """14:15 시작 — 권장 여유를 못 채운 것이지, 15분 안에 못 간다는 증명이 아니다."""
    c = travel(judge(museum_then_palace("14:15"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "BUFFER_NOT_MET"


def test_ride_alone_late_is_not_buffer_shortfall(facts, policy):
    """14:05 시작 — 권장 여유가 아니라 주행시간 하한선만으로 이미 늦는다. 지하철로는 못 가지만
    택시로는 될 수 있어 fail 은 아니다. 대조군은 E 의 이동 0분 fail 이다."""
    c = travel(judge(museum_then_palace("14:05"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "LATE_BY_LOWER_BOUND"


@pytest.mark.parametrize("buffer", [10, 30, 120])
def test_raising_travel_buffer_never_creates_fail(facts, policy, buffer):
    """권장 여유만 늘었다고 확정 불가능으로 바뀌지 않는다. 권장 여유는 사실이 아니라 설정이다."""
    policy["buffer_minutes"]["transit_leg"] = buffer
    c = travel(judge(museum_then_palace("14:30"), facts, policy), MUSEUM, PALACE)
    assert c["status"] != FAIL


@pytest.mark.parametrize("buffer", [15, 60, 120])
def test_raising_rail_buffer_never_creates_fail(facts, policy, buffer):
    """열차 조건은 이미 권장 여유 부족을 BUFFER_NOT_MET(unknown)으로 낸다. 회귀 방지용."""
    policy["buffer_minutes"]["rail_boarding"] = buffer
    it = {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00", "dwell_minutes": 30}],
          "hard_constraints": [ktx_at("19:45")]}
    assert train(judge(it, facts, policy))["status"] != FAIL


# ── B. 시스템 기본 체류시간 (이동) ────────────────────────────────────
# 경복궁 체류를 말하지 않으면 기본값 90분이 들어간다. 경복궁 → 광장시장 하한선 4.5분.

def palace_then_market(market_start):
    return {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "10:00"},
        {"place": MARKET, "start": market_start},
    ]}


def test_default_dwell_alone_is_not_fail(facts, policy):
    """10:30 광장시장 — 경복궁에서 몇 분 머무를지 모른다. 체류 검사도 같은 이유로 unknown 이다."""
    c = travel(judge(palace_then_market("10:30"), facts, policy), PALACE, MARKET)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "NO_USER_DWELL"


def test_late_even_with_zero_dwell_and_zero_travel_is_fail(facts, policy):
    """대조군: 09:55 광장시장 — 경복궁에 10:00 에 들어가자마자 나와도, 이동 0분이어도 늦는다."""
    c = travel(judge(palace_then_market("09:55"), facts, policy), PALACE, MARKET)
    assert c["status"] == FAIL


@pytest.mark.parametrize("palace_default", [90, 240, 400])
def test_raising_default_dwell_never_creates_fail(facts, policy, palace_default):
    """
    시스템 기본 체류시간만으로 확정 실패를 만들지 않는다. 기본값도 사실이 아니라 설정이다.

    1-2 전에는 기본값 D 의 ±50% 가 둘 다 늦을 때 fail 을 냈다.
    10:00 + D/2 + 4.5 + 10 > 12:00 이 되는 D > 211 부터 fail 이 됐다.
    """
    policy["default_dwell_minutes"]["palace"] = palace_default
    c = travel(judge(palace_then_market("12:00"), facts, policy), PALACE, MARKET)
    assert c["status"] != FAIL


# ── D. 시스템 기본 체류시간 (열차 조건) ───────────────────────────────
# 광장시장 체류를 말하지 않으면 기본값 60분. 광장시장 → 서울역 하한선 7.0분.

def market_then_ktx(time):
    return {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00"}],
            "hard_constraints": [ktx_at(time)]}


def test_default_dwell_alone_is_not_train_fail(facts, policy):
    """19:20 KTX — 광장시장에서 바로 나오면 19:07 에 닿는다."""
    p = train(judge(market_then_ktx("19:20"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "NO_USER_DWELL"
    assert p["severity"] == "blocking"  # 정보가 부족해도 열차 조건의 중요도는 낮추지 않는다


@pytest.mark.parametrize("market_default", [60, 200, 400])
def test_raising_default_dwell_never_creates_train_fail(facts, policy, market_default):
    """열차 조건도 기본 체류시간만으로 확정 실패를 만들지 않는다."""
    policy["default_dwell_minutes"]["market_meal"] = market_default
    assert train(judge(market_then_ktx("20:30"), facts, policy))["status"] != FAIL


def test_train_late_even_with_zero_dwell_and_zero_travel_is_fail(facts, policy):
    """대조군: 19:00 KTX — 광장시장에 19:00 에 들어가자마자 나와도 열차는 이미 떠났다."""
    assert train(judge(market_then_ktx("19:00"), facts, policy))["status"] == FAIL


# ── E. 이동수단 ──────────────────────────────────────────────────────
# 이동수단은 보지 않는다. 되는 수단이 하나라도 있으면 된다.
# 이동시간 근거는 지하철 주행시간뿐이다. "지하철로는 늦는다" 는 "못 간다" 가 아니다.

def test_subway_late_with_zero_dwell_is_not_fail(facts, policy):
    """10:03 광장시장 — 경복궁에 들어가자마자 나와도 주행만으로 10:04.5 다. 지하철로는 못 가지만
    택시로는 될 수 있어 확정하지 않는다."""
    c = travel(judge(palace_then_market("10:03"), facts, policy), PALACE, MARKET)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "LATE_BY_LOWER_BOUND"


def test_subway_late_is_not_train_fail(facts, policy):
    """19:05 KTX — 바로 나와도 주행만으로 19:07 이다. 열차 조건도 확정하지 않되 중요도는 낮추지 않는다."""
    p = train(judge(market_then_ktx("19:05"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "LATE_BY_LOWER_BOUND"
    assert p["severity"] == "blocking"


def test_late_even_with_zero_travel_is_fail(facts, policy):
    """13:00 미술관에서 한 시간 보고 13:50 경복궁 — 순간이동을 해도 늦는다."""
    c = travel(judge(museum_then_palace("13:50"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == FAIL


def test_train_late_even_with_zero_travel_is_fail(facts, policy):
    """광장시장 19:00 에 한 시간 먹고 19:30 KTX — 이동 0분이어도 이미 늦다."""
    it = {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00", "dwell_minutes": 60}],
          "hard_constraints": [ktx_at("19:30")]}
    assert train(judge(it, facts, policy))["status"] == FAIL


# ── C. 고정일 휴관 ────────────────────────────────────────────────────
# facts.json 미술관 rule_text: "휴관 1월 1일, 매주 월요일 / 월요일이 공휴일인 경우 정상 개관"

def museum_on(date):
    return {"date": date, "stops": [{"place": MUSEUM, "start": "11:00", "dwell_minutes": 60}]}


def test_fixed_date_closure(facts, policy):
    """2026-01-01(목) — 월요일이 아니지만 휴관일이다."""
    assert closed_day(judge(museum_on("2026-01-01"), facts, policy), MUSEUM)["status"] == FAIL


def test_day_after_fixed_closure_is_open(facts, policy):
    """대조군: 2026-01-02(금) — 휴관 규칙 어디에도 걸리지 않는다."""
    assert closed_day(judge(museum_on("2026-01-02"), facts, policy), MUSEUM)["status"] != FAIL


def test_fixed_date_on_holiday_monday_is_rule_conflict(facts, policy):
    """
    2029-01-01(월, 신정) — "휴관 1월 1일" 은 휴관, "월요일이 공휴일인 경우 정상 개관" 은 개관이다.
    공식 페이지에 우선순위가 없으므로 어느 쪽으로도 확정하지 않는다.

    지금 근거는 2026-12-31 까지라 2029년은 원래 EVIDENCE_EXPIRED 다. 근거를 갱신한
    상황을 가정해 적용 기간과 공휴일만 늘렸다. 기간만 늘려도 충돌이 확정되지 않는지 본다.
    """
    facts["places"][MUSEUM]["closed_days"]["valid_until"] = "2029-12-31"
    facts["holidays"]["valid_until"] = "2029-12-31"
    facts["holidays"]["dates"].append("2029-01-01")
    c = closed_day(judge(museum_on("2029-01-01"), facts, policy), MUSEUM)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "RULE_CONFLICT"


def test_no_exception_rule_closes_even_on_a_holiday(facts, policy):
    """
    예외 규칙이 없으면 정기휴일이 공휴일이어도 쉬고, 다음 날로 밀지도 않는다. 지금 이 규칙을 쓰는 장소는
    없지만, 전에는 궁궐처럼 다음 날로 밀어서 문을 연 날을 휴무로 확정했다. 2026-10-05(월)은 개천절 대체공휴일이다.
    """
    facts["places"][MUSEUM]["closed_days"]["exception_rule"] = "none"
    assert closed_day(judge(museum_on("2026-10-05"), facts, policy), MUSEUM)["status"] == FAIL
    assert closed_day(judge(museum_on("2026-10-06"), facts, policy), MUSEUM)["status"] == PASS


# ── F. LLM 이 추정한 입력 ─────────────────────────────────────────────
# 추출 단계는 "점심 먹고" 같은 표현에서 시각을 짐작하면 start_source=inferred,
# 연도가 없으면 가장 가까운 미래로 채우고 date_source=inferred 로 표시한다.

def palace_at(start, source="explicit"):
    return {"date": THURSDAY, "weekday_stated": "THU", "stops": [
        {"place": PALACE, "start": start, "start_source": source, "dwell_minutes": 60}]}


def test_inferred_start_does_not_confirm_fail(facts, policy):
    """추정한 17:30 으로는 "입장마감 이후" 를 확정하지 않는다. 그 시각이 맞는지 묻는다."""
    c = admission(judge(palace_at("17:30", "inferred"), facts, policy), PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "INFERRED_INPUT"
    assert "17:30에 도착하는 것이 맞나요" in c["how_to_resolve"]["user"]


def test_explicit_start_still_confirms_fail(facts, policy):
    """대조군: 사용자가 17시 30분이라고 적었으면 입장마감(17:00) 위반이 확정된다."""
    assert admission(judge(palace_at("17:30"), facts, policy), PALACE)["status"] == FAIL


def test_inferred_start_does_not_confirm_pass(facts, policy):
    """추정한 10:00 으로 "입장 가능" 을 단정하지도 않는다. 가능하다고 단정해서도 안 된다."""
    out = judge(palace_at("10:00", "inferred"), facts, policy)
    assert admission(out, PALACE)["status"] == UNKNOWN
    assert out["summary"]["verdict"] == "undetermined"


def test_inferred_year_without_weekday_does_not_confirm_fail(facts, policy):
    """
    "10월 6일 창덕궁" — 연도를 2026 으로 채우면 휴궁일(10/5 대체공휴일에서 밀림)이다.
    하지만 요일을 말하지 않았으니 2026년이 맞는지 사용자 말로 확인되지 않았다.
    """
    it = {"date": "2026-10-06", "date_source": "inferred",
          "stops": [{"place": "창덕궁", "start": "10:00", "dwell_minutes": 60}]}
    c = closed_day(judge(it, facts, policy), "창덕궁")
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "INFERRED_INPUT"


def test_inferred_year_confirmed_by_stated_weekday(facts, policy):
    """대조군: "10월 6일 화요일" — 말한 요일이 맞으니 날짜는 확인된 것이다. 휴궁이 확정된다."""
    it = {"date": "2026-10-06", "date_source": "inferred", "weekday_stated": "TUE",
          "stops": [{"place": "창덕궁", "start": "10:00", "dwell_minutes": 60}]}
    assert closed_day(judge(it, facts, policy), "창덕궁")["status"] == FAIL


def test_inferred_start_does_not_confirm_train_fail(facts, policy):
    """추정한 시각으로 열차를 놓친다고 단정하지 않는다. 중요도는 그대로 둔다."""
    it = {"date": THURSDAY, "stops": [
        {"place": MARKET, "start": "19:00", "start_source": "inferred", "dwell_minutes": 60}],
          "hard_constraints": [ktx_at("19:30")]}
    p = train(judge(it, facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "INFERRED_INPUT"
    assert p["severity"] == "blocking"


def test_mismatched_weekday_is_not_infeasible(facts, policy):
    """
    "10월 8일 수요일" — 2026-10-08 은 목요일이다. 7일 수요일인지 8일 목요일인지 모를 뿐,
    일정이 불가능하다는 증거는 아니다. 판정을 보류하고 어느 쪽이 맞는지 묻는다.
    """
    it = {"date": THURSDAY, "date_source": "inferred", "weekday_stated": "WED",
          "stops": [{"place": PALACE, "start": "10:00", "dwell_minutes": 60}]}
    out = judge(it, facts, policy)
    assert out["summary"]["verdict"] == "undetermined"
    assert out["checks"][0]["status"] == UNKNOWN
    assert out["checks"][0]["unknown_reason"] == "MISMATCHED_WEEKDAY"


# ── G. 개장 전 도착 ──────────────────────────────────────────────────
# 개장 전에 오면 기다렸다가 개장 시각에 들어간다. 불가능이 아니다.
# 대신 체류와 다음 이동은 개장 시각부터 센다.

def test_early_arrival_is_not_fail(facts, policy):
    """창덕궁 08:30 — 09:00 개장까지 30분 기다리면 된다. 안내만 붙인다."""
    it = {"date": THURSDAY, "stops": [{"place": "창덕궁", "start": "08:30", "dwell_minutes": 60}]}
    out = judge(it, facts, policy)
    c = admission(out, "창덕궁")
    assert c["status"] == PASS
    assert "기다려야" in c["notice"]
    assert out["summary"]["verdict"] == "feasible"


def test_early_arrival_shifts_departure(facts, policy):
    """경복궁 08:30 에 와서 한 시간 — 09:00 에 들어가 10:00 에 나온다. 창덕궁 09:50 에는 못 간다."""
    it = {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "08:30", "dwell_minutes": 60},
        {"place": "창덕궁", "start": "09:50", "dwell_minutes": 60}]}
    assert travel(judge(it, facts, policy), PALACE, "창덕궁")["status"] == FAIL


def test_arriving_by_next_opening_is_enough(facts, policy):
    """
    미술관을 09:45 로 계획했어도 10:00 에 연다. 경복궁에서 09:50 에 나와도(지하철 09:56 도착)
    10:00 전이면 계획과 똑같이 들어간다. 계획 시각이 아니라 입장 가능 시각까지 가면 된다.
    """
    it = {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "08:30", "dwell_minutes": 50},
        {"place": MUSEUM, "start": "09:45", "dwell_minutes": 60}]}
    c = travel(judge(it, facts, policy), PALACE, MUSEUM)
    # 계획 시각(09:45)까지 가야 한다고 보면 '늦는다' 가 나온다. 10:00 까지면 닿되 권장 여유 10분만 모자란다
    assert c["unknown_reason"] == "BUFFER_NOT_MET"


# ── H. 필수 조건의 종류 ───────────────────────────────────────────────
# 열차·항공편은 출발 시각이라 그 시각에 도착하면 놓친 것이다.
# "까지 도착" 은 그 시각에 닿으면 제시간이다.

def market_until(time, type_, place="김포공항"):
    rule = "flight_boarding" if type_ == "FLIGHT_DEPARTURE" else "transit_leg"  # extract.BUFFER_RULES 와 같다
    return {"date": THURSDAY, "stops": [{"place": MARKET, "start": "18:00", "dwell_minutes": 60}],
            "hard_constraints": [{"id": "HC1", "type": type_, "place": place, "time": time,
                                  "buffer_rule": rule, "source": "user_stated"}]}


def to_airport(facts, minutes=20):
    """광장시장 → 김포공항 구간을 판정할 때 받는 예상치 모양으로 넣는다. 값은 테스트용으로 정했다."""
    facts["legs"][f"{MARKET}|김포공항"] = {"minutes": minutes, "mode": "transit", "is_lower_bound": False,
                                       "source": "synthetic_fixture", "method": "synthetic_fixture"}


def test_flight_message_does_not_say_train(facts, policy):
    """항공편 조건인데 "열차는 … 출발한다" 고 적으면 안 된다."""
    p = train(judge(market_until("18:30", "FLIGHT_DEPARTURE"), facts, policy))
    assert p["status"] == FAIL
    assert "항공편 출발" in p["detail"] and "열차" not in p["detail"]


def test_leaving_at_departure_time_misses_the_flight(facts, policy):
    """19:00 에 나와서 19:00 항공편 — 이동 0분이어도 이미 놓쳤다."""
    assert train(judge(market_until("19:00", "FLIGHT_DEPARTURE"), facts, policy))["status"] == FAIL


def test_leaving_at_deadline_is_not_late_for_arrive_by(facts, policy):
    """19:00 에 나와서 "19:00 까지 도착" — 이동 0분이면 제시간이라 그것만으로 fail 이 아니다."""
    assert train(judge(market_until("19:00", "ARRIVE_BY"), facts, policy))["status"] != FAIL


def test_flight_is_never_passed_without_a_boarding_rule(facts, policy):
    """19:00 에 나와 20분이면 19:20 공항 — 21:00 항공편보다 100분 일러도 pass 가 아니다. 탑승 수속 ·
    보안 검색 기준을 확보하지 않았다. 중요도는 그대로다."""
    to_airport(facts)
    p = train(judge(market_until("21:00", "FLIGHT_DEPARTURE"), facts, policy))
    assert p["status"] == UNKNOWN and p["unknown_reason"] == "NO_BUFFER_RULE"
    assert p["severity"] == "blocking"


def test_arrive_by_still_passes_with_its_rule(facts, policy):
    """대조군: '21:00 까지 도착' 은 권장 여유 기준(transit_leg)이 있어 지금처럼 pass 다."""
    to_airport(facts)
    assert train(judge(market_until("21:00", "ARRIVE_BY"), facts, policy))["status"] == PASS
