"""
데모 API — 2차 피드백 4번 "자연어 입력 → 빠진 정보 질문 → 근거별 판정 → 사용자 수정 → 재검사".

화면(core/web/index.html)이 부르는 API 두 개를 FastAPI 의 TestClient 로 끝까지 부른다. 화면은 이 응답을 그리기만
한다. Claude 와 TMAP 은 test_verify.py 와 같은 가짜를 쓴다 — 진짜로 부르지 않으니 키도 요금도 들지 않는다.
"""

import copy

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

import routes
from app import create_app, normalized
from test_verify import T01, TEXT, Claude, tmap

TODAY = "2026-10-01"
LOCAL = "http://127.0.0.1"  # 앱은 내 PC 주소로 온 요청만 받는다
LEG = "서울시립미술관 서소문본관|경복궁"
# T08 과 같은 일정 — 10월 8일 목요일 16:30 경복궁. 얼마나 머물지 말하지 않았다
PALACE = {"date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [],
          "hard_constraints": [],
          "stops": [{"raw": "오후 4시 반에 경복궁", "place": "경복궁", "start": "16:30", "start_source": "explicit",
                     "dwell_minutes": None, "scope": "place", "scope_note": None}]}
PALACE_KTX = {**PALACE, "hard_constraints": [
    {"raw": "20:30 서울역 KTX 는 꼭 타야 해요", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30"}]}


def client(extraction=None, tmap_key=None):
    return TestClient(create_app(Claude(extraction) if extraction else None, tmap_key, today=TODAY), base_url=LOCAL)


def check(c, text=TEXT):
    res = c.post("/api/check", json={"text": text})
    assert res.status_code == 200, res.text
    return res.json()


def recheck(c, original, revised, legs=None):
    res = c.post("/api/recheck", json={"original": original, "revised": revised, "legs": legs or {}})
    assert res.status_code == 200, res.text
    return res.json()


def test_page_is_served():
    """첫 화면은 HTML 한 장이다."""
    res = client().get("/")
    assert res.status_code == 200 and "<title>여행 일정 검증</title>" in res.text


def test_supported_places_come_from_the_judge():
    """화면 위 안내 — 판정 코드가 휴무일과 입장을 확정하는 장소만, 확정하지 못하는 요일과 함께 온다.
    종묘는 월 · 목 · 금이 회차 입장이라 검사가 없고, 공휴일은 일반관람이라 확정된다(facts-2026-10-19)."""
    res = client().get("/api/places")
    assert res.status_code == 200
    places = {p["place"]: p for p in res.json()["places"]}
    assert set(places) == {"경복궁", "창덕궁", "덕수궁", "서울시립미술관 서소문본관", "종묘"}
    assert places["종묘"]["unsettled_weekdays"] == ["MON", "THU", "FRI"] and places["종묘"]["holiday"]
    assert places["종묘"]["open_weekdays"] == ["WED", "SAT", "SUN"]  # 화요일은 쉬는 날로 확정 — 연 날이 아니다
    assert all(not p["unsettled_weekdays"] for name, p in places.items() if name != "종묘")


def test_requests_to_other_hosts_are_refused():
    """다른 사이트가 자기 도메인을 127.0.0.1 로 돌려 이 서버를 부르는 것(DNS 리바인딩)을 막는다. Host 가 127.0.0.1 이나
    localhost 가 아니면 거절한다 — 그래야 남의 페이지가 이 키로 Claude 를 부를 수 없다."""
    app = create_app(Claude(PALACE), today=TODAY)
    assert TestClient(app, base_url="http://evil.example").post("/api/check", json={"text": TEXT}).status_code == 400
    assert TestClient(app, base_url="http://localhost:8001").get("/").status_code == 200


def test_missing_info_is_asked_then_rechecked():
    """빠진 정보 질문 → 사용자 수정 → 재검사. 체류시간을 묻고, 표에 60분을 넣으면 판정이 난다 (T08)."""
    c = client(PALACE)
    first = check(c)
    assert first["report"]["verdict"] == "undetermined"
    assert first["questions"] == ["경복궁에서 얼마나 머무르실 예정인가요?"]

    revised = copy.deepcopy(first["itinerary"])
    revised["stops"][0]["dwell_minutes"] = 60
    again = recheck(c, first["itinerary"], revised)
    assert (again["verdict_before"], again["verdict_after"]) == ("undetermined", "feasible")
    assert again["changed_fields"] == ["stops[0].dwell_minutes"]
    assert again["needs_confirmation"] == [] and again["questions"] == []


def test_changing_the_must_keep_train_asks_first():
    """꼭 타야 한다던 열차 시각을 표에서 바꾸면 판정과 따로 바꿔도 되는지 묻는다 (recheck.needs_confirmation)."""
    c = client(PALACE_KTX)
    first = check(c)
    revised = copy.deepcopy(first["itinerary"])
    revised["hard_constraints"][0]["time"] = "21:30"
    again = recheck(c, first["itinerary"], revised)
    assert [n["kind"] for n in again["needs_confirmation"]] == ["HARD_CONSTRAINT_CHANGED"]


def test_buffer_rule_follows_the_type_chosen_in_the_table():
    """표에서 열차를 항공편으로 바꾸면 권장 여유 기준도 바뀐다. 처음 기준을 그대로 두면 항공편을 열차 기준으로 판정한다."""
    hc = {"id": "HC1", "type": "FLIGHT_DEPARTURE", "place": "김포공항", "time": "20:30",
          "buffer_rule": "rail_boarding", "raw": "20:30 서울역 KTX 는 꼭 타야 해요"}
    assert normalized({"hard_constraints": [hc]})["hard_constraints"][0]["buffer_rule"] == "flight_boarding"


def test_before_keeps_the_first_travel_times(monkeypatch):
    """원래 일정은 처음 받은 이동시간으로, 고친 일정은 고친 출발 시각으로 새로 받은 이동시간으로 판정한다.

    섞으면 '수정 전' 판정이 처음 화면에서 본 것과 달라진다. 이 예에서 새로 받은 55분으로 원래 일정을 보면
    14:55 도착이라 권장 여유가 모자라 보류가 되는데, 처음 화면은 20분으로 '실행 가능' 이었다.
    """
    tmap(monkeypatch, 20)
    c = client(T01, tmap_key="key")
    first = check(c)
    assert first["report"]["verdict"] == "feasible" and first["legs"][LEG]["minutes"] == 20

    tmap(monkeypatch, 55)
    revised = copy.deepcopy(first["itinerary"])
    revised["stops"][0]["start"] = "13:30"  # 14:30 에 나와 55분이면 15:25 — 15:00 경복궁에 늦을 듯하다
    again = recheck(c, first["itinerary"], revised, first["legs"])
    assert (again["verdict_before"], again["verdict_after"]) == ("feasible", "undetermined")
    assert again["legs"][LEG]["minutes"] == 55


@pytest.mark.parametrize("change", [{"checked_at": "2026-01-01"}, {"source": "public_data"}],
                         ids=["received-another-day", "not-from-tmap"])
def test_only_todays_tmap_values_are_reused(monkeypatch, change):
    """TMAP 값은 받은 뒤 24시간 넘게 쓸 수 없다. 화면을 켜 둔 채 다음 날 다시 검사하면 처음 값을 버린다.
    TMAP 에서 받은 것이 아닌 구간도 화면에서 받지 않는다 — 원래 일정은 저장해 둔 지하철 하한선으로만 본다."""
    tmap(monkeypatch, 20)
    c = client(T01, tmap_key="key")
    first = check(c)
    legs = {LEG: {**first["legs"][LEG], **change}}
    again = recheck(c, first["itinerary"], first["itinerary"], legs)
    assert (again["verdict_before"], again["verdict_after"]) == ("undetermined", "feasible")


def test_taxi_miss_goes_out_as_a_reason(monkeypatch):
    """택시를 받지 못한 이유는 구간 안에 예외 객체로 들어 있다. 화면에는 이유 글자만 간다."""
    tmap(monkeypatch, 20)
    fake = routes.call

    def no_taxi(method, url, key, body=None):
        if "/prediction" in url:
            raise routes.Miss("RATE_LIMITED", "HTTP 429")
        return fake(method, url, key, body)
    monkeypatch.setattr(routes, "call", no_taxi)
    first = check(client(T01, tmap_key="key"))
    assert first["leg_misses"] == {f"{LEG} 택시": "RATE_LIMITED"}
    assert "taxi_miss" not in first["legs"][LEG]


ORIGINAL = {"date": "2026-10-08", "date_source": "explicit", "weekday_stated": "THU", "hard_constraints": [],
            "stops": [{"place": "서울시립미술관 서소문본관", "start": "13:00", "start_source": "explicit",
                       "dwell_minutes": 60, "scope": "place"},
                      {"place": "경복궁", "start": "15:00", "start_source": "explicit",
                       "dwell_minutes": 120, "scope": "place"}]}


@pytest.mark.parametrize("change", [
    {"stops": [{"place": "경복궁", "start": "25:00", "start_source": "explicit", "scope": "place"}]},
    {"weekday_stated": "WED"},
], ids=["unreadable-time", "weekday-conflict"])
def test_no_route_lookup_when_the_judge_stops_first(monkeypatch, change):
    """판정 코어가 입력이나 요일부터 묻는 일정이면 길찾기를 부르지 않는다. 표 밖에서 온 틀린 시각에도 죽지 않는다."""
    sent = tmap(monkeypatch, 20)
    again = recheck(client(tmap_key="key"), ORIGINAL, {**ORIGINAL, **change})
    assert sent == [] and again["verdict_after"] == "undetermined"


def test_recheck_works_without_keys():
    """키 없이 켜면 자연어는 추출할 수 없다고 알리고, 표로 고친 일정은 그대로 다시 검사한다."""
    c = client()
    res = c.post("/api/check", json={"text": TEXT})
    assert res.status_code == 503
    assert recheck(c, ORIGINAL, ORIGINAL)["changed_fields"] == []


def test_empty_text_is_refused():
    res = client(PALACE).post("/api/check", json={"text": "  "})
    assert res.status_code == 422 and res.json()["detail"] == "일정을 적어 주세요"


def test_malformed_extraction_is_refused():
    """Claude 가 '4:30 PM' 처럼 형식이 틀린 시각을 내면 판정하지 않고 추출하지 못했다고 알린다."""
    bad = {**PALACE, "stops": [{**PALACE["stops"][0], "start": "4:30 PM"}]}
    res = client(bad).post("/api/check", json={"text": TEXT})
    assert res.status_code == 422 and res.json()["detail"].startswith("일정을 추출하지 못했습니다")


class Down:
    """연결이 안 되는 Claude."""

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_claude_outage_is_reported():
    """Claude API 에 닿지 못하면 서버 오류로 죽지 않고 무엇이 실패했는지 알린다."""
    res = TestClient(create_app(Down(), today=TODAY), base_url=LOCAL).post("/api/check", json={"text": TEXT})
    assert res.status_code == 502 and "APIConnectionError" in res.json()["detail"]


def test_timeline_goes_out_in_itinerary_order(monkeypatch):
    """화면은 일정 순서 줄(report.timeline)을 그린다. 검사와 다시 검사 둘 다 보낸다."""
    tmap(monkeypatch, 20)
    c = client(T01, tmap_key="key")
    first = check(c)
    assert [(r["kind"], r["title"], r["status"]) for r in first["timeline"]] == [
        ("stop", "서울시립미술관 서소문본관", "pass"), ("travel", "서울시립미술관 서소문본관 → 경복궁", "pass"),
        ("stop", "경복궁", "pass")]
    again = recheck(c, first["itinerary"], first["itinerary"], first["legs"])
    assert [r["kind"] for r in again["timeline"]] == ["stop", "travel", "stop"]
