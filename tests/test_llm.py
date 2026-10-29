"""Claude 호출을 본다. SDK 단계를 건너뛰지 않게 가짜 응답도 SDK Message 로 만든다."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from helpers import CUT, REFUSAL, Claude, reply
from llm import BatchMiss, InvalidOutput, Refusal, ask, ask_many

ANSWER = [{"type": "text", "text": '{"verdict": "feasible"}'}]


class Answer(BaseModel):
    verdict: str


REQ = {"model": "claude-opus-5-5", "effort": "low", "system": "system", "content": "content", "schema": Answer}


def fake(content, stop_reason, stop_details=None, output_tokens=5):
    """캐시 쓰기 3000 토큰이 찍히고 캐시 읽기는 SDK 가 None 으로 주는 응답이다."""
    return Claude(reply(content, stop_reason, stop_details, input_tokens=412, output_tokens=output_tokens,
                        cache_read_input_tokens=None, cache_creation_input_tokens=3000))


def call(client, model="claude-opus-5-5", effort="low"):
    return ask(client, **{**REQ, "model": model, "effort": effort})


def test_answer_is_validated_against_the_schema():
    result, usage = call(fake(ANSWER, "end_turn"))
    assert result == Answer(verdict="feasible")
    assert usage["stop_reason"] == "end_turn"
    assert usage["cache_read"] == 0  # SDK 가 None 을 주면 비용 계산을 위해 0 으로 센다.
    assert usage["cache_write"] == 3000


def test_refusal_after_partial_output_is_a_refusal_not_a_validation_error():
    """잘린 JSON 이 와도 검증 전에 거부를 보고, 공식 문서대로 부분 출력은 버린다."""
    with pytest.raises(Refusal) as e:
        call(fake(CUT, "refusal", REFUSAL, output_tokens=7))
    assert e.value.usage["refusal_category"] == "cyber"
    assert e.value.usage["output_tokens"] == 7  # model_info.billed 가 이 값으로 청구 여부를 본다.


def test_refusal_before_output():
    with pytest.raises(Refusal) as e:
        call(fake([], "refusal", REFUSAL, output_tokens=0))
    assert e.value.usage["input_tokens"] == 412


def test_cut_off_answer_is_invalid_output_with_usage():
    """max_tokens 로 잘린 응답은 검증하지 않고 멈춘 이유와 사용량을 넘긴다."""
    with pytest.raises(InvalidOutput, match="max_tokens") as e:
        call(fake(CUT, "max_tokens", output_tokens=16000))
    assert e.value.usage["output_tokens"] == 16000


def test_answer_that_breaks_the_schema_is_invalid_output():
    """어디가 틀렸는지 적고 사용량을 함께 넘긴다."""
    with pytest.raises(InvalidOutput, match="verdict") as e:
        call(fake([{"type": "text", "text": "{}"}], "end_turn"))
    assert e.value.usage["input_tokens"] == 412


def test_request_carries_schema_effort_and_cached_system():
    client = fake(ANSWER, "end_turn")
    call(client)
    (sent,) = client.sent
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_no_effort_is_sent_when_the_model_takes_none():
    """Haiku 4.5 에 effort 를 보내면 400 이 난다."""
    client = fake(ANSWER, "end_turn")
    call(client, model="claude-haiku-4-5", effort=None)
    assert "effort" not in client.sent[0]["output_config"]


# 배치

def batched(content, stop_reason, stop_details=None, output_tokens=5):
    return reply(content, stop_reason, stop_details, input_tokens=300, output_tokens=output_tokens,
                 cache_read_input_tokens=2000, cache_creation_input_tokens=0)


class BatchClient:
    """messages.create 와 messages.batches 를 흉내 내는 가짜 클라이언트다."""

    def __init__(self, outcomes):
        self.messages = self.batches = self
        self.outcomes, self.direct, self.batched, self.asked = outcomes, [], None, 0

    def create(self, **kw):
        if "requests" in kw:  # messages.batches.create
            self.batched = kw["requests"]
            return SimpleNamespace(id="msgbatch_test", processing_status="in_progress",
                                   request_counts=SimpleNamespace(processing=len(self.batched)))
        self.direct.append(kw)  # messages.create
        return batched(ANSWER, "end_turn")

    def retrieve(self, batch_id):
        self.asked += 1
        return SimpleNamespace(id=batch_id, processing_status="ended", request_counts=SimpleNamespace(processing=0))

    def results(self, batch_id):
        for req, (kind, msg) in zip(self.batched, self.outcomes):
            yield SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(type=kind, message=msg))


def many(client, names):
    seen = {}
    got = ask_many(client, {n: REQ for n in names}, on_answer=seen.__setitem__, wait=lambda s: None, log=lambda *a: None)
    assert seen == got  # 돌려준 답은 모두 받는 즉시 on_answer 로도 넘어갔다.
    return got


def test_first_request_warms_the_cache_and_the_rest_go_in_one_batch():
    """배치 안에서는 캐시가 되는 대로 맞아서 첫 요청으로 캐시를 먼저 써 둔다."""
    client = BatchClient([("succeeded", batched(ANSWER, "end_turn"))] * 2)
    got = many(client, ["T01-첫째", "T02-둘째", "T03-셋째"])
    assert len(client.direct) == 1 and len(client.batched) == 2
    assert all(r["custom_id"].isascii() for r in client.batched)  # 한글 문제 이름은 custom_id 로 쓸 수 없다.
    assert client.batched[0]["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert got["T01-첫째"][0] == Answer(verdict="feasible") and "batch" not in got["T01-첫째"][1]
    assert got["T02-둘째"][1]["batch"] is True and got["T02-둘째"][1]["latency_ms"] is None


def test_batch_refusal_and_missing_answers_come_back_in_place():
    """배치 안의 거부는 Refusal, errored · expired 는 BatchMiss 로 그 자리에 온다."""
    client = BatchClient([("succeeded", batched(CUT, "refusal", REFUSAL, output_tokens=7)),
                          ("errored", None), ("expired", None)])
    got = many(client, ["a", "b", "c", "d"])
    assert isinstance(got["b"], Refusal) and got["b"].usage["batch"] is True
    assert isinstance(got["c"], BatchMiss) and isinstance(got["d"], BatchMiss)


def test_one_request_needs_no_batch():
    client = BatchClient([])
    many(client, ["a"])
    assert len(client.direct) == 1 and client.batched is None


def test_paid_answer_is_handed_over_before_the_batch_fails():
    """바로 부른 첫 요청은 이미 청구됐다. 배치를 맡기다 오류가 나도 그 답은 먼저 넘어간다."""

    class Down(BatchClient):
        def create(self, **kw):
            if "requests" in kw:
                raise ConnectionError("batches.create 실패")
            return super().create(**kw)

    seen = {}
    with pytest.raises(ConnectionError):
        ask_many(Down([]), {"a": REQ, "b": REQ}, on_answer=seen.__setitem__, wait=lambda s: None, log=lambda *a: None)
    assert list(seen) == ["a"] and seen["a"][0] == Answer(verdict="feasible")
