"""반례 일정을 재현한다. 기대 결과는 판정 코드를 보지 않고 정했다."""

import pytest

import routes
from helpers import MUSEUM, PALACE, THURSDAY, Claude, demo, estimate, ktx, place_checks, travel, with_taxi
from report import render, report, timeline
from routes import live_legs
from verdict import FAIL, NA, PASS, UNKNOWN, judge

# 1. 같은 장소를 다시 방문할 때 이동시간이 덮어써진다

LOCATION = {PALACE: (37.578, 126.977), MUSEUM: (37.564, 126.974)}


class TimedTmap:
    """출발 시각과 방향에 따라 이동시간이 다른 가짜 TMAP 이다."""

    def __call__(self, method, url, key, body=None):
        if "/pois" in url:  # 우리 이름으로 묻고 TMAP 이 쓰는 이름으로 답한다.
            name = next(n for n in LOCATION if url.endswith(routes.urllib.parse.quote(n)))
            lat, lon = LOCATION[name]
            return {"searchPoiInfo": {"pois": {"poi": [
                {"name": routes.TMAP_NAMES.get(name, name), "frontLat": str(lat), "frontLon": str(lon)}]}}}
        if "/prediction" in url:
            return {"features": [{"properties": {"totalTime": 50 * 60, "taxiFare": 9000}}]}
        from_palace = body["startY"] == str(LOCATION[PALACE][0])
        minutes = (45 if body["searchDttm"].endswith("1030") else 10) if from_palace else 10
        return {"metaData": {"plan": {"itineraries": [{"totalTime": minutes * 60}]}}}


def revisit():
    return {"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "10:00", "dwell_minutes": 30},
        {"place": MUSEUM, "start": "11:00", "dwell_minutes": 30},
        {"place": PALACE, "start": "13:00", "dwell_minutes": 30},
        {"place": MUSEUM, "start": "14:00", "dwell_minutes": 30},
    ]}


def test_revisit_keeps_each_legs_own_travel_time(monkeypatch, facts, policy):
    """오전 구간은 10:30 + 45분 = 11:15 도착이라 늦지만 예상치라 확정하지 않는다."""
    monkeypatch.setattr(routes, "call", TimedTmap())
    legs, misses = live_legs(revisit(), "key", facts, policy)
    assert not misses
    result = judge(revisit(), {**facts, "legs": {**facts["legs"], **legs}}, policy)
    morning, afternoon = [c for c in result["checks"] if c["target"] == f"{PALACE} → {MUSEUM}"]
    assert morning["status"] == UNKNOWN and morning["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert afternoon["status"] != FAIL
    assert result["summary"]["verdict"] == "undetermined"


# 2. 입력이 일부 빠져도 확인할 수 있는 검사는 계속한다
# 1월 1일은 미술관 휴관일이라 덕수궁 시각이 없어도 위반으로 확정한다.

def test_missing_time_elsewhere_keeps_confirmed_violation(facts, policy):
    result = judge({"date": "2026-01-01", "stops": [
        {"place": MUSEUM, "start": "11:00", "dwell_minutes": 60},
        {"place": "덕수궁"},
    ]}, facts, policy)
    assert place_checks(result, MUSEUM)["CLOSED_DAY"]["status"] == FAIL
    assert any(c.get("unknown_reason") == "MISSING_START_TIME" for c in result["checks"])
    assert result["summary"]["verdict"] == "infeasible"


# 3. 필수 조건을 일정의 실제 위치와 연결한다
# 정오 약속을 마지막 장소에서 판정하면 정오 뒤 일정을 근거로 쓰게 된다.

def test_midday_constraint_is_not_judged_from_the_last_stop(facts, policy):
    result = judge({"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "09:00", "dwell_minutes": 60},
        {"place": "덕수궁", "start": "15:00", "dwell_minutes": 60},
    ], "hard_constraints": [{"id": "HC1", "type": "ARRIVE_BY", "place": MUSEUM, "time": "12:00",
                             "buffer_rule": "transit_leg", "source": "user_stated"}]}, facts, policy)
    policy_eval = result["hard_constraints"][0]["policy"]
    assert policy_eval["status"] == UNKNOWN and policy_eval["unknown_reason"] == "MIDDAY_HARD_CONSTRAINT"
    assert policy_eval["notice"]  # 아직 검사하지 않는다고 알린다.
    assert result["summary"]["verdict"] != "infeasible"


def test_last_train_of_the_day_is_still_judged(facts, policy):
    """대조군: 19:00 에 광장시장을 나와 19:00 KTX 는 이동 0분이어도 늦는다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}],
                    "hard_constraints": [ktx("19:00")]}, facts, policy)
    assert result["hard_constraints"][0]["policy"]["status"] == FAIL


# 4. 재검사 API 에도 일정 검증이 필요하다

def palace_at(dwell):
    stop = {"place": PALACE, "start": "16:30", "start_source": "explicit", "scope": "place"}
    if dwell is not ...:
        stop["dwell_minutes"] = dwell
    return {"date": THURSDAY, "date_source": "explicit", "weekday_stated": None,
            "stops": [stop], "hard_constraints": []}


def recheck(revised):
    return demo().post("/api/recheck", json={"original": palace_at(120), "revised": revised})


@pytest.mark.parametrize("dwell", [-60, "60", 0, 2000])
def test_recheck_rejects_bad_dwell(dwell):
    assert recheck(palace_at(dwell)).status_code == 422


def test_recheck_asks_when_dwell_is_unknown():
    """대조군: 체류를 모르는 것은 잘못된 요청이 아니라 추가 질문과 보류다."""
    res = recheck(palace_at(...))
    assert res.status_code == 200
    assert res.json()["verdict_after"] == "undetermined" and res.json()["questions"]


# 5. 필수 시각을 모르거나 추정한 경우 · 택시를 안 탄다는 조건

def test_train_without_time_is_kept_and_asked(facts, policy):
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}],
                    "hard_constraints": [ktx(None, time_source="missing")]}, facts, policy)
    (hc,) = result["hard_constraints"]
    assert hc["event"]["place"] == "서울역" and hc["event"]["time"] is None
    assert hc["policy"]["status"] == UNKNOWN and "시각" in hc["policy"]["how_to_resolve"]["user"]
    assert result["summary"]["verdict"] == "undetermined"
    assert "None" not in render(report(result))
    assert all("None" not in row["title"] for row in timeline({"stops": []}, result))


def test_extraction_with_null_train_time_is_asked():
    extraction = {"date": THURSDAY, "date_source": "explicit", "weekday_stated": "THU", "notes": [], "avoid_taxi": False,
                  "hard_constraints": [{"raw": "서울역에서 KTX 를 꼭 타야 해요", "type": "TRAIN_DEPARTURE",
                                        "place": "서울역", "time": None, "time_source": "missing"}],
                  "stops": [{"raw": "광장시장 6시", "place": "광장시장", "start": "18:00", "start_source": "explicit",
                             "dwell_minutes": 60, "scope": "place", "scope_note": None, "outside_only": False}]}
    res = demo(Claude(extraction)).post("/api/check", json={"text": "10월 8일 목요일 6시 광장시장, 서울역에서 KTX 를 꼭 타야 해요"})
    assert res.status_code == 200, res.text
    assert any("시각" in q for q in res.json()["questions"])


def test_inferred_train_time_does_not_confirm(facts, policy):
    """19:00 에 나와 19:00 KTX 라도 추정한 시각이면 위반으로 확정하지 않는다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}],
                    "hard_constraints": [ktx("19:00", time_source="inferred")]}, facts, policy)
    policy_eval = result["hard_constraints"][0]["policy"]
    assert policy_eval["status"] == UNKNOWN and policy_eval["unknown_reason"] == "INFERRED_INPUT"
    assert any(a["field"] == "hard_constraints[0].time" for a in result["assumptions"])


def museum_then_palace(avoid_taxi):
    return {"date": THURSDAY, "avoid_taxi": avoid_taxi, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "15:00", "dwell_minutes": 60}]}


def test_no_taxi_is_kept(facts, policy):
    """택시를 안 탄다고 했으니 택시 20분은 빼고 늦는 대중교통 65분으로 본다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    c = travel(judge(museum_then_palace(True), facts, policy), MUSEUM, PALACE)
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "LATE_BY_ESTIMATE"


def test_taxi_is_used_when_not_excluded(facts, policy):
    """대조군: 택시를 빼지 않았으면 택시로 통과시키고 그렇게 알린다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    c = travel(judge(museum_then_palace(False), facts, policy), MUSEUM, PALACE)
    assert c["status"] != UNKNOWN and "택시" in c.get("notice", "")


# 6. 새 평가 첫 실행에서 드러난 것

TUESDAY, MONDAY = "2026-11-17", "2026-11-16"


def test_closed_place_without_time_is_still_caught(facts, policy):
    """월요일 창덕궁은 시각이 없어도 날짜만으로 휴무 위반이다."""
    result = judge({"date": MONDAY, "stops": [{"place": PALACE, "start": "10:00", "dwell_minutes": 60},
                                              {"place": "창덕궁", "dwell_minutes": 90}]}, facts, policy)
    c = place_checks(result, "창덕궁")
    assert c["CLOSED_DAY"]["status"] == FAIL
    assert c["ADMISSION_NOT_POSSIBLE"]["status"] == NA and c["DWELL_NOT_COMPLETABLE"]["status"] == NA
    assert result["summary"]["verdict"] == "infeasible"


def test_open_place_without_time_asks_for_entry_and_dwell(facts, policy):
    """N02: 화요일 창덕궁은 열지만 시각이 없어 입장 · 체류 · 다음 이동이 미확인이다."""
    result = judge({"date": TUESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                               {"place": "창덕궁", "dwell_minutes": 90},
                                               {"place": PALACE, "start": "15:30", "dwell_minutes": 60}]},
                   facts, policy)
    c = place_checks(result, "창덕궁")
    assert c["CLOSED_DAY"]["status"] == PASS
    assert c["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"
    assert c["DWELL_NOT_COMPLETABLE"]["status"] == UNKNOWN
    assert travel(result, "창덕궁", PALACE)["status"] == UNKNOWN
    assert result["summary"]["verdict"] == "infeasible"  # 경복궁 휴궁은 그대로 위반이다.


def test_dwell_is_unknown_when_entry_is_unknown(facts, policy):
    """광장시장은 운영시간 근거가 없어 입장도 체류도 미확인이다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}]},
                   facts, policy)
    c = place_checks(result, "광장시장")
    assert c["ADMISSION_NOT_POSSIBLE"]["status"] == UNKNOWN
    assert c["DWELL_NOT_COMPLETABLE"]["status"] == UNKNOWN


def test_dwell_is_not_applicable_on_a_closed_day(facts, policy):
    """대조군: 쉬는 날은 들어갈 수 없으니 입장 · 체류는 해당 없음이다."""
    result = judge({"date": TUESDAY, "stops": [{"place": PALACE, "start": "11:00", "dwell_minutes": 60}]},
                   facts, policy)
    c = place_checks(result, PALACE)
    assert c["CLOSED_DAY"]["status"] == FAIL
    assert c["ADMISSION_NOT_POSSIBLE"]["status"] == NA and c["DWELL_NOT_COMPLETABLE"]["status"] == NA


def test_travel_to_a_closed_place_is_still_judged(facts, policy):
    """휴궁인 곳도 일부러 갈 수 있어 이동을 본다. 11:00 + 20 + 10 = 11:30 이라 11:45 전이다."""
    facts["legs"][f"덕수궁|{PALACE}"] = estimate(20)
    result = judge({"date": TUESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                               {"place": PALACE, "start": "11:45", "dwell_minutes": 60}]},
                   facts, policy)
    assert travel(result, "덕수궁", PALACE)["status"] == PASS
    assert result["summary"]["verdict"] == "infeasible"


def test_travel_across_a_midday_constraint_is_unknown(facts, policy):
    """N06: 두 장소 사이에 13:00 광화문 약속이 끼면 그 구간 계산은 실제 동선이 아니다."""
    facts["legs"][f"{PALACE}|창덕궁"] = estimate(15)
    facts["legs"]["창덕궁|덕수궁"] = estimate(20)
    result = judge({"date": THURSDAY, "stops": [{"place": PALACE, "start": "09:30", "dwell_minutes": 60},
                                                {"place": "창덕궁", "start": "11:00", "dwell_minutes": 60},
                                                {"place": "덕수궁", "start": "14:00", "dwell_minutes": 60}],
                    "hard_constraints": [{"id": "HC1", "type": "ARRIVE_BY", "place": "광화문", "time": "13:00",
                                          "buffer_rule": "transit_leg", "source": "user_stated"}]}, facts, policy)
    across = travel(result, "창덕궁", "덕수궁")
    assert travel(result, PALACE, "창덕궁")["status"] == PASS
    assert across["status"] == UNKNOWN and across["unknown_reason"] == "MIDDAY_HARD_CONSTRAINT"


# 7. 문 닫은 걸 알고 밖에서만 머무는 일정

def outside_palace(outside_only):
    return {"date": TUESDAY, "stops": [
        {"place": PALACE, "start": "10:00", "dwell_minutes": 60, "outside_only": outside_only},
        {"place": "덕수궁", "start": "11:45", "dwell_minutes": 60}]}


def test_outside_only_stop_is_not_held_to_its_hours(facts, policy):
    """휴궁인 경복궁 앞에서만 쉬면 들어가지 않으니 해당 없음이고 휴무는 안내만 한다."""
    facts["legs"][f"{PALACE}|덕수궁"] = estimate(20)
    result = judge(outside_palace(True), facts, policy)
    c = place_checks(result, PALACE)
    assert [c[t]["status"] for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE", "DWELL_NOT_COMPLETABLE")] == [NA, NA, NA]
    assert "휴무일" in c["CLOSED_DAY"]["notice"]
    assert travel(result, PALACE, "덕수궁")["status"] == PASS
    assert result["summary"]["verdict"] == "feasible"


def test_closed_place_is_still_a_violation_when_entering(facts, policy):
    """대조군: 밖에서만이라고 하지 않았으면 휴무 위반이다."""
    facts["legs"][f"{PALACE}|덕수궁"] = estimate(20)
    result = judge(outside_palace(False), facts, policy)
    assert place_checks(result, PALACE)["CLOSED_DAY"]["status"] == FAIL
    assert result["summary"]["verdict"] == "infeasible"


def test_extraction_keeps_outside_only():
    extraction = {"date": TUESDAY, "date_source": "explicit", "weekday_stated": "TUE", "notes": [], "avoid_taxi": False,
                  "hard_constraints": [],
                  "stops": [{"raw": "경복궁 앞에서 한 시간 쉬다 갈게요", "place": PALACE, "start": "10:00",
                             "start_source": "explicit", "dwell_minutes": 60, "scope": "place", "scope_note": None,
                             "outside_only": True}]}
    res = demo(Claude(extraction)).post("/api/check", json={"text": "11월 17일 화요일 10시, 문 닫은 거 알아요. 경복궁 앞에서 한 시간 쉬다 갈게요"})
    assert res.status_code == 200, res.text
    assert res.json()["itinerary"]["stops"][0]["outside_only"] is True
    assert res.json()["result"]["summary"]["verdict"] != "infeasible"
