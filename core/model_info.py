"""기본 모델, 단가, 모델별 옵션과 호출 비용 계산을 모아 둔다."""

from __future__ import annotations

DEFAULT_MODEL = "claude-opus-5-5"

# 캐시 읽기는 모델마다 배수가 달라 값으로 적는다. 캐시 쓰기는 5분 캐시 단가다.
# $/MTok, 2026-10-01 확인 https://platform.claude.com/docs/en/about-claude/pricing
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}

# 배치는 캐시 쓰기 · 읽기까지 모든 사용량이 절반이다.
# 2026-10-26 확인 https://platform.claude.com/docs/en/build-with-claude/batch-processing#pricing
BATCH_DISCOUNT = 0.5

# effort 를 보내면 400 이 나는 모델이다.
NO_EFFORT = {"claude-haiku-4-5"}

# 출력 전 거부는 이 분류만 청구된다. 다른 분류는 usage 에 토큰이 찍혀도 청구되지 않는다.
# 2026-10-01 확인 https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#how-refusals-are-billed
BILLED_REFUSAL_BEFORE_OUTPUT = {"bio", "frontier_llm", "reasoning_extraction"}


def effort_for(model: str, requested: str) -> str | None:
    """받지 않는 모델이면 None 이고 기록에도 None 으로 남는다."""
    return None if model in NO_EFFORT else requested


def billed(usage: dict) -> bool:
    if usage["stop_reason"] != "refusal":
        return True
    # 출력 토큰이 있으면 출력 뒤 거부라 분류와 상관없이 청구된다.
    return usage["output_tokens"] > 0 or usage["refusal_category"] in BILLED_REFUSAL_BEFORE_OUTPUT


def cost_usd(model: str, usage: dict) -> float | None:
    """단가를 모르는 모델이면 지어내지 않고 None 을 돌려준다."""
    p = PRICES.get(model)
    if p is None:
        return None
    if not billed(usage):
        return 0.0
    cost = (usage["input_tokens"] * p["input"] + usage["output_tokens"] * p["output"]
            + usage["cache_write"] * p["cache_write"] + usage["cache_read"] * p["cache_read"]) / 1e6
    return cost * BATCH_DISCOUNT if usage.get("batch") else cost
