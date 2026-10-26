"""
Claude 호출 — 구조화 출력(JSON 스키마)으로 한 번 부른다. 추출(extract.py)과 비교 실험(compare.py)이 같이 쓴다.

    from llm import Refusal, ask
    result, usage = ask(client, model=..., effort="low", system=..., content=..., schema=Extraction)

평가처럼 여러 문제를 한꺼번에 부를 때는 ask_many 를 쓴다 — 첫 요청만 바로 부르고 나머지는 배치(값이 절반)로 맡긴다.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from anthropic import transform_schema
from pydantic import ValidationError


class Refusal(Exception):
    """모델이 응답을 거부했다(stop_reason == "refusal"). 메시지에 거부 분류와 설명이 들어간다.

    거부된 호출도 청구될 수 있다(model_info.billed). 비용에서 빠지지 않게 사용량을 함께 넘긴다.
    """

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


class InvalidOutput(Exception):
    """모델 출력을 쓸 수 없다 — 응답이 끝까지 오지 않았거나(max_tokens 등) 스키마 검증을 통과하지 못했다.

    거부와 같이 사용량을 함께 넘긴다. 청구된 호출이라 비용에서 빠지면 안 되고, 평가 실행은 이 케이스를
    오류로 남기고 다음으로 넘어간다.
    """

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


class BatchMiss(Exception):
    """배치에서 답이 오지 않았다 — errored · canceled · expired 이거나 결과에 없다. 이런 요청은 청구되지 않는다
    (배치 문서 'Retrieving batch results'). 부르는 쪽은 받은 답만 남기고 멈춘다. 다시 돌리면 남은 것만 부른다."""


def request_params(*, model: str, effort: str | None, system: str, content, schema,
                   max_tokens: int = 16000) -> dict:
    """모델에 보내는 것 전부. ask 가 messages.create 에 이대로 넘긴다.

    system 은 프롬프트 캐시에 올린다 — 케이스마다 같으므로 두 번째 호출부터 캐시 읽기 단가가 붙는다.
    content 는 글 하나이거나, 앞부분을 캐시에 올릴 때는 글 조각(블록) 목록이다(compare.llm_requests).
    응답 캐시(response_cache.py)는 이것으로 요청 지문을 만든다. 여기 없는 것은 모델이 받지 않으므로,
    이것이 같으면 같은 요청이다.
    """
    output_config = {"format": {"type": "json_schema", "schema": transform_schema(schema)}}
    if effort:
        output_config["effort"] = effort
    return {"model": model, "max_tokens": max_tokens, "output_config": output_config,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}]}


def ask(client, *, model: str, effort: str | None, system: str, content, schema,
        max_tokens: int = 16000):
    """한 번 불러 schema(pydantic 모델)로 검증한 결과와 사용량을 돌려준다.

    거부면 Refusal 을, 응답이 끝까지 오지 않았거나 스키마에 맞지 않으면 InvalidOutput 을 낸다.
    """
    # ⭐ SDK 의 messages.parse() 를 쓰지 않는다. parse() 는 stop_reason 을 보기 전에 본문을
    #    스키마로 검증한다(anthropic 1.7.0 · 1.11.0 의 lib/_parse/_response.py). 출력 도중 거부돼 JSON 이
    #    잘린 응답이면 거부를 확인하기도 전에 ValidationError 로 실행이 멈춘다. 공식 문서도
    #    거부된 응답의 부분 출력은 버리라고 한다. 그래서 create 로 받아 stop_reason 을 먼저 보고,
    #    끝까지 온 응답만 검증한다. 요청 스키마는 parse() 와 같은 transform_schema 로 만든다.
    params = request_params(model=model, effort=effort, system=system, content=content, schema=schema,
                            max_tokens=max_tokens)
    started = datetime.now(timezone.utc)
    r = client.messages.create(**params)
    latency = round((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return _answer(r, schema, _usage(r, model, effort, latency), max_tokens)


def _usage(r, model: str, effort: str | None, latency_ms: int | None, batch: bool = False) -> dict:
    """응답 하나의 사용량. 비용(model_info.cost_usd)과 기록에 쓴다. 배치로 받은 응답은 batch 가 붙고 지연은 모른다."""
    details = getattr(r, "stop_details", None)  # 거부가 아니면 None 이다
    usage = {
        "model": model,
        "effort": effort,
        "latency_ms": latency_ms,
        # input_tokens 에는 캐시 읽기·쓰기 토큰이 들어 있지 않다. 따로 받아야 비용이 맞는다
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
    """응답을 검증해 (결과, 사용량)을 돌려준다. 거부면 Refusal, 끝까지 오지 않았거나 스키마에 맞지 않으면 InvalidOutput."""
    # ⭐ 거부되면 다른 모델로 넘기지 않는다(fallbacks 를 켜지 않는다). 평가하는 실행에서
    #    다른 모델이 대신 답하면 그 결과는 이 모델의 결과가 아니다. 거부는 거부로 남긴다.
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
    """요청 여러 개를 부른다. 첫 요청은 바로 불러 프롬프트 캐시를 데우고, 나머지는 배치 하나로 맡겨 끝날 때까지 기다린다.

    requests 는 {이름: ask 와 같은 인자}다. 돌려주는 값은 {이름: (결과, 사용량)}이고, 답을 쓸 수 없는 요청은 그 자리에
    예외(Refusal · InvalidOutput · BatchMiss)가 온다. 첫 요청이나 배치를 만들다 난 API 오류(잔액 · 네트워크)는
    그대로 올린다 — 부르는 쪽이 멈춘다.
    """
    # ⭐ 배치(Message Batches API)는 모든 사용량을 표준 가격의 50%로 받는다(2026-10-26 확인 — 배치 문서 'Pricing').
    #    평가처럼 답을 바로 받지 않아도 되는 실행에 맞는다. 대부분 한 시간 안에 끝나고, 24시간 안에 못 끝난 요청은
    #    청구 없이 expired 로 온다. 배치 안의 요청은 한꺼번에 처리돼 프롬프트 캐시가 '되는 대로' 맞는다 — 앞 요청이
    #    캐시를 쓰기 전에 뒤 요청이 읽으러 가면 놓친다. 그래서 첫 요청 하나는 바로 불러 캐시를 데워 둔다
    names = list(requests)
    out: dict = {}
    if not names:
        return out
    try:
        out[names[0]] = ask(client, **requests[names[0]])
    except (Refusal, InvalidOutput) as e:
        out[names[0]] = e
    ids = {f"q{i:04d}": name for i, name in enumerate(names[1:])}  # custom_id 는 영문 · 숫자 · - · _ 만 받는다
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
