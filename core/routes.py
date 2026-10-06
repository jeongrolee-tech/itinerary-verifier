"""
TMAP 길찾기 — 판정할 때 일정의 구간마다 예상 이동시간을 받는다.

    from routes import live_legs
    legs, misses = live_legs(itinerary, key)   # 판정 코어에 넘길 구간, 받지 못한 구간과 이유

장소 좌표도 판정할 때 TMAP 장소 검색으로 받는다(시설물 입구 좌표). 받은 값은 TMAP 약관상
저장 후 24시간 넘게 쓸 수 없어서(docs/data-policy.md) 메모리에만 두고, 파일이나 기록에 남기지 않는다.
"""

from __future__ import annotations

import http.client
import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

POI = "https://apis.openapi.sk.com/tmap/pois?version=1&count=10&searchKeyword={}"
TRANSIT = "https://apis.openapi.sk.com/transit/routes"
PEDESTRIAN = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"
PREDICTION = "https://apis.openapi.sk.com/tmap/routes/prediction?version=1"  # 타임머신 자동차 길 안내
TIMEOUT_S = 20
TOO_CLOSE = 11  # 대중교통 API 의 "출발지와 도착지가 너무 가까움". HTTP 200 으로 온다
RETRIES = 3     # 429(너무 빨리 부름)를 받았을 때 다시 묻는 횟수
BACKOFF_S = 1   # Retry-After 가 없으면 1초 · 2초 · 4초 기다린다
MAX_WAIT_S = 30  # Retry-After 가 아무리 길어도 이만큼만 기다린다. 넘으면 RATE_LIMITED 로 남는다
sleep = time.sleep  # 테스트에서 바꿔 끼운다

# TMAP 장소 검색에서 이름이 우리 이름과 다르게 나오는 곳. 2026-10-05 check-tmap-api.mjs 로 확인했다.
#   서울시립미술관 서소문본관 — TMAP 이름은 "서울시립미술관"
#   서울역 — TMAP 은 노선별로 나눈다. 열차 조건의 목적지라 KTX 정차역을 쓴다
TMAP_NAMES = {"서울시립미술관 서소문본관": "서울시립미술관", "서울역": "서울역[KTX정차역]"}


class Miss(Exception):
    """받지 못했다. reason — TIMEOUT · NETWORK_ERROR · RATE_LIMITED · HTTP_ERROR · UNPARSEABLE_RESPONSE
    · NO_ROUTE · PLACE_NOT_FOUND"""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


def call(method: str, url: str, key: str, body: dict | None = None) -> dict:
    """TMAP 을 한 번 부른다. 응답 JSON 을 돌려주거나 Miss 를 낸다. 결과가 없으면 본문이 비어서 온다."""
    req = urllib.request.Request(
        url, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"appKey": key, "Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
                raw = res.read()
            break
        except urllib.error.HTTPError as e:
            # ⭐ 429 는 요청이 틀린 게 아니라 너무 빨리 불렀다는 뜻이다. 2026-10-05 에 대중교통을 연달아
            #    네 번 부르자 네 번째가 429 였다(하루 무료 한도 안). 기다릴 시간(Retry-After)을 주면
            #    그만큼, 안 주면 1 · 2 · 4초 기다렸다 다시 묻는다. 그래도 막히면 RATE_LIMITED 로 남긴다.
            if e.code == 429 and attempt < RETRIES:
                sleep(min(_retry_after(e) or BACKOFF_S * 2 ** attempt, MAX_WAIT_S))
                continue
            raise Miss("RATE_LIMITED" if e.code == 429 else "HTTP_ERROR", f"HTTP {e.code}") from e
        except (TimeoutError, socket.timeout) as e:
            raise Miss("TIMEOUT", f"{TIMEOUT_S}초 안에 응답하지 않았다") from e
        except urllib.error.URLError as e:
            # 연결 단계의 시간 초과는 URLError 에 감싸여 온다
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise Miss("TIMEOUT", f"{TIMEOUT_S}초 안에 응답하지 않았다") from e
            raise Miss("NETWORK_ERROR", str(e.reason)) from e
        except (http.client.HTTPException, OSError) as e:
            # 요청을 보낸 뒤 응답을 받다가 끊긴 것은 URLError 로 감싸지지 않고 그대로 온다
            # (RemoteDisconnected · ConnectionResetError · IncompleteRead). 한 구간 때문에 검증 전체가 멈추면 안 된다
            raise Miss("NETWORK_ERROR", f"{type(e).__name__}: {e}") from e
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise Miss("UNPARSEABLE_RESPONSE", raw[:200].decode("utf-8", "replace")) from e


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    """429 응답이 알려 준 기다릴 시간(초). 없거나 날짜 형식이면 None."""
    try:
        return float((e.headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None


def place_entrance(name: str, key: str) -> tuple[float, float]:
    """TMAP 장소 검색에서 이름이 같은 장소의 입구 좌표 (위도, 경도). 입구 좌표가 없으면 중심점을 쓴다."""
    found = call("GET", POI.format(urllib.parse.quote(name)), key)
    pois = ((found.get("searchPoiInfo") or {}).get("pois") or {}).get("poi") or []
    # ⭐ 이름이 같은 결과만 쓴다. "경복궁" 을 찾았는데 첫 결과가 "경복궁역" 이면 엉뚱한 곳까지의
    #    길을 받게 된다. 띄어쓰기만 다른 것은 같은 이름으로 보고, TMAP 이름이 다른 곳은 TMAP_NAMES 로 맞춘다.
    #    이름이 비슷하면 받아 주는 식으로 느슨하게 맞추지 않는다 — "서울시립미술관 주차장" 도 비슷하다.
    target = TMAP_NAMES.get(name, name)
    same = lambda s: (s or "").replace(" ", "") == target.replace(" ", "")
    hit = next((p for p in pois if same(p.get("name"))), None)
    if hit is None:
        raise Miss("PLACE_NOT_FOUND", f"TMAP 장소 검색에 '{target}'과 이름이 같은 곳이 없다")
    lat, lng = float(hit.get("frontLat") or 0), float(hit.get("frontLon") or 0)
    if not (lat and lng):
        lat, lng = float(hit["noorLat"]), float(hit["noorLon"])
    return lat, lng


def estimate(seconds: float, mode: str, method: str) -> dict:
    """판정 코어가 읽는 구간 모양. 하한선이 아니라 예상치다. 분은 올림한다 — 짧게 잡으면 늦는 일정을 통과시킨다."""
    return {"minutes": math.ceil(seconds / 60), "mode": mode, "is_lower_bound": False,
            "source": "api_runtime", "method": method, "checked_at": date.today().isoformat()}


def leg(frm: str, frm_at: tuple[float, float], to: str, to_at: tuple[float, float],
        depart: datetime, key: str) -> dict:
    """두 곳 사이 예상 이동시간. 대중교통으로 묻고, 너무 가깝다고 하면 걷기로 다시 묻는다."""
    xy = {"startX": str(frm_at[1]), "startY": str(frm_at[0]), "endX": str(to_at[1]), "endY": str(to_at[0])}
    res = call("POST", TRANSIT, key, {**xy, "count": 1, "lang": 0, "format": "json",
                                      "searchDttm": depart.strftime("%Y%m%d%H%M")})
    itineraries = ((res.get("metaData") or {}).get("plan") or {}).get("itineraries") or []
    if itineraries:
        return estimate(itineraries[0]["totalTime"], "transit", "tmap_transit")
    # ⭐ HTTP 200 이어도 경로가 없을 수 있다. 너무 가까우면 대중교통 대신 걸어가는 시간을 묻는다.
    #    이걸 "조회했다" 로 넘기면 이동시간 없이 판정하게 된다. status 가 result 아래에 오는지 맨 위에
    #    오는지는 확인하지 못해서(10-02 에 status 11 만 봤다) 둘 다 읽는다.
    status = (res.get("result") or res).get("status")
    if str(status) != str(TOO_CLOSE):
        raise Miss("NO_ROUTE", f"대중교통 경로 없음 {json.dumps(res, ensure_ascii=False)[:200]}")
    walk = call("POST", PEDESTRIAN, key, {**xy, "reqCoordType": "WGS84GEO", "resCoordType": "WGS84GEO",
                                         "startName": urllib.parse.quote(frm), "endName": urllib.parse.quote(to)})
    props = ((walk.get("features") or [{}])[0]).get("properties") or {}
    if "totalTime" not in props:
        raise Miss("NO_ROUTE", "가까운 구간인데 걷기 경로도 오지 않았다")
    return estimate(props["totalTime"], "walk", "tmap_pedestrian")


def taxi(frm: str, frm_at: tuple[float, float], to: str, to_at: tuple[float, float],
         depart: datetime, key: str) -> dict:
    """두 곳 사이 택시 예상 시간과 요금. 계획한 출발 시각의 교통으로 예측한다(타임머신 자동차 길 안내)."""
    point = lambda name, p: {"name": name, "lon": str(p[1]), "lat": str(p[0])}
    # ⭐ predictionType 은 이름과 반대로 읽는다. "arrival" 이 출발 시각을 넣고 도착을 예측한다.
    #    "departure" 로 19:30 을 넣었더니 도착이 19:30 으로 왔다(2026-10-05, check-tmap-api.mjs).
    res = call("POST", PREDICTION, key, {"routesInfo": {
        "departure": point(frm, frm_at), "destination": point(to, to_at),
        "predictionType": "arrival", "predictionTime": depart.strftime("%Y-%m-%dT%H:%M:%S+0900")}})
    props = ((res.get("features") or [{}])[0]).get("properties") or {}
    if "totalTime" not in props:
        raise Miss("NO_ROUTE", f"택시 예측이 오지 않았다 {json.dumps(res, ensure_ascii=False)[:200]}")
    return {**estimate(props["totalTime"], "taxi", "tmap_prediction"), "fare": props.get("taxiFare")}


def live_legs(itinerary: dict, key: str) -> tuple[dict, dict]:
    """일정의 구간마다 예상 이동시간을 받는다. (판정 코어의 legs 에 덮어쓸 구간, 받지 못한 구간 → Miss)

    출발 시각은 계획한 시작 시각에 사용자가 말한 체류시간을 더한 것이다. 체류를 모르면 바로 나온다고
    본다 — 판정 코어도 체류를 모르면 0분으로 보고, 0분으로도 늦을 때만 확정한다.

    대중교통 구간에는 같은 출발 시각의 택시를 대안(alternatives)으로 붙인다. 판정 코어가 대중교통으로
    '된다' 가 안 나올 때 본다. 걸어갈 만큼 가까운 구간에는 붙이지 않는다. 택시를 받지 못해도 대중교통
    판정은 그대로 하고, 이유는 구간의 taxi_miss 에 남긴다.
    """
    stops = itinerary["stops"]
    if not itinerary.get("date") or not stops:
        return {}, {}  # 날짜나 장소가 없으면 판정 코어가 입력부터 묻는다. 길을 찾을 출발 시각도 없다
    pairs = list(zip(stops, stops[1:])) + [
        (stops[-1], {"place": hc["place"]}) for hc in itinerary.get("hard_constraints", [])]
    day = date.fromisoformat(itinerary["date"])
    at, legs, misses = {}, {}, {}
    for a, b in pairs:
        if not a.get("start"):
            continue  # 시각이 없으면 판정 코어도 이 구간을 판정하지 않는다
        if "area" in (a.get("scope"), b.get("scope")):
            continue  # 구역은 대표 지점으로 길을 찾지 않는다. 판정 코어도 이 구간을 판정하지 않는다
        k = f"{a['place']}|{b['place']}"
        try:
            for name in (a["place"], b["place"]):
                if name not in at:
                    try:
                        at[name] = place_entrance(name, key)
                    except Miss as e:
                        at[name] = e  # 못 찾은 장소는 다음 구간에서 다시 묻지 않는다
                if isinstance(at[name], Miss):
                    raise at[name]
            depart = (datetime.combine(day, datetime.strptime(a["start"], "%H:%M").time())
                      + timedelta(minutes=a.get("dwell_minutes") or 0))
            legs[k] = leg(a["place"], at[a["place"]], b["place"], at[b["place"]], depart, key)
        except Miss as e:
            misses[k] = e
            continue
        if legs[k]["mode"] == "transit":
            try:
                legs[k]["alternatives"] = [taxi(a["place"], at[a["place"]], b["place"], at[b["place"]],
                                                depart, key)]
            except Miss as e:
                legs[k]["taxi_miss"] = e
    return legs, misses
