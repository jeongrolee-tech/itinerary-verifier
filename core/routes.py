"""TMAP 으로 구간마다 예상 이동시간을 받는다. 약관상 판정할 때만 쓰고 저장하지 않는다."""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from spend import count_tmap
from verdict import (HHMM, WHOLE_ITINERARY_INPUTS, crossing_constraint, input_checks, is_midday, judge, segment_key,
                     weekday_of, window_candidate, window_departure)

POI = "https://apis.openapi.sk.com/tmap/pois?version=1&count=10&searchKeyword={}"
TRANSIT = "https://apis.openapi.sk.com/transit/routes"
PEDESTRIAN = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1"
PREDICTION = "https://apis.openapi.sk.com/tmap/routes/prediction?version=1"
# 비용을 셀 때 쓰는 이름이다. spend.TMAP_PRICES 의 키와 같아야 한다.
API_NAMES = {POI.split("?")[0]: "장소 검색", TRANSIT: "대중교통", PEDESTRIAN.split("?")[0]: "보행자 길찾기",
             PREDICTION.split("?")[0]: "택시 예측(타임머신)"}
TIMEOUT_S = 20
TOO_CLOSE = 11  # 출발지와 도착지가 너무 가깝다는 status. HTTP 200 으로 온다
RETRIES = 3
BACKOFF_S = 1
MAX_WAIT_S = 30
sleep = time.sleep  # 테스트에서 바꿔 끼운다

# TMAP 장소 검색에서 이름이 다르게 나오는 곳이다.
# 서울역은 노선별로 나뉘어 있어 열차를 타는 KTX 정차역으로 맞춘다.
TMAP_NAMES = {"서울시립미술관 서소문본관": "서울시립미술관", "서울역": "서울역[KTX정차역]"}


class Miss(Exception):
    """TMAP 에서 값을 받지 못했다."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


def call(method: str, url: str, key: str, body: dict | None = None) -> dict:
    """TMAP 을 부른다. 받지 못하면 Miss 를 낸다."""
    req = urllib.request.Request(
        url, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"appKey": key, "Accept": "application/json", "Content-Type": "application/json"})
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
                raw = res.read()
            break
        except urllib.error.HTTPError as e:
            # 429 는 한도 안에서도 연달아 부르면 온다. 기다렸다 다시 묻는다.
            if e.code == 429 and attempt < RETRIES:
                sleep(min(_retry_after(e) or BACKOFF_S * 2 ** attempt, MAX_WAIT_S))
                continue
            raise Miss("RATE_LIMITED" if e.code == 429 else "HTTP_ERROR", f"HTTP {e.code}") from e
        except (TimeoutError, socket.timeout) as e:
            raise Miss("TIMEOUT", f"{TIMEOUT_S}초 안에 응답하지 않았다") from e
        except urllib.error.URLError as e:
            # 연결 단계의 시간 초과는 URLError 에 감싸여 온다.
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise Miss("TIMEOUT", f"{TIMEOUT_S}초 안에 응답하지 않았다") from e
            raise Miss("NETWORK_ERROR", str(e.reason)) from e
        except (http.client.HTTPException, OSError) as e:
            # 응답을 받다 끊기면 URLError 로 감싸지지 않고 그대로 온다.
            raise Miss("NETWORK_ERROR", f"{type(e).__name__}: {e}") from e
    # 응답을 받은 호출만 센다. 429 와 오류 응답이 청구되는지는 확인하지 못했다.
    count_tmap(API_NAMES.get(url.split("?")[0], url.split("?")[0]))
    # 결과가 없으면 본문이 비어서 온다.
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise Miss("UNPARSEABLE_RESPONSE", raw[:200].decode("utf-8", "replace")) from e


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    """Retry-After 가 날짜 형식이면 읽지 않고 None 을 돌려준다."""
    try:
        return float((e.headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None


def place_entrance(name: str, key: str) -> tuple[float, float]:
    """입구 좌표를 (위도, 경도) 로 돌려준다. 입구가 없으면 중심점을 쓴다."""
    found = call("GET", POI.format(urllib.parse.quote(name)), key)
    pois = ((found.get("searchPoiInfo") or {}).get("pois") or {}).get("poi") or []
    # 이름이 같은 결과만 쓴다. "경복궁" 의 첫 결과가 "경복궁역" 일 수 있다.
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
    """분은 올림한다. 짧게 잡으면 늦는 일정을 통과시킨다."""
    return {"minutes": math.ceil(seconds / 60), "mode": mode, "is_lower_bound": False,
            "source": "api_runtime", "method": method, "checked_at": date.today().isoformat()}


def leg(frm: str, frm_at: tuple[float, float], to: str, to_at: tuple[float, float],
        depart: datetime, key: str) -> dict:
    xy = {"startX": str(frm_at[1]), "startY": str(frm_at[0]), "endX": str(to_at[1]), "endY": str(to_at[0])}
    res = call("POST", TRANSIT, key, {**xy, "count": 1, "lang": 0, "format": "json",
                                      "searchDttm": depart.strftime("%Y%m%d%H%M")})
    itineraries = ((res.get("metaData") or {}).get("plan") or {}).get("itineraries") or []
    if itineraries:
        return estimate(itineraries[0]["totalTime"], "transit", "tmap_transit")
    # HTTP 200 이어도 경로가 없을 수 있다. 너무 가까우면 걷기로 다시 묻는다.
    # status 가 result 안에 오는지 맨 위에 오는지 확인하지 못해 둘 다 읽는다.
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
    """타임머신으로 계획한 출발 시각의 택시 시간과 요금을 받는다."""
    point = lambda name, p: {"name": name, "lon": str(p[1]), "lat": str(p[0])}
    # predictionType 은 이름과 반대로 arrival 이 출발 시각을 넣는 쪽이다.
    res = call("POST", PREDICTION, key, {"routesInfo": {
        "departure": point(frm, frm_at), "destination": point(to, to_at),
        "predictionType": "arrival", "predictionTime": depart.strftime("%Y-%m-%dT%H:%M:%S+0900")}})
    props = ((res.get("features") or [{}])[0]).get("properties") or {}
    if "totalTime" not in props:
        raise Miss("NO_ROUTE", f"택시 예측이 오지 않았다 {json.dumps(res, ensure_ascii=False)[:200]}")
    return {**estimate(props["totalTime"], "taxi", "tmap_prediction"), "fare": props.get("taxiFare")}


def _readable(stop: dict) -> bool:
    """시작 시각이 HH:MM 인지 본다. 판정 코어가 구간을 판정하는 조건과 같다."""
    return bool(re.fullmatch(HHMM, str(stop.get("start") or "")))


def _departure(day: date, i: int, k: str, a: dict, b: dict, itinerary: dict, entry_times: dict, facts: dict,
               policy: dict) -> datetime | None:
    """판정 코어와 같은 출발 시각을 돌려준다. 판정하지 않는 구간이면 None 이다."""
    if "area" in (a.get("scope"), b.get("scope")):
        return None  # 구역이 낀 구간은 판정 코어도 판정하지 않는다
    hc_leg = k.startswith("HC")
    # 뒤 장소에 시각이 없으면 판정 코어가 그 시간대를 계산할 수 있을 때만 묻는다.
    if _readable(a) and (hc_leg or _readable(b) or window_candidate(itinerary, i + 1, facts, policy)):
        if not hc_leg and _readable(b) and crossing_constraint(itinerary.get("hard_constraints") or [], a, b):
            return None  # 사이에 필수 일정이 낀 구간은 판정 코어가 계산하지 않는다
        entry_at = entry_times.get(i, a["start"])
        return (datetime.combine(day, datetime.strptime(entry_at, "%H:%M").time())
                + timedelta(minutes=a.get("dwell_minutes") or 0))  # 체류를 모르면 판정 코어처럼 0분으로 본다
    # 앞 장소에 시각이 없으면 가장 이르게 들어갔다 나서는 시각을 쓴다.
    if not hc_leg and _readable(b) and (leave := window_departure(itinerary, i, facts, policy)):
        return datetime.combine(day, datetime.strptime(leave, "%H:%M").time())
    return None


def live_legs(itinerary: dict, key: str, facts: dict, policy: dict) -> tuple[dict, dict]:
    """(판정 코어의 legs 에 덮어쓸 구간, 받지 못한 구간의 Miss) 를 돌려준다."""
    # 날짜나 장소가 없거나 요일이 어긋나면 판정 코어가 입력부터 묻고 구간은 보지 않는다.
    stated = itinerary.get("weekday_stated")
    if (any(c["unknown_reason"] in WHOLE_ITINERARY_INPUTS for c in input_checks(itinerary))
            or (stated and stated != weekday_of(itinerary["date"]))):
        return {}, {}
    stops = itinerary["stops"]
    # 개장 전에 도착하면 개장부터 머무르므로 판정 코어가 계산한 입장 시각을 쓴다.
    judged = judge(itinerary, facts, policy)
    admission_checks = [c for c in judged.get("checks", [])
                        if c.get("type") == "ADMISSION_NOT_POSSIBLE"]
    entry_times = {}
    if len(admission_checks) == len(stops):
        for i, check in enumerate(admission_checks):
            evidence = check.get("evidence") or check.get("reference") or {}
            if evidence.get("entry_at"):
                entry_times[i] = evidence["entry_at"]

    # 같은 장소 사이를 두 번 가도 출발 시각이 달라 구간마다 따로 받는다.
    pairs = [(i, segment_key(i, a["place"], b["place"]), a, b) for i, (a, b) in enumerate(zip(stops, stops[1:]))] + [
        (len(stops) - 1, segment_key(hc.get("id") or f"HC{n}", stops[-1]["place"], hc["place"]),
         stops[-1], {"place": hc["place"]})
        for n, hc in enumerate(itinerary.get("hard_constraints", []), 1)
        if hc.get("time") and not is_midday(hc, stops)]  # 하루 중간의 필수 일정은 판정하지 않는다
    day = date.fromisoformat(itinerary["date"])
    at, legs, misses = {}, {}, {}
    for i, k, a, b in pairs:
        # 시각이 빠진 장소 뒤 구간의 출발 시각은 앞 구간 이동시간에 달렸다.
        depart = _departure(day, i, k, a, b, itinerary, entry_times,
                            {**facts, "legs": {**facts["legs"], **legs}}, policy)
        if depart is None:
            continue
        try:
            for name in (a["place"], b["place"]):
                if name not in at:
                    try:
                        at[name] = place_entrance(name, key)
                    except Miss as e:
                        at[name] = e  # 못 찾은 장소는 다음 구간에서 다시 묻지 않는다
                if isinstance(at[name], Miss):
                    raise at[name]
            # 구간은 받은 출발 시각에만 쓴다. 판정 코어가 depart_at 으로 맞춰 본다.
            legs[k] = {**leg(a["place"], at[a["place"]], b["place"], at[b["place"]], depart, key),
                       "depart_at": depart.strftime("%Y-%m-%dT%H:%M")}
        except Miss as e:
            misses[k] = e
            continue
        # 택시는 대중교통으로 안 될 때 판정 코어가 보는 대안이다.
        if legs[k]["mode"] == "transit" and not itinerary.get("avoid_taxi"):
            try:
                legs[k]["alternatives"] = [taxi(a["place"], at[a["place"]], b["place"], at[b["place"]],
                                                depart, key)]
            except Miss as e:
                legs[k]["taxi_miss"] = e
    return legs, misses
