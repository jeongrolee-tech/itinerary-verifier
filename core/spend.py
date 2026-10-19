"""
이번 실행에서 나간 돈 — Anthropic 호출과 TMAP 호출을 세서 화면에 찍는다.

    meter = Spend()
    meter.claude("구조화 추출", usage)      # llm.ask 가 준 사용량. 거부 · 형식 오류로 끝난 호출도 넣는다
    with counting(meter):                  # 이 안에서 routes.call 이 응답을 받을 때마다 meter 에 센다
        legs, misses = live_legs(...)
    print(meter.summary())

평가 실행(run_extract.py · compare.py)의 기록에 남는 '비용'은 응답을 받을 때 든 값이라, 받아 둔 응답을 다시 쓴
실행에서도 같은 숫자가 나온다. 이것은 그와 달리 이번 실행에서 실제로 나간 돈이다 — 새로 부른 호출만 센다.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar

from model_info import cost_usd

# TMAP 요금. 대중교통은 docs/data-policy.md '길찾기 플랫폼별로 되는 것'(2026-10-02 확인 — 하루 10건 무료, 넘으면 건당 0.88원).
# 무료 제공량은 https://tmapapi.tmapmobility.com 첫 화면 "POI 검색 일 2만 건, 경로탐색 일 1천건"(2026-10-14 확인).
# ⭐ 무료 제공량을 넘은 뒤의 건당 단가는 대중교통만 확인했다. 모르는 값은 None 으로 두고 지어내지 않는다.
#    무료 제공량은 하루 전체 사용량에 걸리는데, 오늘 이미 몇 건을 썼는지는 TMAP 콘솔에만 있다. 그래서 한 번의
#    실행이 쓴 돈은 범위(0원 ~ 전부 유료일 때)로만 말할 수 있다.
TMAP_PRICES = {
    "장소 검색": {"free_per_day": 20000, "won": None},
    "대중교통": {"free_per_day": 10, "won": 0.88},
    "보행자 길찾기": {"free_per_day": 1000, "won": None},
    "자동차 길찾기": {"free_per_day": 1000, "won": None},
    "택시 예측(타임머신)": {"free_per_day": None, "won": None},
}

_current: ContextVar[Spend | None] = ContextVar("spend", default=None)


class Spend:
    """한 번의 실행(또는 데모의 요청 하나)에서 돈이 드는 호출을 센다."""

    def __init__(self):
        self.claude_calls: Counter[str] = Counter()  # 이름 → 건수
        self.unpriced: Counter[str] = Counter()      # 단가를 모르는 모델로 부른 건수 — 비용에 넣지 못했다
        self.claude_usd = 0.0
        self.tmap_calls: Counter[str] = Counter()    # TMAP API 이름 → 응답을 받은 건수

    def claude(self, label: str, usage: dict) -> float | None:
        """호출 하나를 센다. 이 호출의 비용을 돌려준다 — 청구되지 않는 거부는 0, 단가를 모르는 모델이면 None."""
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
            if won is not None:
                parts.append(f"{api} {n}건(하루 {free:,}건까지 무료, 넘은 건은 건당 {won}원 — 0원 ~ {n * won:,.2f}원)")
            elif free:
                parts.append(f"{api} {n}건(하루 {free:,}건까지 무료, 넘은 뒤 단가는 확인하지 못했다)")
            else:
                parts.append(f"{api} {n}건(요금을 확인하지 못했다)")
        return "TMAP " + " · ".join(parts)

    def line(self) -> str:
        """한 줄로. 데모 서버가 요청마다 찍는다."""
        return " / ".join(x for x in (self.claude_line(), self.tmap_line()) if x) or "돈이 드는 호출 없음"

    def summary(self) -> str:
        """실행이 끝날 때 찍는 여러 줄."""
        lines = [x for x in (self.claude_line(), self.tmap_line()) if x]
        if not lines:
            return "  이번 실행에서 나간 돈 — 없다. 돈이 드는 호출을 하지 않았다."
        return "\n".join(["  이번 실행에서 나간 돈"] + [f"    {x}" for x in lines])


@contextmanager
def counting(meter: Spend | None):
    """이 안에서 일어난 TMAP 호출을 meter 에 센다. meter 가 None 이면 세지 않는다.

    ⭐ 호출마다 meter 를 넘기지 않고 '지금 세는 계량기' 를 둔다. TMAP 은 routes.call 한 곳에서 부르지만
       장소 검색 · 대중교통 · 걷기 · 택시로 여러 함수를 거쳐 가고, 테스트는 routes.call 을 가짜로 바꿔 끼운다.
       ContextVar 라서 데모 서버에 요청 두 개가 동시에 와도 각자의 계량기에 센다.
    """
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def count_tmap(api: str) -> None:
    """routes.call 이 응답을 받으면 부른다. 세는 중이 아니면 아무것도 하지 않는다."""
    meter = _current.get()
    if meter is not None:
        meter.tmap(api)
