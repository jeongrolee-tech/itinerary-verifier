"""가짜 Claude 와 TMAP 으로 자연어 일정부터 [결과] 까지 끝까지 돌린다."""

import json

import pytest
from anthropic.types import Message

import routes
from llm import InvalidOutput
from verify import verify

TEXT = "10월 8일 목요일 오후 1시에 서울시립미술관 서소문본관에서 한 시간, 3시에 경복궁에서 두 시간 볼 거예요."
T01 = {"date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [], "avoid_taxi": False,
       "hard_constraints": [],
       "stops": [{"raw": "오후 1시에 서울시립미술관 서소문본관에서 한 시간", "place": "서울시립미술관 서소문본관",
                  "start": "13:00", "start_source": "explicit", "dwell_minutes": 60, "scope": "place",
                  "scope_note": None, "outside_only": False},
                 {"raw": "3시에 경복궁에서 두 시간", "place": "경복궁", "start": "15:00",
                  "start_source": "explicit", "dwell_minutes": 120, "scope": "place", "scope_note": None, "outside_only": False}]}


class Claude:
    """messages.create 에 정해 둔 추출 결과를 SDK 의 Message 로 돌려준다."""

    def __init__(self, extraction):
        self.messages = self
        self.reply = Message.model_validate({
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(extraction, ensure_ascii=False)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50}})

    def create(self, **kw):
        return self.reply


def tmap(monkeypatch, transit_minutes, taxi_minutes=70):
    """가짜 TMAP 이다. 실제 값은 약관상 저장할 수 없어 둥근 값을 쓴다."""
    sent = []

    def call(method, url, key, body=None):
        sent.append(url)
        if "/pois" in url:
            return {"searchPoiInfo": {"pois": {"poi": [
                {"name": "서울시립미술관", "frontLat": "37.564", "frontLon": "126.974"},
                {"name": "경복궁", "frontLat": "37.578", "frontLon": "126.977"}]}}}
        if "/prediction" in url:
            return {"features": [{"properties": {"totalTime": taxi_minutes * 60, "taxiFare": 10000}}]}
        return {"metaData": {"plan": {"itineraries": [{"totalTime": transit_minutes * 60}]}}}
    monkeypatch.setattr(routes, "call", call)
    return sent


def run(extraction, facts, policy):
    return verify(TEXT, Claude(extraction), "key", facts, policy, today="2026-10-01")


def test_t01_is_feasible_end_to_end(monkeypatch, facts, policy):
    """20분이면 14:20 도착이라 여유를 더해도 15:00 전이다."""
    tmap(monkeypatch, 20)
    out = run(T01, facts, policy)
    assert "서울시립미술관 서소문본관 → 경복궁  대중교통 20분" in out
    assert "[결과]  가능" in out


def test_late_by_estimate_is_shown_but_not_confirmed(monkeypatch, facts, policy):
    """65분이면 늦을 듯하지만 예상치라 확정하지 않는다."""
    tmap(monkeypatch, 65)
    out = run(T01, facts, policy)
    assert "[결과]  보류" in out
    assert "예상 이동시간(65분) 기준이라 확정하지 않는다" in out
    assert "택시로도 시간 안에 닿기 어렵다" in out  # 택시 70분이라 택시로도 늦는다.


def test_taxi_makes_it_and_says_so(monkeypatch, facts, policy):
    """대중교통 65분은 늦지만 택시 20분이면 되어 택시 안내가 보인다."""
    tmap(monkeypatch, 65, taxi_minutes=20)
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
def test_asks_first_and_calls_no_tmap(monkeypatch, facts, policy, change):
    sent = tmap(monkeypatch, 20)
    out = run({**T01, **change}, facts, policy)
    assert sent == []
    assert "[결과]  보류" in out


def test_train_without_time_is_asked(monkeypatch, facts, policy):
    tmap(monkeypatch, 20)
    train = {"raw": "서울역에서 KTX 꼭 타야 해요", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": None,
             "time_source": "missing"}
    out = run({**T01, "hard_constraints": [train]}, facts, policy)
    assert "[필수 일정] 시각 모름  서울역" in out
    assert "[결과]  보류" in out


def test_malformed_extraction_stops_before_tmap(monkeypatch, facts, policy):
    sent = tmap(monkeypatch, 20)
    bad = {**T01, "stops": [{**T01["stops"][0], "start": "1:00 PM"}, T01["stops"][1]]}
    with pytest.raises(InvalidOutput):
        run(bad, facts, policy)
    assert sent == []
