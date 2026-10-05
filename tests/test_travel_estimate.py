"""
길찾기 예상 이동시간 — 판정할 때 TMAP 으로 받은 값을 넣었을 때의 규칙.

실제로 쓸 때는 판정하는 순간 TMAP 을 불러 문 앞에서 문 앞까지의 예상 시간을 받는다. 그 값은
약관상 저장할 수 없어서, 여기서는 우리가 정한 이동시간을 넣는다(source: synthetic_fixture).
테스트가 확인하는 것은 TMAP 값이 맞는지가 아니라 "이동시간이 이만큼이면 판정이 맞게 나오는가"다.

  예상 시간에 권장 여유까지 들어온다   pass — 장소가 여럿이어도 feasible 이 나온다
  닿기는 하지만 권장 여유가 모자란다   unknown (BUFFER_NOT_MET) — 사용자에게 묻는다
  예상으로는 늦는다                   unknown (LATE_BY_ESTIMATE) — 예상치라 확정하지 않는다
  이동 0분으로도 늦는다               fail — 지금처럼 확정한다
  대중교통으로 안 되고 택시로는 된다   pass — "택시로 가야 한다" 고 알린다 (수단을 말하지 않았을 때)

xfail(strict=True) 는 "지금 코드가 틀렸고, 고칠 작업이 정해져 있다" 는 표시다.
고치면 XPASS 가 되어 스위트가 실패하므로, 그 작업에서 표시를 지워야 한다.
"""

import pytest

from verdict import FAIL, PASS, UNKNOWN, judge

MUSEUM = "서울시립미술관 서소문본관"
PALACE = "경복궁"
DEOKSU = "덕수궁"
MARKET = "광장시장"
THURSDAY = "2026-10-08"  # 세 곳 모두 여는 날, 공휴일 아님


def estimate(minutes, mode="transit"):
    """판정할 때 길찾기로 받는 구간과 같은 모양이다. 값은 테스트용으로 정했다."""
    return {"minutes": minutes, "mode": mode, "is_lower_bound": False,
            "source": "synthetic_fixture", "method": "synthetic_fixture"}


def with_taxi(transit_minutes, taxi_minutes, fare=9400):
    """대중교통 구간에 택시 구간을 대안으로 붙인다. 판정할 때 TMAP 타임머신 길 안내로 받는 모양이다."""
    leg = estimate(transit_minutes)
    leg["alternatives"] = [{**estimate(taxi_minutes, mode="taxi"), "fare": fare}]
    return leg


def travel(result, frm, to):
    hits = [c for c in result["checks"]
            if c["type"] == "INSUFFICIENT_TRAVEL_TIME" and c["target"] == f"{frm} → {to}"]
    assert len(hits) == 1, f"{frm} → {to} 이동 검사가 {len(hits)}개다"
    return hits[0]


def train(result):
    assert len(result["hard_constraints"]) == 1
    return result["hard_constraints"][0]["policy"]


def t01(palace_start="15:00", mode=None):
    """T01 모양 — 미술관 13:00 에 한 시간 보고, 경복궁 palace_start 에 두 시간."""
    it = {"date": THURSDAY, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": palace_start, "dwell_minutes": 120}]}
    if mode:
        it["travel_mode"] = mode
    return it


def market_then_ktx(ktx_time, mode=None):
    it = {"date": THURSDAY, "stops": [{"place": MARKET, "start": "19:00", "dwell_minutes": 60}],
          "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역",
                                "time": ktx_time, "buffer_rule": "rail_boarding", "source": "user_stated"}]}
    if mode:
        it["travel_mode"] = mode
    return it


# ── A. 예상 시간에 들어오면 장소가 여럿이어도 feasible ──────────────────

def test_two_stops_feasible_with_estimate(facts, policy):
    """T01 — 14:00 에 나와 25분이면 14:25 도착. 권장 여유 10분을 더해도 15:00 전이다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(25)
    out = judge(t01(), facts, policy)
    assert travel(out, MUSEUM, PALACE)["status"] == PASS
    assert out["summary"]["verdict"] == "feasible"


def test_three_stops_feasible_with_estimates(facts, policy):
    """덕수궁 → 미술관 → 경복궁. 구간마다 예상 시간에 권장 여유까지 들어오면 feasible 이다."""
    facts["legs"][f"{DEOKSU}|{MUSEUM}"] = estimate(10)
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(25)
    it = {"date": THURSDAY, "stops": [
        {"place": DEOKSU, "start": "10:00", "dwell_minutes": 60},
        {"place": MUSEUM, "start": "11:30", "dwell_minutes": 60},
        {"place": PALACE, "start": "13:30", "dwell_minutes": 90}]}
    assert judge(it, facts, policy)["summary"]["verdict"] == "feasible"


def test_estimate_short_of_buffer_asks_user(facts, policy):
    """55분이면 14:55 에 닿아 15:00 전이지만 권장 여유 10분이 모자란다. 확정하지 않고 묻는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(55)
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "BUFFER_NOT_MET"


# ── B. 예상으로는 늦어도 확정하지 않는다 ───────────────────────────────

def test_late_by_estimate_is_not_fail_even_with_stated_mode(facts, policy):
    """
    65분이면 15:05 도착이라 예상으로는 늦는다. 대중교통으로 간다고 말했어도 예상치일 뿐이다.
    걸음이 빠르거나 앞 열차를 타면 더 일찍 닿을 수 있다.
    """
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(65)
    c = travel(judge(t01(mode="transit"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN
    assert c["unknown_reason"] == "LATE_BY_ESTIMATE"


def test_late_by_estimate_is_not_train_fail(facts, policy):
    """광장시장에서 20:00 에 나와 40분이면 20:40 — 20:30 KTX 를 놓칠 것 같지만 확정하지 않는다. 중요도는 그대로다."""
    facts["legs"][f"{MARKET}|서울역"] = estimate(40)
    p = train(judge(market_then_ktx("20:30", mode="transit"), facts, policy))
    assert p["status"] == UNKNOWN
    assert p["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert p["severity"] == "blocking"


# ── C. 대조군 — 확정할 수 있는 것은 지금처럼 확정한다 ────────────────────

def test_late_even_with_zero_travel_is_still_fail(facts, policy):
    """예상치와 상관없이 14:00 에 나오는데 13:50 경복궁이면 순간이동을 해도 늦는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = estimate(25)
    c = travel(judge(t01("13:50", mode="transit"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == FAIL


# ── D. 대중교통으로 안 되면 택시로 ──────────────────────────────────
# 판정 코어의 원칙: pass 는 갈 수 있는 수단이 하나는 있으면 된다. 사용자가 수단을 말하지 않았으면
# 택시로 시간 안에 닿는 것도 '된다' 다. 대신 택시로 가야 한다는 것과 요금, 당일 교통에 따라
# 달라질 수 있다는 것을 알린다. 사용자가 말한 수단이 있으면 그 수단으로만 판정한다.

@pytest.mark.xfail(strict=True, reason="택시 대안을 아직 보지 않는다 — 다음 커밋에서 고친다")
def test_taxi_makes_it_when_transit_is_late(facts, policy):
    """대중교통 65분이면 15:05 라 늦지만, 택시 20분이면 14:20 — 권장 여유를 더해도 15:00 전이다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    out = judge(t01(), facts, policy)
    c = travel(out, MUSEUM, PALACE)
    assert c["status"] == PASS
    assert "택시" in c["notice"] and "9,400원" in c["notice"] and "당일 교통" in c["notice"]
    assert out["summary"]["verdict"] == "feasible"


@pytest.mark.xfail(strict=True, reason="택시 대안을 아직 보지 않는다 — 다음 커밋에서 고친다")
def test_stated_taxi_is_judged_by_taxi(facts, policy):
    """택시로 간다고 말했으면 대중교통 시간이 아니라 택시 시간으로 판정한다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    assert travel(judge(t01(mode="taxi"), facts, policy), MUSEUM, PALACE)["status"] == PASS


@pytest.mark.xfail(strict=True, reason="택시 대안을 아직 보지 않는다 — 다음 커밋에서 고친다")
def test_taxi_also_late_is_told(facts, policy):
    """택시로도 늦으면 대중교통 결과(늦을 가능성이 높다)를 그대로 두고, 택시로도 어렵다고 알린다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 70)
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert "택시" in (c.get("notice") or "")


@pytest.mark.xfail(strict=True, reason="택시 대안을 아직 보지 않는다 — 다음 커밋에서 고친다")
def test_taxi_makes_the_train(facts, policy):
    """광장시장에서 20:00 에 나와 대중교통 40분이면 20:30 KTX 를 놓칠 것 같지만, 택시 12분이면 20:12 —
    권장 여유 15분을 둔 20:15 전이다."""
    facts["legs"][f"{MARKET}|서울역"] = with_taxi(40, 12)
    p = train(judge(market_then_ktx("20:30"), facts, policy))
    assert p["status"] == PASS
    assert "택시" in p["notice"]


def test_stated_transit_is_not_passed_by_taxi(facts, policy):
    """대조군: 대중교통으로 간다고 말했으면 택시로는 된다는 이유로 통과시키지 않는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    c = travel(judge(t01(mode="transit"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "LATE_BY_ESTIMATE"


@pytest.mark.xfail(strict=True, reason="택시 대안을 아직 보지 않는다 — 다음 커밋에서 고친다")
def test_stated_transit_is_told_taxi_would_make_it(facts, policy):
    """대중교통으로 간다고 했으면 판정은 그대로 두되, 택시로는 시간 안에 닿는다는 것을 알려 준다.
    사용자가 고를 수 있게 하려는 것이다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    c = travel(judge(t01(mode="transit"), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert "택시" in (c.get("notice") or "") and "9,400원" in c["notice"]


def test_transit_ok_needs_no_taxi(facts, policy):
    """대조군: 대중교통으로 되면 택시를 꺼내지 않는다. 택시 안내도 붙이지 않는다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(25, 10)
    c = travel(judge(t01(), facts, policy), MUSEUM, PALACE)
    assert c["status"] == PASS and not c.get("notice")
