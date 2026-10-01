"""
모델 정보 — 기본 모델 · 단가 · 모델별 옵션 · 비용.

비교 실험의 비용 · 지연은 결과의 일부로 README 에 인용된다. 단가가 틀리면 그 숫자가
틀리고, 모델이 받지 않는 옵션을 보내면 실행 자체가 실패한다.
"""

import pytest

from model_info import DEFAULT_MODEL, PRICES, billed, cost_usd, effort_for


def usage(input_tokens=0, output_tokens=0, cache_write=0, cache_read=0,
          stop_reason="end_turn", refusal_category=None):
    """llm.ask 가 만드는 사용량 중 비용 계산에 쓰는 칸만."""
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_write": cache_write,
            "cache_read": cache_read, "stop_reason": stop_reason, "refusal_category": refusal_category}


def test_default_model_has_a_price():
    assert DEFAULT_MODEL in PRICES


def test_opus_5_5_cache_read_is_not_the_usual_tenth():
    """대부분 캐시 읽기는 입력의 0.1배지만 Opus 5.5 는 0.05배다. 배수로 계산하면 여기서 틀린다."""
    assert PRICES["claude-opus-5"]["cache_read"] == pytest.approx(PRICES["claude-opus-5"]["input"] * 0.1)
    assert PRICES["claude-opus-5-5"]["cache_read"] == pytest.approx(PRICES["claude-opus-5-5"]["input"] * 0.05)


def test_cost_adds_every_token_kind():
    # Opus 5 에서 네 종류를 백만 토큰씩 — 5 + 25 + 6.25 + 0.5
    assert cost_usd("claude-opus-5", usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)) == pytest.approx(36.75)


def test_unknown_model_has_no_cost():
    """단가를 모르는 모델에 다른 모델 단가를 빌려 쓰면 틀린 비용이 기록된다."""
    assert cost_usd("claude-unknown", usage(1000, 1000)) is None


def test_refusal_before_output_is_billed_only_in_listed_categories():
    """출력 전 거부는 bio · frontier_llm · reasoning_extraction 일 때만 청구된다(공식 문서).

    usage 에 입력 토큰이 찍혀 있어도 다른 분류면 청구되지 않는다. 그대로 곱하면 비용이 부풀려진다.
    """
    assert not billed(usage(412, 0, stop_reason="refusal", refusal_category="cyber"))
    assert not billed(usage(412, 0, stop_reason="refusal", refusal_category=None))
    assert billed(usage(412, 0, stop_reason="refusal", refusal_category="bio"))
    assert cost_usd(DEFAULT_MODEL, usage(412, 0, stop_reason="refusal", refusal_category="cyber")) == 0.0


def test_refusal_after_output_is_billed():
    """출력이 나온 뒤 거부되면 분류와 상관없이 입력과 나온 출력이 청구된다."""
    assert billed(usage(412, 7, stop_reason="refusal", refusal_category="cyber"))


def test_haiku_is_not_sent_effort():
    """Haiku 4.5 는 effort 를 받지 않는다. 보내면 400 으로 실행이 실패한다."""
    assert effort_for("claude-haiku-4-5", "low") is None
    assert effort_for(DEFAULT_MODEL, "low") == "low"
