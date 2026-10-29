"""여러 테스트가 같이 쓰는 장소 이름 · 일정 조각 · 검사 찾기 · 가짜 Claude · 가짜 TMAP 이다."""

import json
from pathlib import Path

from anthropic.types import Message
from fastapi.testclient import TestClient

import routes
from app import create_app

CORE = Path(__file__).parent.parent / "core"
SUITE = json.loads((CORE / "tests.json").read_text(encoding="utf-8"))
NEW = json.loads((CORE / "eval-new.json").read_text(encoding="utf-8"))

MUSEUM, PALACE, MARKET, DEOKSU = "서울시립미술관 서소문본관", "경복궁", "광장시장", "덕수궁"
THURSDAY = "2026-10-08"  # 미술관 · 궁 · 시장이 모두 열고 공휴일이 아니다.


# 결과에서 검사 찾기
def check(result, type_, target):
    """그 대상의 검사가 꼭 하나여야 한다."""
    (c,) = [c for c in result["checks"] if c["type"] == type_ and c["target"] == target]
    return c


def travel(result, frm, to):
    return check(result, "INSUFFICIENT_TRAVEL_TIME", f"{frm} → {to}")


def train(result):
    (h,) = result["hard_constraints"]
    return h["policy"]


def place_checks(result, name):
    return {c["type"]: c for c in result["checks"] if c["target"] == name}


# 일정 조각
def estimate(minutes, mode="transit", source="synthetic_fixture"):
    """판정할 때 받는 구간 모양이다. TMAP 값은 약관상 저장할 수 없어 값을 정해 넣는다."""
    return {"minutes": minutes, "mode": mode, "is_lower_bound": False, "source": source, "method": source}


def with_taxi(transit_minutes, taxi_minutes, fare=10000):
    return {**estimate(transit_minutes), "alternatives": [{**estimate(taxi_minutes, "taxi"), "fare": fare}]}


def ktx(time, **extra):
    return {"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": time,
            "buffer_rule": "rail_boarding", "source": "user_stated", **extra}


# 가짜 Claude
TEXT = "10월 8일 목요일 오후 1시에 서울시립미술관 서소문본관에서 한 시간, 3시에 경복궁에서 두 시간 볼 거예요."
T01 = {"date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [], "avoid_taxi": False,
       "hard_constraints": [],
       "stops": [{"raw": "오후 1시에 서울시립미술관 서소문본관에서 한 시간", "place": MUSEUM,
                  "start": "13:00", "start_source": "explicit", "dwell_minutes": 60, "scope": "place",
                  "scope_note": None, "outside_only": False},
                 {"raw": "3시에 경복궁에서 두 시간", "place": PALACE, "start": "15:00",
                  "start_source": "explicit", "dwell_minutes": 120, "scope": "place", "scope_note": None,
                  "outside_only": False}]}
REFUSAL = {"type": "refusal", "category": "cyber", "explanation": "declined"}
CUT = [{"type": "text", "text": '{"verd'}]  # 도중에 끊긴 JSON 이다.


def reply(content, stop_reason="end_turn", stop_details=None, **usage):
    """SDK 단계를 건너뛰지 않게 가짜 응답도 SDK Message 로 만든다."""
    return Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": {"input_tokens": 100, "output_tokens": 50, **usage}})


class Claude:
    """정해 둔 응답을 돌려주고 받은 요청을 sent 에 남긴다. dict 를 주면 JSON 글로 답한다."""

    def __init__(self, answer):
        self.messages, self.sent = self, []
        self.reply = answer if isinstance(answer, Message) else \
            reply([{"type": "text", "text": json.dumps(answer, ensure_ascii=False)}])

    def create(self, **kw):
        self.sent.append(kw)
        return self.reply


def demo(claude=None, tmap_key=None):
    """화면 서버의 테스트 클라이언트다. 서버는 내 PC 주소로 온 요청만 받는다."""
    return TestClient(create_app(claude, tmap_key, today="2026-10-01"), base_url="http://127.0.0.1")


# 가짜 TMAP. 실제 값은 약관상 저장할 수 없어 둥근 값을 쓴다.
def poi(*places):
    """places 는 (이름, 입구 위도, 입구 경도)다. 입구 좌표가 없으면 TMAP 은 0 을 준다."""
    return {"searchPoiInfo": {"pois": {"poi": [
        {"name": n, "frontLat": str(la), "frontLon": str(lo), "noorLat": "37.5", "noorLon": "126.9"}
        for n, la, lo in places]}}}


def transit(seconds):
    return {"metaData": {"plan": {"itineraries": [{"totalTime": seconds, "transferCount": 0}]}}}


TOO_CLOSE = {"result": {"status": 11, "message": "출발지와 도착지가 너무 가까움"}}


def walking(seconds):
    return {"type": "FeatureCollection", "features": [{"properties": {"totalTime": seconds}}]}


def by_taxi(seconds, fare=10000):
    """TMAP 타임머신 자동차 길 안내 응답이다."""
    return {"type": "FeatureCollection", "features": [{"properties": {
        "totalTime": seconds, "taxiFare": fare,
        "departureTime": "2026-10-08T14:00:00+0900", "arrivalTime": "2026-10-08T14:10:00+0900"}}]}


class FakeTmap:
    """routes.call 대신 주소마다 정해 둔 응답을 주고, 받은 요청을 sent 에 남긴다."""

    def __init__(self, transit_reply, walk_reply=None, places=None, taxi_reply=None):
        self.transit_reply, self.walk_reply = transit_reply, walk_reply
        self.taxi_reply = by_taxi(600) if taxi_reply is None else taxi_reply
        # 실제 검색처럼 이름이 비슷한 주차장과 역이 함께 온다.
        self.places = places or {MUSEUM: poi(("서울시립미술관 주차장", 37.565, 126.975), ("서울시립미술관", 37.564, 126.974)),
                                 PALACE: poi(("경복궁역", 37.575, 126.973), (PALACE, 37.578, 126.977))}
        self.sent = []

    def __call__(self, method, url, key, body=None):
        self.sent.append((url, body))
        if "/pois" in url:
            name = next(n for n in self.places if url.endswith(routes.urllib.parse.quote(n)))
            return self.places[name]
        if "/transit/" in url:
            return self.transit_reply
        if "/prediction" in url:
            return self.taxi_reply
        return self.walk_reply
