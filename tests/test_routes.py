"""
TMAP 길찾기 — 가짜 응답으로 "TMAP 이 이렇게 답하면 우리 코드가 맞게 처리하나" 를 본다.

진짜 TMAP 은 부르지 않는다. 키와 요금이 들고, 인터넷이나 TMAP 사정으로 코드가 멀쩡해도 실패하고,
시간 초과 같은 상황은 일부러 만들 수 없다. 대신 가짜 응답은 2026-10-02 에 check-tmap-api.mjs 로
실제로 받은 모양과 공식 문서를 따랐다. TMAP 이 정말 그렇게 답하는지는 진짜 키로 따로 확인한다.
"""

import json
import socket
import urllib.error

import pytest

import routes
from routes import Miss, live_legs, place_entrance
from verdict import judge

MUSEUM = "서울시립미술관 서소문본관"
PALACE = "경복궁"


def poi(*places):
    """장소 검색 응답. places = (이름, 입구 위도, 입구 경도) — 입구 좌표가 없으면 0 으로 온다."""
    return {"searchPoiInfo": {"pois": {"poi": [
        {"name": n, "frontLat": str(la), "frontLon": str(lo), "noorLat": "37.5", "noorLon": "126.9"}
        for n, la, lo in places]}}}


def transit(seconds):
    return {"metaData": {"plan": {"itineraries": [{"totalTime": seconds, "transferCount": 0}]}}}


TOO_CLOSE = {"result": {"status": 11, "message": "출발지와 도착지가 너무 가까움"}}


def walking(seconds):
    return {"type": "FeatureCollection", "features": [{"properties": {"totalTime": seconds}}]}


class FakeTmap:
    """routes.call 대신 들어간다. 주소마다 정해 둔 응답을 돌려주고, 받은 요청을 남긴다."""

    def __init__(self, transit_reply, walk_reply=None, places=None):
        self.transit_reply, self.walk_reply = transit_reply, walk_reply
        # 2026-10-05 실제 검색 결과처럼 미술관은 "서울시립미술관", 경복궁은 "경복궁역" 이 함께 온다
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
        return self.walk_reply


@pytest.fixture
def tmap(monkeypatch):
    def install(*args, **kw):
        fake = FakeTmap(*args, **kw)
        monkeypatch.setattr(routes, "call", fake)
        return fake
    return install


def t01():
    return {"date": "2026-10-08", "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "15:00", "dwell_minutes": 120}]}


# ── 장소 검색 ───────────────────────────────────────────────────────

def test_place_uses_same_name_not_first_result(tmap):
    """'경복궁' 을 찾았는데 첫 결과가 '경복궁역' 이다. 이름이 같은 곳의 입구 좌표를 쓴다."""
    tmap(transit(0))
    assert place_entrance(PALACE, "key") == (37.578, 126.977)


def test_place_with_a_different_tmap_name(tmap):
    """TMAP 이름이 다른 곳은 정해 둔 이름으로 찾는다. 서울역은 노선별로 나뉘어 있어 KTX 정차역을 쓴다."""
    tmap(transit(0), places={
        "서울역": poi(("서울역[수도권1호선]", 37.556, 126.972), ("서울역[KTX정차역]", 37.555, 126.971)),
        MUSEUM: poi(("서울시립미술관 주차장", 37.565, 126.975), ("서울시립미술관", 37.564, 126.974))})
    assert place_entrance("서울역", "key") == (37.555, 126.971)
    assert place_entrance(MUSEUM, "key") == (37.564, 126.974)


def test_place_with_no_same_name_is_a_miss(tmap):
    tmap(transit(0), places={PALACE: poi(("경복궁역", 37.575, 126.973))})
    with pytest.raises(Miss) as e:
        place_entrance(PALACE, "key")
    assert e.value.reason == "PLACE_NOT_FOUND"


def test_place_without_entrance_uses_center(tmap):
    tmap(transit(0), places={PALACE: poi((PALACE, 0, 0))})
    assert place_entrance(PALACE, "key") == (37.5, 126.9)


# ── 구간 ────────────────────────────────────────────────────────────

def test_transit_becomes_an_estimate_leg(tmap):
    """대중교통 1260초 → 21분. 하한선이 아니라 예상치이고, 판정할 때 받은 값이라고 표시한다."""
    fake = tmap(transit(1260))
    legs, misses = live_legs(t01(), "key")
    leg = legs[f"{MUSEUM}|{PALACE}"]
    assert leg["minutes"] == 21 and leg["mode"] == "transit"
    assert leg["is_lower_bound"] is False and leg["source"] == "api_runtime"
    assert misses == {}
    # 출발 시각은 13:00 시작 + 체류 60분
    assert next(b for u, b in fake.sent if "/transit/" in u)["searchDttm"] == "202610081400"


def test_minutes_are_rounded_up(tmap):
    """1201초는 20분 1초다. 20분으로 내리면 늦는 일정을 통과시킬 수 있어 21분으로 올린다."""
    tmap(transit(1201))
    assert live_legs(t01(), "key")[0][f"{MUSEUM}|{PALACE}"]["minutes"] == 21


@pytest.mark.parametrize("too_close", [TOO_CLOSE, TOO_CLOSE["result"]])
def test_too_close_asks_walking_instead(tmap, too_close):
    """HTTP 200 이어도 '너무 가까움'(11)이면 경로가 없다. 걷기로 다시 묻는다. status 가 어디에 와도 읽는다."""
    tmap(too_close, walk_reply=walking(420))
    leg = live_legs(t01(), "key")[0][f"{MUSEUM}|{PALACE}"]
    assert leg["mode"] == "walk" and leg["minutes"] == 7


def test_no_transit_route_is_a_miss(tmap):
    """다른 이유로 경로가 없으면(14) 이동시간 없이 판정하지 않는다. 받지 못한 구간으로 남긴다."""
    tmap({"result": {"status": 14, "message": "기타"}})
    legs, misses = live_legs(t01(), "key")
    assert legs == {} and misses[f"{MUSEUM}|{PALACE}"].reason == "NO_ROUTE"


def test_train_destination_is_a_leg_too(tmap):
    """열차 조건이 있으면 마지막 장소 → 역 구간도 받는다."""
    it = {"date": "2026-10-08", "stops": [{"place": MUSEUM, "start": "19:00", "dwell_minutes": 30}],
          "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30"}]}
    places = {MUSEUM: poi(("서울시립미술관", 37.564, 126.974)),
              "서울역": poi(("서울역[수도권1호선]", 37.556, 126.972), ("서울역[KTX정차역]", 37.555, 126.971))}
    tmap(transit(900), places=places)
    assert live_legs(it, "key")[0][f"{MUSEUM}|서울역"]["minutes"] == 15


# ── 판정까지 ─────────────────────────────────────────────────────────

def test_t01_is_feasible_with_live_leg(tmap, facts, policy):
    """받은 구간을 판정 코어의 legs 에 덮어쓰면 T01 이 feasible 이 된다. 저장된 하한선 대신 쓰인다."""
    tmap(transit(1260))
    legs, _ = live_legs(t01(), "key")
    out = judge(t01(), {**facts, "legs": {**facts["legs"], **legs}}, policy)
    assert out["summary"]["verdict"] == "feasible"


# ── 호출 실패 ───────────────────────────────────────────────────────

class Body:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("error, reason", [
    (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), "HTTP_ERROR"),
    (socket.timeout("timed out"), "TIMEOUT"),
    (urllib.error.URLError(TimeoutError("timed out")), "TIMEOUT"),
    (urllib.error.URLError("getaddrinfo failed"), "NETWORK_ERROR"),
])
def test_call_failures_are_named(monkeypatch, error, reason):
    """실패를 한데 뭉치지 않는다. 키 오류 · 시간 초과 · 네트워크는 고치는 방법이 다르다."""
    def raise_(*a, **kw):
        raise error
    monkeypatch.setattr(routes.urllib.request, "urlopen", raise_)
    with pytest.raises(Miss) as e:
        routes.call("GET", "https://example.invalid", "key")
    assert e.value.reason == reason


def test_unparseable_body_is_named(monkeypatch):
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: Body(b"<html>error</html>"))
    with pytest.raises(Miss) as e:
        routes.call("GET", "https://example.invalid", "key")
    assert e.value.reason == "UNPARSEABLE_RESPONSE"


def test_empty_body_means_no_result(monkeypatch):
    """장소 검색은 결과가 없으면 본문이 비어서 온다. 오류가 아니라 '결과 없음' 이다."""
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: Body(b""))
    assert routes.call("GET", "https://example.invalid", "key") == {}
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: Body(json.dumps({"a": 1}).encode()))
    assert routes.call("GET", "https://example.invalid", "key") == {"a": 1}


# ── 429 너무 빨리 불렀다 ─────────────────────────────────────────────
# 2026-10-05 에 대중교통을 연달아 네 번 부르자 네 번째가 429 THROTTLED 였다(하루 무료 한도 안).

def answers(monkeypatch, *outcomes):
    """urlopen 이 차례대로 내놓는다. 예외면 던지고, bytes 면 본문으로 돌려준다."""
    queue = iter(outcomes)

    def urlopen(*a, **kw):
        o = next(queue)
        if isinstance(o, Exception):
            raise o
        return Body(o)
    monkeypatch.setattr(routes.urllib.request, "urlopen", urlopen)


def throttled(headers=None):
    return urllib.error.HTTPError("u", 429, "Too Many Requests", headers or {}, None)


@pytest.fixture
def waits(monkeypatch):
    """실제로 기다리지 않고, 몇 초를 기다렸는지만 남긴다."""
    slept = []
    monkeypatch.setattr(routes, "sleep", slept.append)
    return slept


def test_throttled_call_waits_and_asks_again(monkeypatch, waits):
    """429 다음에 정상 응답이 오면, 1초 기다렸다 다시 물은 응답을 쓴다."""
    answers(monkeypatch, throttled(), b'{"ok": 1}')
    assert routes.call("GET", "https://example.invalid", "key") == {"ok": 1}
    assert waits == [1]


def test_retry_after_sets_the_wait(monkeypatch, waits):
    """TMAP 이 기다릴 시간을 알려 주면 그만큼 기다린다."""
    answers(monkeypatch, throttled({"Retry-After": "3"}), b'{"ok": 1}')
    routes.call("GET", "https://example.invalid", "key")
    assert waits == [3.0]


def test_still_throttled_is_rate_limited(monkeypatch, waits):
    """네 번 모두 429 면 1 · 2 · 4초 기다린 뒤 RATE_LIMITED 로 남긴다. 키 오류(HTTP_ERROR)와 섞지 않는다."""
    answers(monkeypatch, *[throttled() for _ in range(4)])
    with pytest.raises(Miss) as e:
        routes.call("GET", "https://example.invalid", "key")
    assert e.value.reason == "RATE_LIMITED"
    assert waits == [1, 2, 4]
