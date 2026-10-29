"""Anthropic 과 TMAP 호출을 세서 이번 실행에서 나간 돈을 찍는다."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar

from model_info import cost_usd

# 무료 제공량은 https://tmapapi.tmapmobility.com 첫 화면 "POI 검색 일 2만 건, 경로탐색 일 1천건"(2026-10-14 확인)
# 넘은 뒤 단가는 대중교통만 확인했다. docs/data-policy.md '길찾기 플랫폼별로 되는 것'(2026-10-02 확인)
TMAP_PRICES = {
    "장소 검색": {"free_per_day": 20000, "won": None},
    "대중교통": {"free_per_day": 10, "won": 0.88},
    "보행자 길찾기": {"free_per_day": 1000, "won": None},
    "자동차 길찾기": {"free_per_day": 1000, "won": None},
    "택시 예측(타임머신)": {"free_per_day": None, "won": None},
}

_current: ContextVar[Spend | None] = ContextVar("spend", default=None)


class Spend:
    """호출마다 바로 센다. 실행이 실패로 끝나도 그때까지 나간 돈을 찍을 수 있다."""

    def __init__(self):
        self.claude_calls: Counter[str] = Counter()
        self.unpriced: Counter[str] = Counter()
        self.claude_usd = 0.0
        self.tmap_calls: Counter[str] = Counter()

    def claude(self, label: str, usage: dict) -> float | None:
        """거부나 형식 오류로 끝난 호출도 넣는다. 단가를 모르면 None 을 돌려준다."""
        cost = cost_usd(usage["model"], usage)
        self.claude_calls[label] += 1
        if cost is None:
            self.unpriced[label] += 1
        else:
            self.claude_usd += cost
        return cost

    def tmap(self, api: str) -> None:
        self.tmap_calls[api] += 1

    def merge(self, other: Spend) -> None:
        self.claude_calls.update(other.claude_calls)
        self.unpriced.update(other.unpriced)
        self.claude_usd += other.claude_usd
        self.tmap_calls.update(other.tmap_calls)

    def calls(self) -> int:
        return sum(self.claude_calls.values()) + sum(self.tmap_calls.values())

    def claude_line(self) -> str | None:
        if not self.claude_calls:
            return None
        money = f"${self.claude_usd:.4f}"
        if self.unpriced:
            money += f" + 단가를 모르는 모델 {sum(self.unpriced.values())}건(model_info.PRICES 에 없다)"
        return f"Anthropic {money} — " + " · ".join(f"{k} {n}건" for k, n in self.claude_calls.items())

    def tmap_line(self) -> str | None:
        if not self.tmap_calls:
            return None
        parts = []
        for api, n in self.tmap_calls.items():
            price = TMAP_PRICES.get(api, {})
            free, won = price.get("free_per_day"), price.get("won")
            # 하루 무료 제공량 중 오늘 쓴 건수를 몰라 범위로 찍는다.
            if won is not None:
                parts.append(f"{api} {n}건(하루 {free:,}건까지 무료, 넘은 건은 건당 {won}원 — 0원 ~ {n * won:,.2f}원)")
            elif free:
                parts.append(f"{api} {n}건(하루 {free:,}건까지 무료, 넘은 뒤 단가는 확인하지 못했다)")
            else:
                parts.append(f"{api} {n}건(요금을 확인하지 못했다)")
        return "TMAP " + " · ".join(parts)

    def line(self) -> str:
        """데모 서버가 요청마다 찍는다."""
        return " / ".join(x for x in (self.claude_line(), self.tmap_line()) if x) or "돈이 드는 호출 없음"

    def summary(self) -> str:
        """실행이 끝날 때 찍는다."""
        lines = [x for x in (self.claude_line(), self.tmap_line()) if x]
        if not lines:
            return "  이번 실행에서 나간 돈 — 없다. 돈이 드는 호출을 하지 않았다."
        return "\n".join(["  이번 실행에서 나간 돈"] + [f"    {x}" for x in lines])


@contextmanager
def counting(meter: Spend | None):
    """meter 를 함수마다 넘기지 않으려고 ContextVar 에 둔다. 동시 요청도 각자 센다."""
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def count_tmap(api: str) -> None:
    """routes.call 이 응답을 받으면 부른다."""
    meter = _current.get()
    if meter is not None:
        meter.tmap(api)
