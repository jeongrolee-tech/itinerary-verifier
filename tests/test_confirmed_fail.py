"""
확정 fail 의 반례 — 2차 피드백 1번.

fail 은 사용자가 말한 제약과 확인된 근거만으로 이미 충돌할 때만 낸다.
권장 여유(buffer), 시스템 기본 체류시간, 사용자가 말하지 않은 이동수단에 따라
결론이 달라지면 fail 이 아니라 unknown 이다. 가능하다고 단정해서도 안 되지만, 불가능하다고 단정해서도 안 된다.

반례마다 대조군을 둔다. 반례만 있으면 fail 을 아예 안 내는 구현도 통과한다.

xfail(strict=True) 는 "지금 코드가 틀렸고, 고칠 작업이 정해져 있다" 는 표시다.
고치면 XPASS 가 되어 스위트가 실패하므로, 그 작업에서 표시를 지워야 한다.
"""

import pytest

from verdict import FAIL, UNKNOWN, judge

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


def ktx_at(time):
    return {"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": time,
            "buffer_rule": "rail_boarding", "source": "user_stated"}


# ── A. 권장 여유 ──────────────────────────────────────────────────────
# 미술관 → 경복궁 주행시간 하한선 6.5분, 권장 여유 10분.
# 14:00 출발이면 주행만으로 14:06.5 도착이고, 권장 여유까지 더해야 14:16.5 가 된다.

def museum_then_palace(palace_start, mode=None):
    it = {"date": THURSDAY, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": palace_start, "dwell_minutes": 60},
    ]}
    if mode:
        it["travel_mode"] = mode
    return it


def test_buffer_shortfall_alone_is_not_fail(facts, policy):
    """14:15 시작 — 권장 여유를 못 채운 것이지, 15분 안에 못 간다는 증명이 아니다."""
    c = travel(judge(museum_then_palace("14:15"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "BUFFER_NOT_MET"


def test_ride_alone_late_is_fail(facts, policy):
    """대조군: 14:05 시작, 지하철로 간다고 말했다 — 주행시간 하한선만으로 이미 늦는다."""
    c = travel(judge(museum_then_palace("14:05", mode="metro"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == FAIL


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

def palace_then_market(market_start, mode=None):
    it = {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "10:00"},
        {"place": MARKET, "start": market_start},
    ]}
    if mode:
        it["travel_mode"] = mode
    return it


def test_default_dwell_alone_is_not_fail(facts, policy):
    """10:30 광장시장 — 경복궁에서 몇 분 머무를지 모른다. 체류 검사도 같은 이유로 unknown 이다."""
    c = travel(judge(palace_then_market("10:30"), facts, policy), PALACE, MARKET)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "NO_USER_DWELL"


def test_late_even_with_zero_dwell_is_fail(facts, policy):
    """대조군: 10:03 광장시장, 지하철로 간다 — 경복궁에 들어가자마자 나와도 주행만으로 10:04.5 다."""
    c = travel(judge(palace_then_market("10:03", mode="metro"), facts, policy), PALACE, MARKET)
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

def market_then_ktx(time, mode=None):
    it = {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00"}],
          "hard_constraints": [ktx_at(time)]}
    if mode:
        it["travel_mode"] = mode
    return it


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


def test_train_late_even_with_zero_dwell_is_fail(facts, policy):
    """대조군: 19:05 KTX, 지하철로 간다 — 바로 나와도 주행만으로 19:07 이다."""
    assert train(judge(market_then_ktx("19:05", mode="metro"), facts, policy))["status"] == FAIL


# ── E. 이동수단 ──────────────────────────────────────────────────────
# 이동시간 근거는 지하철 주행시간뿐이다. "지하철로는 늦는다" 는 "못 간다" 가 아니다.

def test_subway_late_without_stated_mode_is_not_fail(facts, policy):
    """14:05 경복궁 — 지하철로는 늦지만, 택시로는 갈 수 있을 수도 있다. 수단을 묻는다."""
    c = travel(judge(museum_then_palace("14:05"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "NO_USER_MODE"


def test_subway_late_without_stated_mode_is_not_train_fail(facts, policy):
    """19:05 KTX — 열차 조건도 같다. 확정하지 않되 중요도는 낮추지 않는다."""
    p = train(judge(market_then_ktx("19:05"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "NO_USER_MODE"
    assert p["severity"] == "blocking"


def test_other_stated_mode_has_no_travel_time(facts, policy):
    """택시로 간다고 했다 — 가진 것은 지하철 주행시간뿐이라 그 수단의 이동시간은 모른다."""
    c = travel(judge(museum_then_palace("14:05", mode="taxi"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "UNVERIFIED_TRAVEL_TIME"


def test_leg_without_mode_does_not_count_as_stated(facts, policy):
    """구간 데이터에 mode 가 없어도, 사용자가 수단을 말하지 않았으면 fail 이 아니다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"].pop("mode")
    c = travel(judge(museum_then_palace("14:05"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN


@pytest.mark.parametrize("mode", [None, "metro", "taxi"])
def test_late_even_with_zero_travel_is_fail_regardless_of_mode(facts, policy, mode):
    """13:00 미술관에서 한 시간 보고 13:50 경복궁 — 순간이동을 해도 늦는다."""
    c = travel(judge(museum_then_palace("13:50", mode=mode), facts, policy), MUSEUM, PALACE)
    assert c["status"] == FAIL


def test_train_late_even_with_zero_travel_is_fail(facts, policy):
    """광장시장 19:00 에 한 시간 먹고 19:30 KTX — 수단을 말하지 않았어도 이미 늦다."""
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
