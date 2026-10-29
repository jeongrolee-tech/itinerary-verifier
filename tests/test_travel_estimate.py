"""판정할 때 받은 예상 이동시간으로 판정 규칙이 맞게 나오는지 본다."""

import pytest

from helpers import DEOKSU, MARKET, MUSEUM, PALACE, THURSDAY, estimate, ktx, train, travel, with_taxi
from verdict import FAIL, PASS, UNKNOWN, judge


def t01(palace_start="15:00"):
    return {"date": THURSDAY, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": palace_start, "dwell_minutes": 120}]}


def market_then_ktx(ktx_time):
    return {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00", "dwell_minutes": 60}],
            "hard_constraints": [ktx(ktx_time)]}


# A. 예상 시간에 들어오면 장소가 여럿이어도 feasible

def test_two_stops_feasible_with_estimate(facts, policy):
    """14:00 + 25분 + 여유 10분 = 14:35 라 15:00 시작 전이다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(25)
    out = judge(t01(), facts, policy)
    assert travel(out, MUSEUM, PALACE)["status"] == PASS
    assert out["summary"]["verdict"] == "feasible"


def test_three_stops_feasible_with_estimates(facts, policy):
    facts["legs"][f"{DEOKSU}|{MUSEUM}"] = estimate(10)
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(25)
    it = {"date": THURSDAY, "stops": [
        {"place": DEOKSU, "start": "10:00", "dwell_minutes": 60},
        {"place": MUSEUM, "start": "11:30", "dwell_minutes": 60},
        {"place": PALACE, "start": "13:30", "dwell_minutes": 90}]}
    assert judge(it, facts, policy)["summary"]["verdict"] == "feasible"


# B. 빠듯하거나 늦어 보여도 예상으로는 확정하지 않고 묻는다

@pytest.mark.parametrize("minutes, reason", [
    (55, "BUFFER_NOT_MET"),  # 14:55 도착이라 15:00 전이지만 권장 여유 10분이 모자란다.
    (65, "LATE_BY_ESTIMATE"),  # 15:05 도착이지만 앞 열차를 타면 더 일찍 닿을 수 있다.
])
def test_tight_or_late_estimate_is_asked_not_failed(facts, policy, minutes, reason):
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(minutes)
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == reason


def test_late_by_estimate_is_not_train_fail(facts, policy):
    """20:00 에 나와 40분이면 20:40 이라 20:30 KTX 를 놓칠 듯하지만 확정하지 않는다."""
    facts["legs"][f"{MARKET}|서울역"] = estimate(40)
    p = train(judge(market_then_ktx("20:30"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert p["severity"] == "blocking"


# C. 대조군: 확정할 수 있는 것은 확정한다

def test_late_even_with_zero_travel_is_still_fail(facts, policy):
    """14:00 에 나오는데 13:50 경복궁이면 이동 0분이어도 늦는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(25)
    c = travel(judge(t01("13:50"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == FAIL


# D. 대중교통으로 안 되면 택시로
# 되는 수단이 하나라도 있으면 pass 다. 대신 택시로 가야 한다고 알린다.

def test_taxi_makes_it_when_transit_is_late(facts, policy):
    """대중교통 65분은 15:05 라 늦고, 택시 20분은 여유를 더해도 14:30 이라 닿는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    out = judge(t01(), facts, policy)
    c = travel(out, MUSEUM, PALACE)
    assert c["status"] == PASS
    assert "택시" in c["notice"] and "10,000원" in c["notice"] and "당일 교통" in c["notice"]
    assert out["summary"]["verdict"] == "feasible"


def test_taxi_also_late_is_told(facts, policy):
    """택시로도 늦으면 대중교통 결과를 그대로 두고 택시로도 어렵다고 알린다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 70)
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert "택시" in (c.get("notice") or "")


def test_taxi_makes_the_train(facts, policy):
    """택시 12분이면 20:12 도착이라 20:30 KTX 의 여유 15분 기준인 20:15 전이다."""
    facts["legs"][f"{MARKET}|서울역"] = with_taxi(40, 12)
    p = train(judge(market_then_ktx("20:30"), facts, policy))
    assert p["status"] == PASS
    assert "택시" in p["notice"]


def test_transit_ok_needs_no_taxi(facts, policy):
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(25, 10)
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == PASS and not c.get("notice")


# D-2. 예상으로 낸 통과는 무엇에 기댔는지 적는다

def test_pass_by_estimate_tells_its_basis(facts, policy):
    """예상으로 낸 통과가 사실처럼 보이지 않게 예상과 여유를 함께 적는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = {**estimate(25), "source": "api_runtime", "method": "tmap_prediction"}
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == PASS
    assert c["detail"] == "TMAP 예상 대중교통 25분에 권장 여유 10분을 더해도 15:00까지 닿는다"


def test_train_pass_tells_its_basis(facts, policy):
    """열차 여유는 15분이다. 20:00 + 10분 = 20:10 이라 20:15 전에 닿는다."""
    facts["legs"][f"{MARKET}|서울역"] = {**estimate(10), "source": "api_runtime", "method": "tmap_prediction"}
    p = train(judge(market_then_ktx("20:30"), facts, policy))
    assert p["status"] == PASS and p["detail"] == "TMAP 예상 대중교통 10분에 권장 여유 15분을 더해도 열차 출발(20:30) 전에 닿는다"


# E. 구역 일정
# 구역은 구간 값이 있어도 대표 지점으로 판정하지 않고 어디로 가는지 묻는다.

def palace_then_area(area_start):
    return {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "10:00", "dwell_minutes": 60},
        {"place": "명동", "start": area_start, "dwell_minutes": 60, "scope": "area",
         "scope_note": "쇼핑할 가게가 특정되지 않았다"}]}


def test_travel_to_an_area_is_not_judged(facts, policy):
    """명동까지 15분짜리 구간이 있어도 명동 어디로 가는지 모르니 통과시키지 않는다."""
    facts["legs"][f"{PALACE}|명동"] = estimate(15)
    c = travel(judge(palace_then_area("12:00"), facts, policy), PALACE, "명동")
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "PLACE_NOT_RESOLVED"


def test_area_still_fails_when_late_with_zero_travel(facts, policy):
    """대조군: 11:00 에 나오는데 10:30 명동이면 명동 어디로 가든 늦는다."""
    facts["legs"][f"{PALACE}|명동"] = estimate(15)
    assert travel(judge(palace_then_area("10:30"), facts, policy), PALACE, "명동")["status"] == FAIL


def test_train_from_an_area_is_not_judged(facts, policy):
    """마지막 일정이 구역이면 어디서 출발하는지 몰라 열차 조건도 판정하지 않는다."""
    facts["legs"]["명동|서울역"] = estimate(15)
    it = {"date": THURSDAY, "stops": [{"place": "명동", "start": "18:00", "dwell_minutes": 60, "scope": "area"}],
          "hard_constraints": [ktx("20:30")]}
    p = train(judge(it, facts, policy))
    assert p["status"] == UNKNOWN and p["unknown_reason"] == "PLACE_NOT_RESOLVED"
    assert p["severity"] == "blocking"
