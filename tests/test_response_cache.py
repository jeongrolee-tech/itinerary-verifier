"""같은 요청은 다시 부르지 않고, 응답은 받는 즉시 남기고, 돈이 들기 전에 묻는다."""

import json
import re

import pytest
from pydantic import BaseModel

from helpers import CORE, Claude
from llm import ask
from model_info import cost_usd
from response_cache import ResponseCache, confirm_spend, cost_summary, request_key
from runmeta import fingerprint


class Answer(BaseModel):
    verdict: str


class Other(BaseModel):
    verdict: str
    reason: str


ASK = {"model": "claude-opus-5-5", "effort": "low", "system": "system", "content": "content", "schema": Answer}
USAGE = {"model": "claude-opus-5-5", "effort": "low", "latency_ms": 900, "input_tokens": 100, "output_tokens": 50,
         "cache_read": 0, "cache_write": 0, "stop_reason": "end_turn", "refusal_category": None,
         "refusal_explanation": None}


# 요청 지문
@pytest.mark.parametrize("change", [
    {"model": "claude-sonnet-5-5"}, {"effort": None}, {"system": "system."}, {"content": "content."},
    {"schema": Other}, {"max_tokens": 100},
])
def test_key_changes_when_anything_the_model_receives_changes(change):
    assert request_key(**{**ASK, **change}) != request_key(**ASK)


def test_same_request_gives_the_same_key():
    assert request_key(**ASK) == request_key(**dict(ASK))


def test_key_is_made_from_what_ask_actually_sends():
    """지문과 보낸 것이 어긋나면 다른 요청의 응답을 쓰게 되어 보낸 인자로 다시 만든다."""
    client = Claude({"verdict": "feasible"})
    ask(client, **ASK)
    assert fingerprint(json.dumps(client.sent[0], ensure_ascii=False, sort_keys=True)) == request_key(**ASK)


# 저장
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
    assert not list(tmp_path.glob("*.tmp"))  # 쓰다 만 파일을 남기지 않는다.


def test_a_new_response_to_the_same_request_replaces_the_old_one(tmp_path):
    """--fresh 로 새로 받으면 그 요청의 응답이 바뀐다. 옛 응답은 git 기록에 남는다."""
    cache = ResponseCache(tmp_path / "response-cache.json")
    cache.put("k1", run="cmp-1", label="llm_naive", result={"verdict": "feasible"}, usage=USAGE)
    cache.put("k1", run="cmp-2", label="llm_naive", result={"verdict": "infeasible"}, usage=USAGE)
    assert cache.get("k1")["run"] == "cmp-2" and cache.get("k1")["result"] == {"verdict": "infeasible"}


def test_user_itineraries_never_reach_the_cache():
    """사용자가 넣은 일정을 저장하지 않게 실제 사용 경로는 캐시를 쓰지 않는다."""
    for name in ("verify.py", "app.py", "extract.py", "llm.py"):
        source = (CORE / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import) response_cache", source, re.M), name


# 돈이 들기 전 확인
def test_nothing_new_means_no_question_and_no_cost(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("새로 부를 것이 없는데 물었다"))
    confirm_spend({"구조화 추출": 0}, 29, {"구조화 추출": 0.0077})
    out = capsys.readouterr().out
    assert "새로 부를 호출은 0건" in out and "API 를 부르지 않는다" in out


def test_new_calls_show_how_many_and_how_much(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    confirm_spend({"LLM에 그냥 물음": 0, "LLM에 사실 데이터 줌": 29}, 58,
                  {"LLM에 그냥 물음": 0.0, "LLM에 사실 데이터 줌": 1.6414})
    out = capsys.readouterr().out
    assert "받아 둔 응답 58건" in out and "새로 부를 호출은 29건" in out
    assert "예상 비용 약 $1.64" in out


@pytest.mark.parametrize("answer", ["", "n", "N", "yes?"])
def test_anything_but_y_stops_before_spending(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    with pytest.raises(SystemExit):
        confirm_spend({"구조화 추출": 3}, 26, {"구조화 추출": 0.0077})


def test_no_one_to_answer_stops_before_spending(monkeypatch):
    """자동 실행처럼 입력을 받을 수 없으면 부르지 않는다."""
    def closed(*_):
        raise EOFError
    monkeypatch.setattr("builtins.input", closed)
    with pytest.raises(SystemExit):
        confirm_spend({"구조화 추출": 3}, 26, {"구조화 추출": 0.0077})


def used(input_tokens, output_tokens, cache_read=0, cache_write=0, model="claude-opus-5-5"):
    return {**USAGE, "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read": cache_read, "cache_write": cache_write}


# T30~T33 구조화 추출에서 실제로 받은 네 호출의 사용량이다. 첫 호출이 캐시를 썼다.
OCT14 = [used(117, 400, cache_write=4435), used(133, 457, cache_read=4435),
         used(161, 583, cache_read=4435), used(134, 455, cache_read=4435)]


def cache_with(tmp_path, usages, label="extract"):
    cache = ResponseCache(tmp_path / "cache.json")
    cache.responses = {f"k{i}": {"label": label, "run": "r", "result": {}, "usage": u} for i, u in enumerate(usages)}
    return cache


def test_estimate_is_not_below_what_the_same_calls_cost(tmp_path):
    """같은 네 호출이면 어림이 실제로 나간 $0.0649 보다 낮으면 안 된다."""
    spent = sum(cost_usd("claude-opus-5-5", u) for u in OCT14)
    estimate = cache_with(tmp_path, OCT14).estimate_usd("claude-opus-5-5", "extract", 4)
    assert spent == pytest.approx(0.0649, abs=1e-4)
    assert spent <= estimate < spent * 1.5  # 넉넉하되 터무니없지 않아야 한다.


def test_estimate_counts_one_cache_write_per_run(tmp_path):
    """한 건이든 네 건이든 시스템 프롬프트를 캐시에 올리는 비용은 한 번이다."""
    cache = cache_with(tmp_path, OCT14)
    one, four = (cache.estimate_usd("claude-opus-5-5", "extract", n) for n in (1, 4))
    per_call = (161 * 4.00 + 4435 * 0.20 + 583 * 20.00) / 1e6
    assert four - one == pytest.approx(3 * per_call)


def test_estimate_without_comparable_calls_is_unknown(tmp_path):
    """다른 이름 · 다른 모델의 응답으로는 어림하지 않는다. 부를 것이 없으면 0 이다."""
    cache = cache_with(tmp_path, OCT14)
    assert cache.estimate_usd("claude-opus-5-5", "llm_naive", 4) is None
    assert cache.estimate_usd("claude-sonnet-5-5", "extract", 4) is None
    assert cache.estimate_usd("claude-opus-5-5", "llm_naive", 0) == 0


def test_unknown_cost_is_said_not_guessed(monkeypatch, capsys):
    """견줄 지난 기록이 없으면 예상 비용을 지어내지 않는다."""
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    confirm_spend({"구조화 추출": 3}, 0, {"구조화 추출": None})
    out = capsys.readouterr().out
    assert "비용 모름" in out and "예상 비용" not in out


# 비용과 지연
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
    """옛 비교 실험처럼 합계만 남은 묶음을 통째로 쓰면 그 합계를 옮겨 적는다."""
    used = [{"usage": None, "run": "cmp-1", "label": "llm_naive"}] * 2
    assert cost_summary("claude-opus-5-5", used, TOTALS) == TOTALS["cmp-1"]["llm_naive"]


def test_a_whole_batch_total_adds_to_new_calls():
    """합계에 새 호출 비용을 더하고, 중앙값은 합칠 수 없어 비운다."""
    used = [{"usage": None, "run": "cmp-1", "label": "llm_naive"}] * 2 + [
        {"usage": USAGE, "run": "cmp-2", "label": "llm_naive"}]
    cost = cost_summary("claude-opus-5-5", used, TOTALS)
    assert cost["n"] == 3 and cost["latency_median_ms"] is None
    assert cost["total_usd"] == round(0.04 + (100 * 4.00 + 50 * 20.00) / 1e6, 4)
    assert cost["per_case_usd"] == round(cost["total_usd"] / 3, 4)


def test_a_total_is_not_split_when_part_of_the_batch_was_replaced():
    """합계만 아는 묶음은 일부만 나눌 수 없어 비워 둔다."""
    used = [{"usage": None, "run": "cmp-1", "label": "llm_naive"}, {"usage": USAGE, "run": "cmp-2", "label": "llm_naive"}]
    cost = cost_summary("claude-opus-5-5", used, TOTALS)
    assert cost == {"n": 2, "total_usd": None, "per_case_usd": None, "latency_median_ms": None}
