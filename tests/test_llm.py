"""
Claude 호출 — 정상 응답 · 거부 · 잘린 응답을 SDK 의 실제 응답 타입(anthropic.types.Message)으로 흘려 본다.

SDK 의 parse() 를 쓰던 때는 출력 도중 거부돼 JSON 이 잘린 응답에서, 거부를 확인하기도 전에
ValidationError 로 실행이 멈췄다. 손으로 만든 가짜 응답으로 시험했을 때는 SDK 의 그 단계를
건너뛰어서 보이지 않았다. 그래서 응답은 SDK 의 Message 로 만든다.
"""

from types import SimpleNamespace

import pytest
from anthropic.types import Message
from pydantic import BaseModel

from llm import BatchMiss, InvalidOutput, Refusal, ask, ask_many

REFUSAL = {"type": "refusal", "category": "cyber", "explanation": "declined"}
ANSWER = [{"type": "text", "text": '{"verdict": "feasible"}'}]
CUT = [{"type": "text", "text": '{"verd'}]  # 도중에 끊긴 JSON


class Answer(BaseModel):
    verdict: str


class FakeClient:
    """messages.create 를 받아 정해 둔 응답을 돌려준다. 받은 인자는 sent 에 남는다."""

    def __init__(self, content, stop_reason, stop_details=None, output_tokens=5):
        self.sent = {}
        self.messages = self
        self.reply = Message.model_validate({
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": 412, "output_tokens": output_tokens,
                      "cache_read_input_tokens": None, "cache_creation_input_tokens": 3000},
        })

    def create(self, **kw):
        self.sent = kw
        return self.reply


def call(client, model="claude-opus-5-5", effort="low"):
    return ask(client, model=model, effort=effort, system="system", content="content", schema=Answer)


def test_answer_is_validated_against_the_schema():
    result, usage = call(FakeClient(ANSWER, "end_turn"))
    assert result == Answer(verdict="feasible")
    assert usage["stop_reason"] == "end_turn"
    assert usage["cache_read"] == 0  # SDK 가 None 으로 주면 0 으로 센다 — 비용 계산이 깨지지 않게
    assert usage["cache_write"] == 3000


def test_refusal_after_partial_output_is_a_refusal_not_a_validation_error():
    """잘린 JSON 이 와도 검증하기 전에 거부를 본다. 부분 출력은 버린다(공식 문서)."""
    with pytest.raises(Refusal) as e:
        call(FakeClient(CUT, "refusal", REFUSAL, output_tokens=7))
    assert e.value.usage["refusal_category"] == "cyber"
    assert e.value.usage["output_tokens"] == 7  # 청구 여부(model_info.billed)를 여기서 본다


def test_refusal_before_output():
    with pytest.raises(Refusal) as e:
        call(FakeClient([], "refusal", REFUSAL, output_tokens=0))
    assert e.value.usage["input_tokens"] == 412


def test_cut_off_answer_is_invalid_output_with_usage():
    """max_tokens 에 걸려 잘린 응답은 검증하지 않는다. 왜 멈췄는지 보이게 하고, 청구된 사용량을 함께 넘긴다."""
    with pytest.raises(InvalidOutput, match="max_tokens") as e:
        call(FakeClient(CUT, "max_tokens", output_tokens=16000))
    assert e.value.usage["output_tokens"] == 16000


def test_answer_that_breaks_the_schema_is_invalid_output():
    """끝까지 왔지만 스키마에 맞지 않는 답은 InvalidOutput 이다. 어디가 틀렸는지 적고, 사용량을 함께 넘긴다."""
    with pytest.raises(InvalidOutput, match="verdict") as e:
        call(FakeClient([{"type": "text", "text": "{}"}], "end_turn"))
    assert e.value.usage["input_tokens"] == 412


def test_request_carries_schema_effort_and_cached_system():
    client = FakeClient(ANSWER, "end_turn")
    call(client)
    assert client.sent["output_config"]["effort"] == "low"
    assert client.sent["output_config"]["format"]["type"] == "json_schema"
    assert client.sent["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_no_effort_is_sent_when_the_model_takes_none():
    """effort_for 가 None 을 주면(Haiku 4.5) effort 를 보내지 않는다 — 보내면 400 이다."""
    client = FakeClient(ANSWER, "end_turn")
    call(client, model="claude-haiku-4-5", effort=None)
    assert "effort" not in client.sent["output_config"]


# ── 배치 — 첫 요청은 바로, 나머지는 한 묶음으로(값이 절반) ──────────────────────

def message(content, stop_reason, stop_details=None, output_tokens=5):
    return Message.model_validate({
        "id": "msg_batch", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": {"input_tokens": 300, "output_tokens": output_tokens,
                  "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0}})


class BatchClient:
    """messages.create(바로 부르기)와 messages.batches(배치)를 흉내 낸다. 배치는 한 번 물으면 끝났다고 답한다.
    outcomes 는 배치 요청 순서대로 (결과 종류, 메시지)다."""

    def __init__(self, outcomes):
        self.messages = self.batches = self
        self.outcomes, self.direct, self.batched, self.asked = outcomes, [], None, 0

    def create(self, **kw):
        if "requests" in kw:  # messages.batches.create
            self.batched = kw["requests"]
            return SimpleNamespace(id="msgbatch_test", processing_status="in_progress",
                                   request_counts=SimpleNamespace(processing=len(self.batched)))
        self.direct.append(kw)  # messages.create
        return message(ANSWER, "end_turn")

    def retrieve(self, batch_id):
        self.asked += 1
        return SimpleNamespace(id=batch_id, processing_status="ended", request_counts=SimpleNamespace(processing=0))

    def results(self, batch_id):
        for req, (kind, msg) in zip(self.batched, self.outcomes):
            yield SimpleNamespace(custom_id=req["custom_id"], result=SimpleNamespace(type=kind, message=msg))


def many(client, names):
    req = {"model": "claude-opus-5-5", "effort": "low", "system": "system", "content": "content", "schema": Answer}
    return ask_many(client, {n: req for n in names}, wait=lambda s: None, log=lambda *a: None)


def test_first_request_warms_the_cache_and_the_rest_go_in_one_batch():
    """첫 요청만 바로 부른다 — 배치 안에서는 캐시가 '되는 대로' 맞아서, 먼저 캐시를 써 둔다. 나머지는 배치 하나다."""
    client = BatchClient([("succeeded", message(ANSWER, "end_turn"))] * 2)
    got = many(client, ["T01-첫째", "T02-둘째", "T03-셋째"])
    assert len(client.direct) == 1 and len(client.batched) == 2
    assert all(r["custom_id"].isascii() for r in client.batched)  # 문제 이름(한글)은 custom_id 로 못 쓴다
    assert client.batched[0]["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert got["T01-첫째"][0] == Answer(verdict="feasible") and "batch" not in got["T01-첫째"][1]
    assert got["T02-둘째"][1]["batch"] is True and got["T02-둘째"][1]["latency_ms"] is None


def test_batch_refusal_and_missing_answers_come_back_in_place():
    """배치 안의 거부는 Refusal, 답이 오지 않은 요청(errored · expired)은 BatchMiss 로 그 자리에 온다."""
    client = BatchClient([("succeeded", message(CUT, "refusal", REFUSAL, output_tokens=7)),
                          ("errored", None), ("expired", None)])
    got = many(client, ["a", "b", "c", "d"])
    assert isinstance(got["b"], Refusal) and got["b"].usage["batch"] is True
    assert isinstance(got["c"], BatchMiss) and isinstance(got["d"], BatchMiss)


def test_one_request_needs_no_batch():
    client = BatchClient([])
    many(client, ["a"])
    assert len(client.direct) == 1 and client.batched is None
