"""Claude 를 구조화 출력으로 부르고 받은 답을 검증한다. 여러 건은 배치로 맡긴다."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from anthropic import transform_schema
from pydantic import ValidationError


class Refusal(Exception):
    """거부된 호출도 청구될 수 있어 사용량을 함께 넘긴다."""

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


class InvalidOutput(Exception):
    """응답이 잘렸거나 스키마에 맞지 않는다. 청구된 호출이라 사용량을 함께 넘긴다."""

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


class BatchMiss(Exception):
    """배치에서 답이 오지 않았다. 배치 문서 'Retrieving batch results' 에 따라 청구되지 않는다."""


def request_params(*, model: str, effort: str | None, system: str, content, schema,
                   max_tokens: int = 16000) -> dict:
    """모델에 보내는 것 전부다. 응답 캐시가 이것으로 요청 지문을 만든다."""
    output_config = {"format": {"type": "json_schema", "schema": transform_schema(schema)}}
    if effort:
        output_config["effort"] = effort
    return {"model": model, "max_tokens": max_tokens, "output_config": output_config,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}]}


def ask(client, *, model: str, effort: str | None, system: str, content, schema,
        max_tokens: int = 16000):
    """거부면 Refusal, 잘렸거나 스키마에 맞지 않으면 InvalidOutput 을 낸다."""
    # parse() 는 거부로 잘린 JSON 도 검증하다가 멈춘다. create 로 받아 직접 검증한다.
    params = request_params(model=model, effort=effort, system=system, content=content, schema=schema,
                            max_tokens=max_tokens)
    started = datetime.now(timezone.utc)
    r = client.messages.create(**params)
    latency = round((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return _answer(r, schema, _usage(r, model, effort, latency), max_tokens)


def _usage(r, model: str, effort: str | None, latency_ms: int | None, batch: bool = False) -> dict:
    details = getattr(r, "stop_details", None)  # 거부가 아니면 None 이다
    usage = {
        "model": model,
        "effort": effort,
        "latency_ms": latency_ms,
        # input_tokens 에는 캐시 토큰이 빠져 있어 따로 받아야 비용이 맞는다.
        "input_tokens": r.usage.input_tokens,
        "output_tokens": r.usage.output_tokens,
        "cache_read": r.usage.cache_read_input_tokens or 0,
        "cache_write": r.usage.cache_creation_input_tokens or 0,
        "stop_reason": r.stop_reason,
        "refusal_category": getattr(details, "category", None),
        "refusal_explanation": getattr(details, "explanation", None),
    }
    return {**usage, "batch": True} if batch else usage


def _answer(r, schema, usage: dict, max_tokens: int):
    # 거부를 다른 모델로 넘기지 않는다. 다른 모델이 답하면 평가할 모델의 결과가 아니다.
    if r.stop_reason == "refusal":
        raise Refusal(f"{usage['refusal_category']} — {usage['refusal_explanation']}", usage)
    if r.stop_reason != "end_turn":
        raise InvalidOutput(f"응답이 끝까지 오지 않았다: stop_reason={r.stop_reason} (max_tokens={max_tokens})",
                            usage)
    text = next(b.text for b in r.content if b.type == "text")
    try:
        return schema.model_validate_json(text), usage
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "본문"
        raise InvalidOutput(f"스키마에 맞지 않는다 ({e.error_count()}곳) — {where}: {first['msg']}", usage) from e


def ask_many(client, requests: dict[str, dict], *, poll_seconds: float = 30, wait=time.sleep, log=print) -> dict:
    """답을 쓸 수 없는 요청은 그 자리에 예외를 담아 돌려준다. API 오류는 그대로 올린다."""
    # 배치 안의 요청은 동시에 처리돼 캐시를 놓치기 쉽다. 첫 요청은 바로 불러 캐시를 데운다.
    names = list(requests)
    out: dict = {}
    if not names:
        return out
    try:
        out[names[0]] = ask(client, **requests[names[0]])
    except (Refusal, InvalidOutput) as e:
        out[names[0]] = e
    ids = {f"q{i:04d}": name for i, name in enumerate(names[1:])}  # custom_id 는 영문, 숫자, -, _ 만 받는다
    if not ids:
        return out
    batch = client.messages.batches.create(requests=[
        {"custom_id": cid, "params": request_params(**requests[name])} for cid, name in ids.items()])
    log(f"  배치 {batch.id} — {len(ids)}건을 맡겼다. 대개 한 시간 안에 끝난다. 기다리는 동안 창을 닫지 않는다")
    shown = None
    while batch.processing_status != "ended":
        done = len(ids) - batch.request_counts.processing
        if done != shown:
            log(f"    끝난 요청 {done}/{len(ids)}")
            shown = done
        wait(poll_seconds)
        batch = client.messages.batches.retrieve(batch.id)
    for item in client.messages.batches.results(batch.id):
        name = ids.get(item.custom_id)
        if name is None:
            continue
        req, res = requests[name], item.result
        if res.type != "succeeded":
            out[name] = BatchMiss(f"배치에서 답을 받지 못했다 — {res.type}")
            continue
        usage = _usage(res.message, req["model"], req["effort"], None, batch=True)
        try:
            out[name] = _answer(res.message, req["schema"], usage, req.get("max_tokens", 16000))
        except (Refusal, InvalidOutput) as e:
            out[name] = e
    for name in ids.values():
        out.setdefault(name, BatchMiss("배치 결과에 없다"))
    return out
