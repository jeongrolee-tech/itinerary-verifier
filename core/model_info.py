"""
모델 정보 — 기본 모델 · 단가 · 모델별로 받는 옵션 · 비용 계산. 추출(run_extract.py)과 비교 실험(compare.py)이 같이 쓴다.

단가와 거부 청구 규칙은 공식 문서에서 2026-10-01 에 확인했다.
https://platform.claude.com/docs/en/about-claude/pricing
https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#how-refusals-are-billed
"""

from __future__ import annotations

DEFAULT_MODEL = "claude-opus-5-5"

# $/MTok. 캐시 쓰기는 5분 캐시(cache_control 의 ephemeral 기본값) 단가다.
# ⭐ 캐시 읽기 배수는 모델마다 다르다. 대부분 입력의 0.1배지만 Opus 5.5 는 0.05배다.
#    입력 단가에 배수를 곱해 계산하면 모델을 바꿀 때 비용이 조용히 틀린다. 그래서 값으로 적는다.
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}

# effort 를 받지 않는 모델. output_config.effort 를 보내면 400 이 난다.
NO_EFFORT = {"claude-haiku-4-5"}

# 출력 전에 거부되면 이 분류일 때만 청구된다. 다른 분류나 분류 없음(null)은 usage 에 토큰이
# 찍혀 있어도 청구되지 않는다. 출력이 나온 뒤의 거부는 분류와 상관없이 입력과 나온 출력이
# 정상 단가로 청구된다. 문서에 이 분류는 바뀔 수 있다고 적혀 있어서 확인한 날짜와 함께 둔다.
BILLED_REFUSAL_BEFORE_OUTPUT = {"bio", "frontier_llm", "reasoning_extraction"}


def effort_for(model: str, requested: str) -> str | None:
    """이 모델에 실제로 보낼 effort. 받지 않는 모델이면 None 이고, 기록에도 None 으로 남는다."""
    return None if model in NO_EFFORT else requested


def billed(usage: dict) -> bool:
    """이 호출이 청구되는가. 거부가 아니면 언제나 청구된다. usage 는 llm.ask 가 만든 것이다."""
    if usage["stop_reason"] != "refusal":
        return True
    # 출력이 나왔는지는 출력 토큰으로 본다 — 출력 전 거부는 출력 토큰이 0 이다
    return usage["output_tokens"] > 0 or usage["refusal_category"] in BILLED_REFUSAL_BEFORE_OUTPUT


def cost_usd(model: str, usage: dict) -> float | None:
    """호출 한 번의 비용. 단가를 모르는 모델이면 None — 지어낸 단가로 비용을 적지 않는다."""
    p = PRICES.get(model)
    if p is None:
        return None
    if not billed(usage):
        return 0.0
    return (usage["input_tokens"] * p["input"] + usage["output_tokens"] * p["output"]
            + usage["cache_write"] * p["cache_write"] + usage["cache_read"] * p["cache_read"]) / 1e6
