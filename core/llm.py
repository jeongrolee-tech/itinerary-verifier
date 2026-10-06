"""
Claude 호출 — 구조화 출력(JSON 스키마)으로 한 번 부른다. 추출(extract.py)과 비교 실험(compare.py)이 같이 쓴다.

    from llm import Refusal, ask
    result, usage = ask(client, model=..., effort="low", system=..., content=..., schema=Extraction)
"""

from __future__ import annotations

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


def ask(client, *, model: str, effort: str | None, system: str, content: str, schema,
        max_tokens: int = 16000):
    """한 번 불러 schema(pydantic 모델)로 검증한 결과와 사용량을 돌려준다.

    system 은 캐시한다 — 케이스마다 같으므로 두 번째 호출부터 캐시 읽기 단가가 붙는다.
    거부면 Refusal 을, 응답이 끝까지 오지 않았거나 스키마에 맞지 않으면 InvalidOutput 을 낸다.
    """
    # ⭐ SDK 의 messages.parse() 를 쓰지 않는다. parse() 는 stop_reason 을 보기 전에 본문을
    #    스키마로 검증한다(anthropic 1.7.0 · 1.11.0 의 lib/_parse/_response.py). 출력 도중 거부돼 JSON 이
    #    잘린 응답이면 거부를 확인하기도 전에 ValidationError 로 실행이 멈춘다. 공식 문서도
    #    거부된 응답의 부분 출력은 버리라고 한다. 그래서 create 로 받아 stop_reason 을 먼저 보고,
    #    끝까지 온 응답만 검증한다. 요청 스키마는 parse() 와 같은 transform_schema 로 만든다.
    output_config = {"format": {"type": "json_schema", "schema": transform_schema(schema)}}
    if effort:
        output_config["effort"] = effort
    started = datetime.now(timezone.utc)
    r = client.messages.create(
        model=model, max_tokens=max_tokens, output_config=output_config,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    details = getattr(r, "stop_details", None)  # 거부가 아니면 None 이다
    usage = {
        "model": model,
        "effort": effort,
        "latency_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
        # input_tokens 에는 캐시 읽기·쓰기 토큰이 들어 있지 않다. 따로 받아야 비용이 맞는다
        "input_tokens": r.usage.input_tokens,
        "output_tokens": r.usage.output_tokens,
        "cache_read": r.usage.cache_read_input_tokens or 0,
        "cache_write": r.usage.cache_creation_input_tokens or 0,
        "stop_reason": r.stop_reason,
        "refusal_category": getattr(details, "category", None),
        "refusal_explanation": getattr(details, "explanation", None),
    }
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
