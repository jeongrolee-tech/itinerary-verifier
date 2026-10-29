"""가짜 Claude 와 TMAP 으로 자연어 일정부터 [결과] 까지 끝까지 돌린다."""

import pytest

import routes
from helpers import T01, TEXT, Claude, by_taxi, transit
from llm import InvalidOutput
from verify import verify


def run(extraction, facts, policy):
    return verify(TEXT, Claude(extraction), "key", facts, policy, today="2026-10-01")


def test_t01_is_feasible_end_to_end(tmap, facts, policy):
    """20분이면 14:20 도착이라 여유를 더해도 15:00 전이다."""
    tmap(transit(20 * 60))
    out = run(T01, facts, policy)
    assert "서울시립미술관 서소문본관 → 경복궁  대중교통 20분" in out
    assert "[결과]  가능" in out


def test_late_by_estimate_is_shown_but_not_confirmed(tmap, facts, policy):
    """65분이면 늦을 듯하지만 예상치라 확정하지 않는다. 택시 70분이라 택시로도 늦는다."""
    tmap(transit(65 * 60), taxi_reply=by_taxi(70 * 60))
    out = run(T01, facts, policy)
    assert "[결과]  보류" in out
    assert "예상 이동시간(65분) 기준이라 확정하지 않는다" in out
    assert "택시로도 시간 안에 닿기 어렵다" in out


def test_taxi_makes_it_and_says_so(tmap, facts, policy):
    """대중교통 65분은 늦지만 택시 20분이면 되어 택시 안내가 보인다."""
    tmap(transit(65 * 60), taxi_reply=by_taxi(20 * 60))
    out = run(T01, facts, policy)
    assert "대중교통 65분 · 택시 20분(약 10,000원)" in out
    assert "[결과]  가능" in out and "택시로 가야 한다" in out


def test_miss_falls_back_to_stored_lower_bound(monkeypatch, facts, policy):
    """받지 못하면 이유를 보이고 저장된 지하철 하한선으로 판정해 보류가 된다."""
    def throttled(method, url, key, body=None):
        raise routes.Miss("RATE_LIMITED", "HTTP 429")
    monkeypatch.setattr(routes, "call", throttled)
    out = run(T01, facts, policy)
    assert "받지 못함 [RATE_LIMITED]" in out
    assert "[결과]  보류" in out


@pytest.mark.parametrize("change", [{"date": None, "date_source": "missing", "weekday_stated": None},
                                    {"weekday_stated": "WED"}], ids=["no-date", "wrong-weekday"])
def test_asks_first_and_calls_no_tmap(tmap, facts, policy, change):
    fake = tmap(transit(20 * 60))
    out = run({**T01, **change}, facts, policy)
    assert fake.sent == []
    assert "[결과]  보류" in out


def test_train_without_time_is_asked(tmap, facts, policy):
    tmap(transit(20 * 60))
    train = {"raw": "서울역에서 KTX 꼭 타야 해요", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": None,
             "time_source": "missing"}
    out = run({**T01, "hard_constraints": [train]}, facts, policy)
    assert "[필수 일정] 시각 모름  서울역" in out
    assert "[결과]  보류" in out


def test_malformed_extraction_stops_before_tmap(tmap, facts, policy):
    fake = tmap(transit(20 * 60))
    bad = {**T01, "stops": [{**T01["stops"][0], "start": "1:00 PM"}, T01["stops"][1]]}
    with pytest.raises(InvalidOutput):
        run(bad, facts, policy)
    assert fake.sent == []
