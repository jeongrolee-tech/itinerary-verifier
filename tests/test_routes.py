"""가짜 TMAP 응답으로 길찾기 처리를 본다."""

import http.client
import io
import socket
import urllib.error

import pytest

import routes
from helpers import MUSEUM, PALACE, TOO_CLOSE, by_taxi, poi, transit, walking
from routes import Miss, live_legs, place_entrance
from verdict import judge

URL = "https://example.invalid"
LEG = f"0:{MUSEUM}|{PALACE}"


def t01():
    return {"date": "2026-10-08", "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "15:00", "dwell_minutes": 120}]}


# 장소 검색

def test_place_uses_same_name_not_first_result(tmap):
    """경복궁을 찾으면 첫 결과가 경복궁역이라 이름이 같은 곳을 고른다."""
    tmap(transit(0))
    assert place_entrance(PALACE, "key") == (37.578, 126.977)


def test_place_with_a_different_tmap_name(tmap):
    """서울역은 TMAP 에서 노선별로 나뉘어 있어 KTX 정차역 이름으로 찾는다."""
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


# 구간

def test_transit_becomes_an_estimate_leg(tmap, facts, policy):
    """대중교통 1200초는 하한선이 아닌 20분짜리 예상치 구간이 된다."""
    fake = tmap(transit(1200))
    legs, misses = live_legs(t01(), "key", facts, policy)
    leg = legs[LEG]
    assert leg["minutes"] == 20 and leg["mode"] == "transit"
    assert leg["is_lower_bound"] is False and leg["source"] == "api_runtime"
    assert misses == {}
    # 13:00 시작 + 체류 60분 = 14:00 출발이다.
    assert next(b for u, b in fake.sent if "/transit/" in u)["searchDttm"] == "202610081400"


def test_opening_wait_is_in_route_departure(tmap, facts, policy):
    """08:30 에 와도 09:00 에 들어가 60분 머무니 10:00 출발로 묻는다."""
    itinerary = {"date": "2026-10-08", "stops": [
        {"place": PALACE, "start": "08:30", "dwell_minutes": 60},
        {"place": MUSEUM, "start": "11:30", "dwell_minutes": 60}]}
    fake = tmap(transit(1200))
    live_legs(itinerary, "key", facts, policy)
    assert next(b for u, b in fake.sent if "/transit/" in u)["searchDttm"] == "202610081000"
    assert next(b for u, b in fake.sent if "/prediction" in u)["routesInfo"]["predictionTime"] == "2026-10-08T10:00:00+0900"


def test_minutes_are_rounded_up(tmap, facts, policy):
    """20분으로 내리면 늦는 일정을 통과시킬 수 있어 1201초는 21분이다."""
    tmap(transit(1201))
    assert live_legs(t01(), "key", facts, policy)[0][LEG]["minutes"] == 21


@pytest.mark.parametrize("too_close", [TOO_CLOSE, TOO_CLOSE["result"]])
def test_too_close_asks_walking_instead(tmap, too_close, facts, policy):
    """너무 가까우면 200 에 status 11 이 온다. status 가 어디에 와도 걷기로 다시 묻는다."""
    tmap(too_close, walk_reply=walking(360))
    leg = live_legs(t01(), "key", facts, policy)[0][LEG]
    assert leg["mode"] == "walk" and leg["minutes"] == 6


def test_no_transit_route_is_a_miss(tmap, facts, policy):
    """status 14 처럼 다른 이유로 경로가 없으면 받지 못한 구간으로 남긴다."""
    tmap({"result": {"status": 14, "message": "기타"}})
    legs, misses = live_legs(t01(), "key", facts, policy)
    assert legs == {} and misses[LEG].reason == "NO_ROUTE"


def test_area_stop_is_not_routed(tmap, facts, policy):
    it = {"date": "2026-10-08", "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": "명동", "start": "15:00", "scope": "area"}]}
    fake = tmap(transit(1200))
    assert live_legs(it, "key", facts, policy) == ({}, {})
    assert fake.sent == []


def test_place_not_found_is_asked_once(tmap, facts, policy):
    """못 찾은 장소는 한 번만 묻고, 앞뒤 두 구간 모두 받지 못한 것으로 남긴다."""
    it = {"date": "2026-10-08", "stops": [
        {"place": MUSEUM, "start": "10:00", "dwell_minutes": 60},
        {"place": "없는곳", "start": "12:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "14:00", "dwell_minutes": 60}]}
    places = {MUSEUM: poi(("서울시립미술관", 37.564, 126.974)), "없는곳": poi(("다른곳", 37.5, 127.0)),
              PALACE: poi((PALACE, 37.578, 126.977))}
    fake = tmap(transit(1200), places=places)
    legs, misses = live_legs(it, "key", facts, policy)
    assert set(misses) == {f"0:{MUSEUM}|없는곳", f"1:없는곳|{PALACE}"}
    assert all(m.reason == "PLACE_NOT_FOUND" for m in misses.values())
    asked = [u for u, _ in fake.sent if "/pois" in u and u.endswith(routes.urllib.parse.quote("없는곳"))]
    assert len(asked) == 1


def test_train_destination_is_a_leg_too(tmap, facts, policy):
    it = {"date": "2026-10-08", "stops": [{"place": MUSEUM, "start": "19:00", "dwell_minutes": 30}],
          "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30"}]}
    places = {MUSEUM: poi(("서울시립미술관", 37.564, 126.974)),
              "서울역": poi(("서울역[수도권1호선]", 37.556, 126.972), ("서울역[KTX정차역]", 37.555, 126.971))}
    tmap(transit(1500), places=places)
    assert live_legs(it, "key", facts, policy)[0][f"HC1:{MUSEUM}|서울역"]["minutes"] == 25


# 판정까지

def test_t01_is_feasible_with_live_leg(tmap, facts, policy):
    """받은 구간이 저장된 하한선 대신 쓰여 T01 이 feasible 이 된다."""
    tmap(transit(1200))
    legs, _ = live_legs(t01(), "key", facts, policy)
    out = judge(t01(), {**facts, "legs": {**facts["legs"], **legs}}, policy)
    assert out["summary"]["verdict"] == "feasible"


# 호출 실패

@pytest.mark.parametrize("error, reason", [
    (urllib.error.HTTPError("u", 401, "Unauthorized", {}, None), "HTTP_ERROR"),
    (socket.timeout("timed out"), "TIMEOUT"),
    (urllib.error.URLError(TimeoutError("timed out")), "TIMEOUT"),
    (urllib.error.URLError("getaddrinfo failed"), "NETWORK_ERROR"),
    # 요청을 보낸 뒤 응답을 받다가 끊기면 URLError 로 감싸지지 않고 그대로 온다.
    (http.client.RemoteDisconnected("closed"), "NETWORK_ERROR"),
    (ConnectionResetError("reset by peer"), "NETWORK_ERROR"),
    (http.client.IncompleteRead(b""), "NETWORK_ERROR"),
])
def test_call_failures_are_named(monkeypatch, error, reason):
    """키 오류 · 시간 초과 · 네트워크는 고치는 방법이 달라 이유를 나눈다."""
    def raise_(*a, **kw):
        raise error
    monkeypatch.setattr(routes.urllib.request, "urlopen", raise_)
    with pytest.raises(Miss) as e:
        routes.call("GET", URL, "key")
    assert e.value.reason == reason


def test_unparseable_body_is_named(monkeypatch):
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"<html>error</html>"))
    with pytest.raises(Miss) as e:
        routes.call("GET", URL, "key")
    assert e.value.reason == "UNPARSEABLE_RESPONSE"


def test_empty_body_means_no_result(monkeypatch):
    """장소 검색은 결과가 없으면 빈 본문이 온다. 오류가 아니라 결과 없음이다."""
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b""))
    assert routes.call("GET", URL, "key") == {}
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b'{"a": 1}'))
    assert routes.call("GET", URL, "key") == {"a": 1}


# 429 재시도
# 하루 무료 한도 안이어도 연달아 부르면 429 가 온다.

def answers(monkeypatch, *outcomes):
    """urlopen 이 차례대로 내놓는다. 예외면 던지고, bytes 면 본문으로 돌려준다."""
    queue = iter(outcomes)

    def urlopen(*a, **kw):
        o = next(queue)
        if isinstance(o, Exception):
            raise o
        return io.BytesIO(o)
    monkeypatch.setattr(routes.urllib.request, "urlopen", urlopen)


def throttled(headers=None):
    return urllib.error.HTTPError("u", 429, "Too Many Requests", headers or {}, None)


@pytest.fixture
def waits(monkeypatch):
    """실제로 기다리지 않고, 몇 초를 기다렸는지만 남긴다."""
    slept = []
    monkeypatch.setattr(routes, "sleep", slept.append)
    return slept


# Retry-After 가 없으면 1초, 있으면 그만큼, 길어도 검증 하나가 몇 분씩 멈추지 않게 30초까지만 기다린다.
@pytest.mark.parametrize("headers, waited", [(None, [1]), ({"Retry-After": "3"}, [3.0]), ({"Retry-After": "300"}, [30])])
def test_throttled_call_waits_and_asks_again(monkeypatch, waits, headers, waited):
    answers(monkeypatch, throttled(headers), b'{"ok": 1}')
    assert routes.call("GET", URL, "key") == {"ok": 1}
    assert waits == waited


def test_still_throttled_is_rate_limited(monkeypatch, waits):
    """계속 429 면 키 오류와 섞지 않고 RATE_LIMITED 로 남긴다."""
    answers(monkeypatch, *[throttled() for _ in range(4)])
    with pytest.raises(Miss) as e:
        routes.call("GET", URL, "key")
    assert e.value.reason == "RATE_LIMITED"
    assert waits == [1, 2, 4]


# 택시 대안

def test_transit_leg_gets_a_taxi_alternative(tmap, facts, policy):
    """출발 시각 기준 택시 예측은 TMAP 에서 predictionType arrival 이다."""
    fake = tmap(transit(1200), taxi_reply=by_taxi(600, 10000))
    taxi = live_legs(t01(), "key", facts, policy)[0][LEG]["alternatives"][0]
    assert (taxi["mode"], taxi["minutes"], taxi["fare"]) == ("taxi", 10, 10000)
    assert taxi["is_lower_bound"] is False
    asked = next(b for u, b in fake.sent if "/prediction" in u)["routesInfo"]
    assert asked["predictionType"] == "arrival"
    assert asked["predictionTime"] == "2026-10-08T14:00:00+0900"


# 걸어갈 구간에는 택시를 붙이지 않고, 택시를 안 탄다고 했으면 판정이 보지 않으니 묻지 않는다.
@pytest.mark.parametrize("reply, avoid_taxi", [((TOO_CLOSE, walking(360)), False), ((transit(1200), None), True)],
                         ids=["walking", "no-taxi"])
def test_taxi_is_not_asked(tmap, facts, policy, reply, avoid_taxi):
    fake = tmap(*reply)
    leg = live_legs({**t01(), "avoid_taxi": avoid_taxi}, "key", facts, policy)[0][LEG]
    assert "alternatives" not in leg
    assert not any("/prediction" in u for u, _ in fake.sent)


def test_leg_across_a_midday_constraint_is_not_asked(tmap, facts, policy):
    """사이에 정오 약속이 끼면 판정이 그 구간을 쓰지 않으니 길도 묻지 않는다."""
    fake = tmap(transit(1200))
    itinerary = {"date": "2026-10-08", "stops": [
        {"place": PALACE, "start": "09:00", "dwell_minutes": 60},
        {"place": MUSEUM, "start": "15:00", "dwell_minutes": 60}],
        "hard_constraints": [{"id": "HC1", "type": "ARRIVE_BY", "place": "광화문", "time": "12:00",
                              "buffer_rule": "transit_leg", "source": "user_stated"}]}
    assert live_legs(itinerary, "key", facts, policy) == ({}, {})
    assert not any("/transit/" in u for u, _ in fake.sent)


def test_taxi_miss_keeps_the_transit_leg(tmap, facts, policy):
    tmap(transit(1200), taxi_reply={})
    legs, misses = live_legs(t01(), "key", facts, policy)
    assert legs[LEG]["minutes"] == 20 and "alternatives" not in legs[LEG]
    assert legs[LEG]["taxi_miss"].reason == "NO_ROUTE" and misses == {}


def test_taxi_rescues_a_late_transit_leg(tmap, facts, policy):
    """대중교통 65분은 늦고 택시 20분은 닿는 T01 은 feasible 이고 택시 안내가 붙는다."""
    tmap(transit(65 * 60), taxi_reply=by_taxi(20 * 60, 10000))
    legs, _ = live_legs(t01(), "key", facts, policy)
    out = judge(t01(), {**facts, "legs": {**facts["legs"], **legs}}, policy)
    assert out["summary"]["verdict"] == "feasible"
    move = next(c for c in out["checks"] if c["type"] == "INSUFFICIENT_TRAVEL_TIME" and c["status"] == "pass")
    assert "택시로 가야 한다" in move["notice"]
