"""
일정 하나 검증 — Claude 와 TMAP 을 가짜로 끼워 자연어 일정부터 [결과] 까지 끝까지 돌린다.

Claude 응답은 SDK 의 Message 로 만들고(test_llm.py 와 같은 이유), TMAP 은 routes.call 을 바꿔 끼운다.
진짜로 부르지 않으니 키도 요금도 들지 않는다. 진짜 호출은 사람이 키를 넣어 한 번 돌려 확인한다.
"""

import json

from anthropic.types import Message

import routes
from verify import verify

TEXT = "10월 8일 목요일 오후 1시에 서울시립미술관 서소문본관에서 한 시간, 3시에 경복궁에서 두 시간 볼 거예요."
T01 = {"date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [],
       "hard_constraints": [],
       "stops": [{"raw": "오후 1시에 서울시립미술관 서소문본관에서 한 시간", "place": "서울시립미술관 서소문본관",
                  "start": "13:00", "start_source": "explicit", "dwell_minutes": 60, "scope": "place",
                  "scope_note": None},
                 {"raw": "3시에 경복궁에서 두 시간", "place": "경복궁", "start": "15:00",
                  "start_source": "explicit", "dwell_minutes": 120, "scope": "place", "scope_note": None}]}


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
    """장소 검색 · 대중교통 · 택시(타임머신)에 정해 둔 답을 준다. 받은 요청 주소를 남긴다."""
    sent = []

    def call(method, url, key, body=None):
        sent.append(url)
        if "/pois" in url:
            return {"searchPoiInfo": {"pois": {"poi": [
                {"name": "서울시립미술관", "frontLat": "37.564", "frontLon": "126.974"},
                {"name": "경복궁", "frontLat": "37.578", "frontLon": "126.977"}]}}}
        if "/prediction" in url:
            return {"features": [{"properties": {"totalTime": taxi_minutes * 60, "taxiFare": 9400}}]}
        return {"metaData": {"plan": {"itineraries": [{"totalTime": transit_minutes * 60}]}}}
    monkeypatch.setattr(routes, "call", call)
    return sent


def run(extraction, facts, policy):
    return verify(TEXT, Claude(extraction), "key", facts, policy, today="2026-10-01")


def test_t01_is_feasible_end_to_end(monkeypatch, facts, policy):
    """TMAP 이 13분이라고 하면 14:13 도착, 권장 여유를 더해도 15:00 전이다. 저장된 하한선이면 보류였던 일정이다."""
    tmap(monkeypatch, 13)
    out = run(T01, facts, policy)
    assert "서울시립미술관 서소문본관 → 경복궁  대중교통 13분" in out
    assert "[결과]  feasible" in out


def test_late_by_estimate_is_shown_but_not_confirmed(monkeypatch, facts, policy):
    """TMAP 이 65분이라고 하면 늦을 것 같지만, 예상치라 '안 된다' 를 확정하지 않는다."""
    tmap(monkeypatch, 65)
    out = run(T01, facts, policy)
    assert "[결과]  undetermined" in out
    assert "예상 이동시간(65분) 기준이라 확정하지 않는다" in out
    assert "택시로도 시간 안에 닿기 어렵다" in out  # 택시 70분이라 택시로도 늦는다


def test_taxi_makes_it_and_says_so(monkeypatch, facts, policy):
    """대중교통 65분이면 늦지만 택시 20분이면 된다. 택시 시간과 요금, 택시로 가야 한다는 안내가 보인다."""
    tmap(monkeypatch, 65, taxi_minutes=20)
    out = run(T01, facts, policy)
    assert "대중교통 65분 · 택시 20분(약 9,400원)" in out
    assert "[결과]  feasible" in out and "택시로 가야 한다" in out


def test_miss_falls_back_to_stored_lower_bound(monkeypatch, facts, policy):
    """TMAP 에서 받지 못하면 이유를 보여 주고, 저장해 둔 지하철 하한선으로 판정한다 — 그러면 보류다."""
    def throttled(method, url, key, body=None):
        raise routes.Miss("RATE_LIMITED", "HTTP 429")
    monkeypatch.setattr(routes, "call", throttled)
    out = run(T01, facts, policy)
    assert "받지 못함 [RATE_LIMITED]" in out
    assert "[결과]  undetermined" in out


def test_no_date_asks_first_and_calls_no_tmap(monkeypatch, facts, policy):
    """날짜가 없으면 TMAP 을 부르지 않는다. 판정 코어가 날짜부터 묻는다."""
    sent = tmap(monkeypatch, 13)
    out = run({**T01, "date": None, "date_source": "missing", "weekday_stated": None}, facts, policy)
    assert sent == []
    assert "[결과]  undetermined" in out
