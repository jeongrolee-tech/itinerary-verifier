"""
응답 캐시 — 같은 요청이면 다시 부르지 않고, 받은 응답은 받는 즉시 파일에 남고, 돈이 들기 전에 묻는다.

2026-10-12 ~ 13 에 화면 글만 바꾸고 전체를 다시 돌려 같은 요청에 돈을 두 번 냈고, 잔액이 떨어져 멈춘 실행은
그때까지 받은 응답을 버렸다. 그 두 가지가 다시 일어나지 않는지를 여기서 본다.
"""

import json
import re
from pathlib import Path

import pytest
from anthropic.types import Message
from pydantic import BaseModel

from llm import ask
from response_cache import ResponseCache, confirm_spend, cost_summary, request_key
from runmeta import fingerprint

CORE = Path(__file__).parent.parent / "core"


class Answer(BaseModel):
    verdict: str


class Other(BaseModel):
    verdict: str
    reason: str


ASK = {"model": "claude-opus-5-5", "effort": "low", "system": "system", "content": "content", "schema": Answer}
USAGE = {"model": "claude-opus-5-5", "effort": "low", "latency_ms": 900, "input_tokens": 100, "output_tokens": 50,
         "cache_read": 0, "cache_write": 0, "stop_reason": "end_turn", "refusal_category": None,
         "refusal_explanation": None}


# ── 요청 지문 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("change", [
    {"model": "claude-sonnet-5-5"}, {"effort": None}, {"system": "system."}, {"content": "content."},
    {"schema": Other}, {"max_tokens": 100},
])
def test_key_changes_when_anything_the_model_receives_changes(change):
    """프롬프트 · 본문 · 출력 스키마 · 모델 · effort · max_tokens 가운데 하나라도 다르면 다른 요청이다."""
    assert request_key(**{**ASK, **change}) != request_key(**ASK)


def test_same_request_gives_the_same_key():
    assert request_key(**ASK) == request_key(**dict(ASK))


def test_key_is_made_from_what_ask_actually_sends():
    """지문과 보낸 것이 따로 놀면 다른 요청에 받아 둔 응답을 쓰게 된다. 그래서 ask 가 보낸 인자로 지문을 다시 만들어 본다."""
    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            self.sent = kw
            return Message.model_validate({
                "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": '{"verdict": "feasible"}'}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    client = Client()
    ask(client, **ASK)
    assert fingerprint(json.dumps(client.sent, ensure_ascii=False, sort_keys=True)) == request_key(**ASK)


# ── 저장 ────────────────────────────────────────────────────────────
def test_response_is_on_disk_as_soon_as_it_is_received(tmp_path):
    """다음 호출에서 실행이 멈춰도, 파일을 다시 열면 돈을 낸 응답이 그대로 있다."""
    path = tmp_path / "response-cache.json"
    ResponseCache(path).put("k1", run="extract-1", label="extract", result={"extracted": {"date": None}}, usage=USAGE)
    again = ResponseCache(path)
    assert again.get("k1")["result"] == {"extracted": {"date": None}}
    assert again.get("k1")["run"] == "extract-1" and again.get("k1")["usage"] == USAGE
    assert again.get("k2") is None


def test_later_responses_keep_earlier_ones(tmp_path):
    path = tmp_path / "response-cache.json"
    cache = ResponseCache(path)
    cache.put("k1", run="cmp-1", label="llm_naive", result={"verdict": "feasible"}, usage=USAGE)
    cache.put("k2", run="cmp-1", label="llm_only", result={"verdict": "undetermined"}, usage=USAGE)
    assert set(ResponseCache(path).responses) == {"k1", "k2"}
    assert not list(tmp_path.glob("*.tmp"))  # 쓰다 만 파일을 남기지 않는다


def test_a_new_response_to_the_same_request_replaces_the_old_one(tmp_path):
    """--fresh 로 새로 받으면 그 요청의 응답이 바뀐다. 옛 응답은 git 기록에 남는다."""
    cache = ResponseCache(tmp_path / "response-cache.json")
    cache.put("k1", run="cmp-1", label="llm_naive", result={"verdict": "feasible"}, usage=USAGE)
    cache.put("k1", run="cmp-2", label="llm_naive", result={"verdict": "infeasible"}, usage=USAGE)
    assert cache.get("k1")["run"] == "cmp-2" and cache.get("k1")["result"] == {"verdict": "infeasible"}


def test_user_itineraries_never_reach_the_cache():
    """실제 사용 경로는 응답 캐시를 쓰지 않는다 — 사용자가 넣은 일정을 저장하지 않는다."""
    for name in ("verify.py", "app.py", "extract.py", "llm.py"):
        source = (CORE / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import) response_cache", source, re.M), name


# ── 돈이 들기 전에 묻는다 ────────────────────────────────────────────
def test_nothing_new_means_no_question_and_no_cost(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("새로 부를 것이 없는데 물었다"))
    confirm_spend({"구조화 추출": 0}, 29, {"구조화 추출": 0.0077})
    out = capsys.readouterr().out
    assert "새로 부를 호출은 0건" in out and "API 를 부르지 않는다" in out


def test_new_calls_show_how_many_and_how_much(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    confirm_spend({"LLM에 그냥 물음": 0, "LLM에 사실 데이터 줌": 29}, 58,
                  {"LLM에 그냥 물음": 0.0218, "LLM에 사실 데이터 줌": 0.0566})
    out = capsys.readouterr().out
    assert "받아 둔 응답 58건" in out and "새로 부를 호출은 29건" in out
    assert "예상 비용 약 $1.64" in out


@pytest.mark.parametrize("answer", ["", "n", "N", "yes?"])
def test_anything_but_y_stops_before_spending(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    with pytest.raises(SystemExit):
        confirm_spend({"구조화 추출": 3}, 26, {"구조화 추출": 0.0077})


def test_no_one_to_answer_stops_before_spending(monkeypatch):
    """입력을 받을 수 없는 곳(예: 자동 실행)에서는 부르지 않는다."""
    def closed(*_):
        raise EOFError
    monkeypatch.setattr("builtins.input", closed)
    with pytest.raises(SystemExit):
        confirm_spend({"구조화 추출": 3}, 26, {"구조화 추출": 0.0077})


def test_unknown_cost_is_said_not_guessed(monkeypatch, capsys):
    """견줄 지난 기록이 없으면 예상 비용을 지어내지 않는다."""
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    confirm_spend({"구조화 추출": 3}, 0, {"구조화 추출": None})
    out = capsys.readouterr().out
    assert "비용 모름" in out and "예상 비용" not in out


# ── 비용과 지연 ──────────────────────────────────────────────────────
def test_cost_comes_from_each_call_usage():
    used = [{"usage": {**USAGE, "latency_ms": ms}, "run": "cmp-2", "label": "llm_naive"} for ms in (300, 100, 200)]
    cost = cost_summary("claude-opus-5-5", used, {})
    assert cost["n"] == 3 and cost["latency_median_ms"] == 200
    assert cost["total_usd"] == round(3 * (100 * 4.00 + 50 * 20.00) / 1e6, 4)
    assert cost["per_case_usd"] == round((100 * 4.00 + 50 * 20.00) / 1e6, 4)


def test_unknown_price_leaves_cost_empty_but_keeps_latency():
    used = [{"usage": USAGE, "run": "cmp-2", "label": "llm_naive"}]
    cost = cost_summary("no-such-model", used, {})
    assert cost["total_usd"] is None and cost["latency_median_ms"] == 900


TOTALS = {"cmp-1": {"llm_naive": {"n": 2, "total_usd": 0.04, "per_case_usd": 0.02, "latency_median_ms": 10000}}}


def test_responses_without_call_usage_carry_the_total_of_their_run():
    """2026-10-12 비교 실험 응답은 방식별 합계만 남아 있다. 그 묶음을 통째로 쓰면 그 합계를 옮겨 적는다."""
    used = [{"usage": None, "run": "cmp-1", "label": "llm_naive"}] * 2
    assert cost_summary("claude-opus-5-5", used, TOTALS) == TOTALS["cmp-1"]["llm_naive"]


def test_a_whole_batch_total_adds_to_new_calls():
    """묶음을 통째로 쓰고 새 문제의 응답이 더해졌으면 합계에 새 호출의 비용을 더한다. 중앙값은 합칠 수 없어 비운다."""
    used = [{"usage": None, "run": "cmp-1", "label": "llm_naive"}] * 2 + [
        {"usage": USAGE, "run": "cmp-2", "label": "llm_naive"}]
    cost = cost_summary("claude-opus-5-5", used, TOTALS)
    assert cost["n"] == 3 and cost["latency_median_ms"] is None
    assert cost["total_usd"] == round(0.04 + (100 * 4.00 + 50 * 20.00) / 1e6, 4)
    assert cost["per_case_usd"] == round(cost["total_usd"] / 3, 4)


def test_a_total_is_not_split_when_part_of_the_batch_was_replaced():
    """합계만 아는 묶음의 일부가 새 응답으로 바뀌었으면 나눌 수 없다. 지어내지 않고 비워 둔다."""
    used = [{"usage": None, "run": "cmp-1", "label": "llm_naive"}, {"usage": USAGE, "run": "cmp-2", "label": "llm_naive"}]
    cost = cost_summary("claude-opus-5-5", used, TOTALS)
    assert cost == {"n": 2, "total_usd": None, "per_case_usd": None, "latency_median_ms": None}
