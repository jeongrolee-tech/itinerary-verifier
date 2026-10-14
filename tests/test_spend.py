"""
이번 실행에서 나간 돈 — Claude 호출 비용을 맞게 더하는지, TMAP 은 응답을 받은 호출만 세는지,
검증 명령과 데모 서버가 돈이 나간 만큼 찍는지를 본다. 진짜 API 는 부르지 않는다.
"""

import json

import pytest
from fastapi.testclient import TestClient

import routes
from app import create_app
from llm import Refusal
from spend import Spend, counting
from test_llm import CUT, REFUSAL, FakeClient
from test_routes import Body
from test_verify import T01, TEXT, Claude
from verify import verify


def usage(model="claude-opus-5-5", input_tokens=1000, output_tokens=500, stop_reason="end_turn", category=None):
    return {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read": 0,
            "cache_write": 0, "stop_reason": stop_reason, "refusal_category": category}


# ── Claude ─────────────────────────────────────────────────────────

def test_claude_costs_add_up():
    """Opus 5.5 는 입력 $4 · 출력 $20 (100만 토큰당). 1,000 + 500 토큰이면 $0.014 이고 두 번이면 $0.028 이다."""
    meter = Spend()
    assert meter.claude("구조화 추출", usage()) == pytest.approx(0.014)
    meter.claude("구조화 추출", usage())
    assert meter.claude_usd == pytest.approx(0.028)
    assert "Anthropic $0.0280 — 구조화 추출 2건" in meter.summary()


def test_refusal_before_output_costs_nothing_but_is_counted():
    """출력 전에 거부되면 청구되지 않는다(model_info.billed). 부른 건 세고 돈은 0 이다."""
    meter = Spend()
    assert meter.claude("구조화 추출", usage(output_tokens=0, stop_reason="refusal")) == 0
    assert meter.claude_line() == "Anthropic $0.0000 — 구조화 추출 1건"


def test_unknown_model_is_not_given_a_made_up_price():
    meter = Spend()
    assert meter.claude("구조화 추출", usage(model="claude-unknown")) is None
    assert "단가를 모르는 모델 1건" in meter.summary()


def test_nothing_spent_says_so():
    assert "없다" in Spend().summary()
    assert Spend().line() == "돈이 드는 호출 없음"


# ── TMAP ───────────────────────────────────────────────────────────

def test_tmap_counts_answered_calls_while_counting(monkeypatch):
    """세는 중일 때 응답을 받은 호출만 센다. 밖에서 부른 호출은 어느 계량기에도 들어가지 않는다."""
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda *a, **kw: Body(b"{}"))
    meter = Spend()
    with counting(meter):
        routes.call("GET", routes.POI.format("경복궁"), "key")
        routes.call("POST", routes.TRANSIT, "key", {})
    routes.call("POST", routes.TRANSIT, "key", {})
    assert meter.tmap_calls == {"장소 검색": 1, "대중교통": 1}


def test_tmap_failed_call_is_not_counted(monkeypatch):
    def fail(*a, **kw):
        raise routes.urllib.error.HTTPError("https://x", 500, "error", {}, None)
    monkeypatch.setattr(routes.urllib.request, "urlopen", fail)
    meter = Spend()
    with counting(meter), pytest.raises(routes.Miss):
        routes.call("POST", routes.TRANSIT, "key", {})
    assert not meter.tmap_calls


def test_transit_cost_is_a_range():
    """대중교통은 하루 10건까지 무료다. 오늘 몇 건을 썼는지 몰라서 0원부터 전부 유료일 때까지로 말한다."""
    meter = Spend()
    for _ in range(3):
        meter.tmap("대중교통")
    meter.tmap("택시 예측(타임머신)")
    line = meter.tmap_line()
    assert "대중교통 3건(하루 10건까지 무료, 넘은 건은 건당 0.88원 — 0원 ~ 2.64원)" in line
    assert "택시 예측(타임머신) 1건(요금을 확인하지 못했다)" in line


# ── 검증 명령 · 데모 서버 ────────────────────────────────────────────

def tmap_answers(monkeypatch):
    """urlopen 에 장소 검색 · 대중교통 · 택시 답을 준다. routes.call 은 진짜를 쓰므로 호출 수가 세진다."""
    def urlopen(req, timeout=None):
        url = req.full_url
        if "/pois" in url:
            body = {"searchPoiInfo": {"pois": {"poi": [
                {"name": "서울시립미술관", "frontLat": "37.564", "frontLon": "126.974"},
                {"name": "경복궁", "frontLat": "37.578", "frontLon": "126.977"}]}}}
        elif "/prediction" in url:
            body = {"features": [{"properties": {"totalTime": 1200, "taxiFare": 10000}}]}
        else:
            body = {"metaData": {"plan": {"itineraries": [{"totalTime": 1200}]}}}
        return Body(json.dumps(body).encode())
    monkeypatch.setattr(routes.urllib.request, "urlopen", urlopen)


def test_verify_counts_claude_and_tmap(monkeypatch, facts, policy):
    """T01 — 추출 1번(입력 100 · 출력 50 토큰 → $0.0014), 장소 검색 2번, 대중교통 1번, 택시 1번."""
    tmap_answers(monkeypatch)
    meter = Spend()
    verify(TEXT, Claude(T01), "key", facts, policy, today="2026-10-01", meter=meter)
    assert meter.claude_calls == {"구조화 추출": 1} and meter.claude_usd == pytest.approx(0.0014)
    assert meter.tmap_calls == {"장소 검색": 2, "대중교통": 1, "택시 예측(타임머신)": 1}


def test_verify_counts_a_refused_extraction(facts, policy):
    """출력 도중 거부돼도 청구된다. 실패로 끝나도 나간 돈은 센다."""
    meter = Spend()
    with pytest.raises(Refusal):
        verify(TEXT, FakeClient(CUT, "refusal", REFUSAL, output_tokens=7), "key", facts, policy,
               today="2026-10-01", meter=meter)
    assert meter.claude_calls == {"구조화 추출": 1} and meter.claude_usd > 0


def test_demo_prints_what_each_request_spent(monkeypatch, capsys):
    """요청마다 서버 창에 그 요청의 돈과 서버를 켠 뒤 누적을 찍는다. 돈이 드는 호출이 없던 요청은 찍지 않는다."""
    tmap_answers(monkeypatch)
    c = TestClient(create_app(Claude(T01), "key", today="2026-10-01"), base_url="http://127.0.0.1")
    first = c.post("/api/check", json={"text": TEXT}).json()
    out = capsys.readouterr().out
    assert "[비용] /api/check — Anthropic $0.0014 — 구조화 추출 1건 / TMAP 장소 검색 2건" in out
    c.post("/api/check", json={"text": TEXT})
    assert "서버를 켠 뒤 누적 — Anthropic $0.0028 — 구조화 추출 2건" in capsys.readouterr().out

    no_tmap = TestClient(create_app(Claude(T01), None, today="2026-10-01"), base_url="http://127.0.0.1")
    no_tmap.post("/api/recheck", json={"original": first["itinerary"], "revised": first["itinerary"]})
    assert "[비용]" not in capsys.readouterr().out
