"""화면이 부르는 데모 API 를 가짜 Claude 와 TMAP 으로 끝까지 부른다."""

import copy

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app, normalized
from helpers import T01, TEXT, Claude, by_taxi, demo, transit

LEG = "0:서울시립미술관 서소문본관|경복궁"  # 판정할 때 받은 구간은 앞에 구간 번호가 붙는다.
# 체류시간을 말하지 않은 T08 일정이다.
PALACE = {"date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [], "avoid_taxi": False,
          "hard_constraints": [],
          "stops": [{"raw": "오후 4시 반에 경복궁", "place": "경복궁", "start": "16:30", "start_source": "explicit",
                     "dwell_minutes": None, "scope": "place", "scope_note": None, "outside_only": False}]}
PALACE_KTX = {**PALACE, "hard_constraints": [
    {"raw": "20:30 서울역 KTX 는 꼭 타야 해요", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30",
     "time_source": "explicit"}]}
ORIGINAL = {"date": "2026-10-08", "date_source": "explicit", "weekday_stated": "THU", "hard_constraints": [],
            "stops": [{"place": "서울시립미술관 서소문본관", "start": "13:00", "start_source": "explicit",
                       "dwell_minutes": 60, "scope": "place"},
                      {"place": "경복궁", "start": "15:00", "start_source": "explicit",
                       "dwell_minutes": 120, "scope": "place"}]}


def check(c, text=TEXT):
    res = c.post("/api/check", json={"text": text})
    assert res.status_code == 200, res.text
    return res.json()


def recheck(c, original, revised, legs=None):
    res = c.post("/api/recheck", json={"original": original, "revised": revised, "legs": legs or {}})
    assert res.status_code == 200, res.text
    return res.json()


def test_page_is_served():
    res = demo().get("/")
    assert res.status_code == 200 and "<title>여행 일정 검증</title>" in res.text


def test_supported_places_come_from_the_judge():
    """판정이 확정하는 장소만 오고, 종묘도 회차 입장 덕에 모든 요일이 확정된다."""
    res = demo().get("/api/places")
    assert res.status_code == 200
    places = {p["place"]: p for p in res.json()["places"]}
    assert set(places) == {"경복궁", "창덕궁", "덕수궁", "서울시립미술관 서소문본관", "종묘"}
    assert all(not p["unsettled_weekdays"] and p["holiday"] for p in places.values())


def test_requests_to_other_hosts_are_refused():
    """DNS 리바인딩으로 남의 페이지가 이 키로 Claude 를 부르지 못하게 Host 를 본다."""
    app = create_app(Claude(PALACE), today="2026-10-01")
    assert TestClient(app, base_url="http://evil.example").post("/api/check", json={"text": TEXT}).status_code == 400
    assert TestClient(app, base_url="http://localhost:8001").get("/").status_code == 200


def test_missing_info_is_asked_then_rechecked():
    """체류시간을 묻고 표에 60분을 넣어 다시 검사하면 판정이 난다."""
    c = demo(Claude(PALACE))
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
    """꼭 타야 한다던 열차 시각을 바꾸면 판정과 따로 바꿔도 되는지 묻는다."""
    c = demo(Claude(PALACE_KTX))
    first = check(c)
    revised = copy.deepcopy(first["itinerary"])
    revised["hard_constraints"][0]["time"] = "21:30"
    again = recheck(c, first["itinerary"], revised)
    assert [n["kind"] for n in again["needs_confirmation"]] == ["HARD_CONSTRAINT_CHANGED"]


def test_buffer_rule_follows_the_type_chosen_in_the_table():
    """기준을 그대로 두면 표에서 바꾼 항공편을 열차 기준으로 판정한다."""
    hc = {"id": "HC1", "type": "FLIGHT_DEPARTURE", "place": "김포공항", "time": "20:30",
          "buffer_rule": "rail_boarding", "raw": "20:30 서울역 KTX 는 꼭 타야 해요"}
    assert normalized({"hard_constraints": [hc]})["hard_constraints"][0]["buffer_rule"] == "flight_boarding"


def test_before_keeps_the_first_travel_times(tmap):
    """원래 일정은 처음 받은 이동시간으로 봐야 수정 전 판정이 처음 화면과 같다."""
    tmap(transit(20 * 60))
    c = demo(Claude(T01), "key")
    first = check(c)
    assert first["report"]["verdict"] == "feasible" and first["legs"][LEG]["minutes"] == 20

    tmap(transit(55 * 60), taxi_reply=by_taxi(70 * 60))
    revised = copy.deepcopy(first["itinerary"])
    revised["stops"][0]["start"] = "13:30"  # 14:30 에 나와 55분이면 15:25 라 15:00 경복궁에 늦을 듯하다. 택시도 늦다.
    again = recheck(c, first["itinerary"], revised, first["legs"])
    assert (again["verdict_before"], again["verdict_after"]) == ("feasible", "undetermined")
    assert again["legs"][LEG]["minutes"] == 55


@pytest.mark.parametrize("change", [{"checked_at": "2026-01-01"}, {"source": "public_data"}],
                         ids=["received-another-day", "not-from-tmap"])
def test_only_todays_tmap_values_are_reused(tmap, change):
    """TMAP 값은 24시간 넘게 쓸 수 없다. 다른 날 값이나 TMAP 에서 받지 않은 구간은 버린다."""
    tmap(transit(20 * 60))
    c = demo(Claude(T01), "key")
    first = check(c)
    legs = {LEG: {**first["legs"][LEG], **change}}
    again = recheck(c, first["itinerary"], first["itinerary"], legs)
    assert (again["verdict_before"], again["verdict_after"]) == ("undetermined", "feasible")


def test_taxi_miss_goes_out_as_a_reason(tmap):
    """구간 안의 taxi_miss 는 예외 객체라 화면에는 이유 글자만 보낸다."""
    tmap(transit(20 * 60), taxi_reply={})
    first = check(demo(Claude(T01), "key"))
    assert first["leg_misses"] == {f"{LEG} 택시": "NO_ROUTE"}
    assert "taxi_miss" not in first["legs"][LEG]


def test_no_route_lookup_when_the_judge_stops_first(tmap):
    """판정 코어가 요일부터 묻는 일정이면 길찾기를 부르지 않는다."""
    fake = tmap(transit(20 * 60))
    again = recheck(demo(tmap_key="key"), ORIGINAL, {**ORIGINAL, "weekday_stated": "WED"})
    assert fake.sent == [] and again["verdict_after"] == "undetermined"


def test_unreadable_time_is_a_bad_request(tmap):
    """표 밖에서 온 틀린 시각은 추출 결과와 같은 모델로 걸러 422 를 낸다."""
    fake = tmap(transit(20 * 60))
    stops = [{"place": "경복궁", "start": "25:00", "start_source": "explicit", "scope": "place"}]
    res = demo(tmap_key="key").post("/api/recheck", json={"original": ORIGINAL, "revised": {**ORIGINAL, "stops": stops}})
    assert res.status_code == 422 and fake.sent == []


@pytest.mark.parametrize("time", ["14:00", "20:30"], ids=["between-stops", "end-of-day"])
def test_must_keep_without_a_place_is_asked(tmap, time):
    """표에서 장소를 비운 필수 일정은 서버를 멈추지 않고 장소를 묻는다."""
    tmap(transit(20 * 60))
    hc = {"id": "HC1", "type": "OTHER", "time": time, "time_source": "explicit"}
    again = recheck(demo(tmap_key="key"), ORIGINAL, {**ORIGINAL, "hard_constraints": [hc]})
    assert again["verdict_after"] == "undetermined"
    assert "필수 일정의 장소와 시각을 알려주세요" in again["questions"]


def test_recheck_works_without_keys():
    """키 없이 켜면 추출은 503 이지만 표로 고친 일정은 다시 검사한다."""
    c = demo()
    assert c.post("/api/check", json={"text": TEXT}).status_code == 503
    assert recheck(c, ORIGINAL, ORIGINAL)["changed_fields"] == []


def test_empty_text_is_refused():
    res = demo(Claude(PALACE)).post("/api/check", json={"text": "  "})
    assert res.status_code == 422 and res.json()["detail"] == "일정을 적어 주세요"


def test_malformed_extraction_is_refused():
    bad = {**PALACE, "stops": [{**PALACE["stops"][0], "start": "4:30 PM"}]}
    res = demo(Claude(bad)).post("/api/check", json={"text": TEXT})
    assert res.status_code == 422 and res.json()["detail"].startswith("일정을 추출하지 못했습니다")


class Down:
    """연결이 안 되는 가짜 Claude 다."""

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_claude_outage_is_reported():
    """Claude API 에 닿지 못하면 500 대신 502 로 무엇이 실패했는지 알린다."""
    res = demo(Down()).post("/api/check", json={"text": TEXT})
    assert res.status_code == 502 and "APIConnectionError" in res.json()["detail"]


def test_timeline_goes_out_in_itinerary_order(tmap):
    """검사와 다시 검사 응답 모두 일정 순서 줄을 보낸다."""
    tmap(transit(20 * 60))
    c = demo(Claude(T01), "key")
    first = check(c)
    assert [(r["kind"], r["title"], r["status"]) for r in first["timeline"]] == [
        ("stop", "서울시립미술관 서소문본관", "pass"), ("travel", "서울시립미술관 서소문본관 → 경복궁", "pass"),
        ("stop", "경복궁", "pass")]
    again = recheck(c, first["itinerary"], first["itinerary"], first["legs"])
    assert [r["kind"] for r in again["timeline"]] == ["stop", "travel", "stop"]
