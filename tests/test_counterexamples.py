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
from routes import live_legs
from verdict import FAIL, UNKNOWN, judge

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


@pytest.mark.xfail(strict=True, reason="이동시간 키가 '출발|도착' 이라 오후 조회가 오전을 덮어쓴다")
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

@pytest.mark.xfail(strict=True, reason="입력 검사에서 바로 반환해 미술관 휴관 위반이 사라진다")
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

@pytest.mark.xfail(strict=True, reason="필수 조건을 늘 마지막 장소(stops[-1])에서 출발한다고 계산한다")
def test_midday_constraint_is_not_judged_from_the_last_stop(facts, policy):
    result = judge({"date": THURSDAY, "stops": [
        {"place": PALACE, "start": "09:00", "dwell_minutes": 60},
        {"place": "덕수궁", "start": "15:00", "dwell_minutes": 60},
    ], "hard_constraints": [{"id": "HC1", "type": "ARRIVE_BY", "place": MUSEUM, "time": "12:00",
                             "buffer_rule": "transit_leg", "source": "user_stated"}]}, facts, policy)
    policy_eval = result["hard_constraints"][0]["policy"]
    assert policy_eval["status"] == UNKNOWN
    assert result["summary"]["verdict"] != "infeasible"


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


@pytest.mark.xfail(strict=True, reason="/api/recheck 가 일정을 dict 로 받아 검증하지 않는다")
@pytest.mark.parametrize("dwell", [-60, "60", 0, 2000])
def test_recheck_rejects_bad_dwell(dwell):
    assert recheck(palace_at(dwell)).status_code == 422


def test_recheck_asks_when_dwell_is_unknown():
    """대조군: 체류를 모르는 것은 잘못된 요청이 아니라 추가 질문과 보류다."""
    res = recheck(palace_at(...))
    assert res.status_code == 200
    assert res.json()["verdict_after"] == "undetermined" and res.json()["questions"]
