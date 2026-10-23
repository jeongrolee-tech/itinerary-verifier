"""
3차 피드백 반례 네 가지 — 저장된 데이터와 가짜 TMAP 응답으로 재현한다(진짜 Anthropic · TMAP 은 부르지 않는다).

기대 결과는 판정 코드가 아니라 피드백의 사례와 지금 판정 원칙에서 정했다.

xfail(strict=True) 는 "지금 코드가 틀렸고, 고칠 작업이 정해져 있다" 는 표시다. 고치면 XPASS 가 되어 스위트가
실패하므로, 그 작업에서 표시를 지운다.
"""

import pytest
from fastapi.testclient import TestClient

import routes
from app import create_app
from report import render, report, timeline
from routes import live_legs
from verdict import FAIL, NA, PASS, UNKNOWN, judge

MUSEUM = "서울시립미술관 서소문본관"
PALACE = "경복궁"
THURSDAY = "2026-10-08"


def travel_checks(result, label):
    return [c for c in result["checks"] if c["type"] == "INSUFFICIENT_TRAVEL_TIME" and c["target"] == label]


# ── 1. 같은 장소를 다시 방문할 때 이동시간이 덮어써진다 ────────────────────
# 경복궁 → 미술관을 오전 10:30 에 나서면 45분, 오후 13:30 에 나서면 10분. 반대 방향 10분, 택시 50분.
# 오전 구간은 10:30 + 45 = 11:15 도착이라 11:00 시작에 늦는다 — 예상치라 확정하지 않고 LATE_BY_ESTIMATE.

LOCATION = {PALACE: (37.578, 126.977), MUSEUM: (37.564, 126.974)}


class TimedTmap:
    """출발 시각과 방향에 따라 다른 이동시간을 돌려주는 가짜 TMAP."""

    def __call__(self, method, url, key, body=None):
        if "/pois" in url:  # 우리 이름으로 묻고, TMAP 이 쓰는 이름(routes.TMAP_NAMES)으로 답한다
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
    monkeypatch.setattr(routes, "call", TimedTmap())
    legs, misses = live_legs(revisit(), "key", facts, policy)
    assert not misses
    result = judge(revisit(), {**facts, "legs": {**facts["legs"], **legs}}, policy)
    morning, afternoon = travel_checks(result, f"{PALACE} → {MUSEUM}")
    assert morning["status"] == UNKNOWN and morning["unknown_reason"] == "LATE_BY_ESTIMATE"
    assert afternoon["status"] != FAIL
    assert result["summary"]["verdict"] == "undetermined"


# ── 2. 입력이 일부 빠져도 확인할 수 있는 검사는 계속한다 ──────────────────
# 2026-01-01 미술관은 고정일 휴관이다. 덕수궁에 시각이 없어도 미술관 휴관은 확인할 수 있다.

def test_missing_time_elsewhere_keeps_confirmed_violation(facts, policy):
    result = judge({"date": "2026-01-01", "stops": [
        {"place": MUSEUM, "start": "11:00", "dwell_minutes": 60},
        {"place": "덕수궁"},
    ]}, facts, policy)
    closed = [c for c in result["checks"] if c["type"] == "CLOSED_DAY" and c["target"] == MUSEUM]
    assert closed and closed[0]["status"] == FAIL
    assert any(c.get("unknown_reason") == "MISSING_START_TIME" for c in result["checks"])
    assert result["summary"]["verdict"] == "infeasible"


# ── 3. 필수 조건을 일정의 실제 위치와 연결한다 ────────────────────────────
# "오전 9시 경복궁 한 시간, 정오까지 미술관 앞에 꼭 도착, 오후 3시 덕수궁 한 시간" — 정오 약속을 덕수궁에서
# 나오는 16:00 으로 판정하면 정오 뒤의 활동을 실패 근거로 쓴 셈이다. 하루 중간의 필수 일정은 아직 검사하지
# 않으므로 위반으로 확정하지 않고 알린다.

def test_midday_constraint_is_not_judged_from_the_last_stop(facts, policy):
    result = judge({"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "09:00", "dwell_minutes": 60},
        {"place": "덕수궁", "start": "15:00", "dwell_minutes": 60},
    ], "hard_constraints": [{"id": "HC1", "type": "ARRIVE_BY", "place": MUSEUM, "time": "12:00",
                             "buffer_rule": "transit_leg", "source": "user_stated"}]}, facts, policy)
    policy_eval = result["hard_constraints"][0]["policy"]
    assert policy_eval["status"] == UNKNOWN and policy_eval["unknown_reason"] == "MIDDAY_HARD_CONSTRAINT"
    assert policy_eval["notice"]  # 아직 검사하지 않는다고 알린다
    assert result["summary"]["verdict"] != "infeasible"


def test_last_train_of_the_day_is_still_judged(facts, policy):
    """대조군: 하루 마지막 교통편은 지금처럼 마지막 장소에서 판정한다 — 19:00 에 광장시장을 나와 19:00 KTX 는
    이동 0분으로도 늦어 위반이다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}],
                    "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "19:00",
                                          "buffer_rule": "rail_boarding", "source": "user_stated"}]}, facts, policy)
    assert result["hard_constraints"][0]["policy"]["status"] == FAIL


# ── 4. 재검사 API 에도 일정 검증이 필요하다 ──────────────────────────────
# 체류 -60 이 통과하고, 문자열 "60" 은 500 이 났다. 음수 · 허용하지 않는 타입은 잘못된 요청(422)이고,
# 체류를 모르는 것은 추가 질문과 보류다.

def palace_at(dwell):
    stop = {"place": PALACE, "start": "16:30", "start_source": "explicit", "scope": "place"}
    if dwell is not ...:
        stop["dwell_minutes"] = dwell
    return {"date": THURSDAY, "date_source": "explicit", "weekday_stated": None,
            "stops": [stop], "hard_constraints": []}


def recheck(revised):
    client = TestClient(create_app(None, None, today="2026-10-01"), base_url="http://127.0.0.1")
    return client.post("/api/recheck", json={"original": palace_at(120), "revised": revised})


@pytest.mark.parametrize("dwell", [-60, "60", 0, 2000])
def test_recheck_rejects_bad_dwell(dwell):
    assert recheck(palace_at(dwell)).status_code == 422


def test_recheck_asks_when_dwell_is_unknown():
    """대조군: 체류를 모르는 것은 잘못된 요청이 아니라 추가 질문과 보류다."""
    res = recheck(palace_at(...))
    assert res.status_code == 200
    assert res.json()["verdict_after"] == "undetermined" and res.json()["questions"]


# ── 5. 필수 시각을 모르거나 추정한 경우 · 택시를 안 탄다는 조건 (3차 피드백 2단계) ────────────
# "서울역에서 KTX 를 꼭 타야 하지만 출발 시각은 모른다" — KTX 조건을 버리지 않고 시각을 묻는다. 추정한 시각은
# 사용자가 확인하기 전까지 확정된 입력이 아니다. 택시를 안 탄다고 했으면 택시 대안으로 통과시키지 않는다.

from test_travel_estimate import with_taxi  # noqa: E402
from test_verify import Claude  # noqa: E402


def ktx(time, time_source):
    return {"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": time, "time_source": time_source,
            "buffer_rule": "rail_boarding", "source": "user_stated"}


def test_train_without_time_is_kept_and_asked(facts, policy):
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}],
                    "hard_constraints": [ktx(None, "missing")]}, facts, policy)
    (hc,) = result["hard_constraints"]
    assert hc["event"]["place"] == "서울역" and hc["event"]["time"] is None
    assert hc["policy"]["status"] == UNKNOWN and "시각" in hc["policy"]["how_to_resolve"]["user"]
    assert result["summary"]["verdict"] == "undetermined"
    # 화면 · 터미널 글에 빈 값(None)을 찍지 않는다
    assert "None" not in render(report(result))
    assert all("None" not in row["title"] for row in timeline({"stops": []}, result))


def test_extraction_with_null_train_time_is_asked():
    extraction = {"date": THURSDAY, "date_source": "explicit", "weekday_stated": "THU", "notes": [], "avoid_taxi": False,
                  "hard_constraints": [{"raw": "서울역에서 KTX 를 꼭 타야 해요", "type": "TRAIN_DEPARTURE",
                                        "place": "서울역", "time": None, "time_source": "missing"}],
                  "stops": [{"raw": "광장시장 6시", "place": "광장시장", "start": "18:00", "start_source": "explicit",
                             "dwell_minutes": 60, "scope": "place", "scope_note": None}]}
    client = TestClient(create_app(Claude(extraction), None, today="2026-10-01"), base_url="http://127.0.0.1")
    res = client.post("/api/check", json={"text": "10월 8일 목요일 6시 광장시장, 서울역에서 KTX 를 꼭 타야 해요"})
    assert res.status_code == 200, res.text
    assert any("시각" in q for q in res.json()["questions"])


def test_inferred_train_time_does_not_confirm(facts, policy):
    """19:00 에 광장시장을 나와 19:00 KTX — 시각이 사용자가 말한 것이면 위반이지만, 추정한 시각이면 확정하지 않는다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}],
                    "hard_constraints": [ktx("19:00", "inferred")]}, facts, policy)
    policy_eval = result["hard_constraints"][0]["policy"]
    assert policy_eval["status"] == UNKNOWN and policy_eval["unknown_reason"] == "INFERRED_INPUT"
    assert any(a["field"] == "hard_constraints[0].time" for a in result["assumptions"])


def museum_then_palace(avoid_taxi):
    return {"date": THURSDAY, "avoid_taxi": avoid_taxi, "stops": [
        {"place": MUSEUM, "start": "13:00", "dwell_minutes": 60},
        {"place": PALACE, "start": "15:00", "dwell_minutes": 60}]}


def test_no_taxi_is_kept(facts, policy):
    """대중교통 65분이면 늦을 가능성이 높고 택시 20분이면 닿는다 — 택시를 안 탄다고 했으면 통과가 아니다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    c = travel_checks(judge(museum_then_palace(True), facts, policy), f"{MUSEUM} → {PALACE}")[0]
    assert c["status"] == UNKNOWN and c["unknown_reason"] == "LATE_BY_ESTIMATE"


def test_taxi_is_used_when_not_excluded(facts, policy):
    """대조군: 말하지 않았으면 지금처럼 택시 대안으로 통과시키고 '택시로 가야 한다' 를 알린다."""
    facts["legs"][f"{MUSEUM}|{PALACE}"] = with_taxi(65, 20)
    c = travel_checks(judge(museum_then_palace(False), facts, policy), f"{MUSEUM} → {PALACE}")[0]
    assert c["status"] != UNKNOWN and "택시" in c.get("notice", "")


# ── 6. 새 평가 첫 실행에서 드러난 것 (2026-10-23) ───────────────────────────
# 정답을 판정 코드보다 먼저 정한 새 평가(core/eval-new.json)에서 판정 코드와 다른 곳이 나왔다
# (docs/experiments/2026-10-23-new-eval-first-run.md). 기대 결과는 그 정답과 사용자 결정에서 왔다.
#   - 확인할 수 있는 검사는 한다: 시각이 없어도 휴무일은 날짜만으로 확인한다
#   - 해야 하는데 못 한 검사는 미확인, 할 필요가 없는 검사만 해당 없음(멘토 T28 지적)
#   - 쉬는 곳이라도 일부러 갈 수 있으니, 쉬는 곳이 낀 이동도 다른 이동처럼 본다
#   - 이동 사이에 필수 일정이 끼면 그 구간 계산은 실제 동선이 아니다

from test_travel_estimate import estimate  # noqa: E402

TUESDAY, MONDAY = "2026-11-17", "2026-11-16"


def place_checks(result, name):
    return {c["type"]: c for c in result["checks"] if c["target"] == name}


@pytest.mark.xfail(strict=True, reason="시각이 빠진 장소는 검사를 하나도 만들지 않아 휴무 위반을 놓친다")
def test_closed_place_without_time_is_still_caught(facts, policy):
    """월요일 창덕궁(정기휴일)에 시각이 없다 — 휴무일은 날짜만으로 확인한다. 놓치면 불가능 대신 보류다."""
    result = judge({"date": MONDAY, "stops": [{"place": PALACE, "start": "10:00", "dwell_minutes": 60},
                                              {"place": "창덕궁", "dwell_minutes": 90}]}, facts, policy)
    c = place_checks(result, "창덕궁")
    assert c["CLOSED_DAY"]["status"] == FAIL
    assert c["ADMISSION_NOT_POSSIBLE"]["status"] == NA and c["DWELL_NOT_COMPLETABLE"]["status"] == NA
    assert result["summary"]["verdict"] == "infeasible"


@pytest.mark.xfail(strict=True, reason="시각이 빠진 장소는 입력 검사 한 줄로만 묻고, 쉬는 곳이 낀 이동은 해당 없음이다")
def test_open_place_without_time_asks_for_entry_and_dwell(facts, policy):
    """N02 — 화요일 창덕궁(연다)에 시각이 없다. 휴무일은 통과, 입장 · 체류는 시각을 몰라 미확인이다.
    창덕궁 → 경복궁(화요일 휴궁) 이동도 창덕궁을 나서는 시각을 몰라 미확인이다."""
    result = judge({"date": TUESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                               {"place": "창덕궁", "dwell_minutes": 90},
                                               {"place": PALACE, "start": "15:30", "dwell_minutes": 60}]},
                   facts, policy)
    c = place_checks(result, "창덕궁")
    assert c["CLOSED_DAY"]["status"] == PASS
    assert c["ADMISSION_NOT_POSSIBLE"]["unknown_reason"] == "MISSING_START_TIME"
    assert c["DWELL_NOT_COMPLETABLE"]["status"] == UNKNOWN
    (move,) = travel_checks(result, f"창덕궁 → {PALACE}")
    assert move["status"] == UNKNOWN
    assert result["summary"]["verdict"] == "infeasible"  # 경복궁 휴궁은 그대로 위반이다


def test_dwell_is_unknown_when_entry_is_unknown(facts, policy):
    """광장시장은 운영시간 근거가 없어 입장이 미확인이다. 체류도 할 필요가 없는 게 아니라 못 한 것이다."""
    result = judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00", "dwell_minutes": 60}]},
                   facts, policy)
    c = place_checks(result, "광장시장")
    assert c["ADMISSION_NOT_POSSIBLE"]["status"] == UNKNOWN
    assert c["DWELL_NOT_COMPLETABLE"]["status"] == UNKNOWN


def test_dwell_is_not_applicable_on_a_closed_day(facts, policy):
    """대조군: 쉬는 날은 들어갈 수 없으니 입장 · 체류는 할 필요가 없다 — 해당 없음 그대로다."""
    result = judge({"date": TUESDAY, "stops": [{"place": PALACE, "start": "11:00", "dwell_minutes": 60}]},
                   facts, policy)
    c = place_checks(result, PALACE)
    assert c["CLOSED_DAY"]["status"] == FAIL
    assert c["ADMISSION_NOT_POSSIBLE"]["status"] == NA and c["DWELL_NOT_COMPLETABLE"]["status"] == NA


@pytest.mark.xfail(strict=True, reason="쉬는 곳이 낀 이동은 해당 없음으로 두고 계산하지 않는다")
def test_travel_to_a_closed_place_is_still_judged(facts, policy):
    """화요일 경복궁은 쉬지만 일부러 갈 수도 있다. 휴무는 휴무일 칸이 알리고, 이동은 이동대로 본다 —
    11:00 에 덕수궁을 나와 20분, 권장 여유 10분을 더해도 11:30 이라 11:45 시작 전이다."""
    facts["legs"][f"덕수궁|{PALACE}"] = estimate(20)
    result = judge({"date": TUESDAY, "stops": [{"place": "덕수궁", "start": "10:00", "dwell_minutes": 60},
                                               {"place": PALACE, "start": "11:45", "dwell_minutes": 60}]},
                   facts, policy)
    (move,) = travel_checks(result, f"덕수궁 → {PALACE}")
    assert move["status"] == PASS
    assert result["summary"]["verdict"] == "infeasible"


@pytest.mark.xfail(strict=True, reason="이동 사이에 낀 필수 일정을 보지 않고 장소끼리만 계산한다")
def test_travel_across_a_midday_constraint_is_unknown(facts, policy):
    """N06 — 창덕궁(11:00~12:00)과 덕수궁(14:00) 사이에 13:00 광화문 약속이 있다. 실제 동선은
    창덕궁 → 광화문 → 덕수궁이라 창덕궁 → 덕수궁 계산으로는 알 수 없다. 약속 앞 구간은 그대로 본다."""
    facts["legs"][f"{PALACE}|창덕궁"] = estimate(15)
    facts["legs"]["창덕궁|덕수궁"] = estimate(20)
    result = judge({"date": THURSDAY, "stops": [{"place": PALACE, "start": "09:30", "dwell_minutes": 60},
                                                {"place": "창덕궁", "start": "11:00", "dwell_minutes": 60},
                                                {"place": "덕수궁", "start": "14:00", "dwell_minutes": 60}],
                    "hard_constraints": [{"id": "HC1", "type": "ARRIVE_BY", "place": "광화문", "time": "13:00",
                                          "buffer_rule": "transit_leg", "source": "user_stated"}]}, facts, policy)
    (before,) = travel_checks(result, f"{PALACE} → 창덕궁")
    (across,) = travel_checks(result, "창덕궁 → 덕수궁")
    assert before["status"] == PASS
    assert across["status"] == UNKNOWN and across["unknown_reason"] == "MIDDAY_HARD_CONSTRAINT"
