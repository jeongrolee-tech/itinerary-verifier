"""
응답 캐시 — 돈을 내고 받은 LLM 응답을 저장해 두고, 같은 요청이면 다시 부르지 않는다.

    cache = ResponseCache()
    key = request_key(**ask_args)          # llm.ask 에 넘길 인자 그대로
    hit = cache.get(key)                   # 없으면 None
    hit = cache.put(key, run=RUN_ID, label="extract", result=..., usage=...)   # 받는 즉시 파일에 쓴다

평가 실행(run_extract.py · compare.py)만 쓴다. 실제 사용 경로(verify.py · app.py)는 쓰지 않는다 —
사용자가 넣은 일정은 어디에도 저장하지 않는다.

왜 있나: 2026-10-12 ~ 13 에 화면 글만 바꾸고 전체를 다시 돌려 같은 요청에 돈을 두 번 냈고, 잔액이 떨어져
도중에 멈춘 실행은 그때까지 받은 응답을 버렸다. 모델이 받는 것이 같으면 답을 다시 살 이유가 없다.

같은 요청인지는 요청 지문으로 본다 — 모델 · 시스템 프롬프트 · 본문 · 출력 스키마 · effort · max_tokens
(llm.request_params). 프롬프트나 사실 데이터가 한 글자라도 바뀌면 지문이 달라져 그 요청만 새로 부른다.
코드 커밋은 지문에 들어가지 않는다. 그래서 화면 글만 바꾼 커밋이나 커밋 메시지를 고친 뒤에도 기록을
지금 커밋으로 다시 만들 수 있다 — 채점과 판정은 지금 코드로 다시 하고, LLM 응답만 받아 둔 것을 쓴다.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from llm import request_params
from model_info import cost_usd
from runmeta import fingerprint

PATH = Path(__file__).parent / "response-cache.json"
ROLE = ("응답 캐시. 평가 실행(run_extract.py · compare.py)이 받은 LLM 응답을 요청 지문별로 둔다. "
        "같은 요청이면 다시 부르지 않고 여기 것을 쓴다. 손으로 고치지 않는다.")


def request_key(**ask_args) -> str:
    """요청 지문. llm.ask 가 받는 인자로 만든다 — 모델에 보내는 것이 한 글자라도 다르면 달라진다."""
    return fingerprint(json.dumps(request_params(**ask_args), ensure_ascii=False, sort_keys=True))


class ResponseCache:
    """요청 지문 → 받은 응답. put 은 받는 즉시 파일에 쓴다 — 다음 호출에서 멈춰도 돈을 낸 응답은 남는다."""

    def __init__(self, path: Path = PATH):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.responses: dict[str, dict] = data.get("responses", {})
        # 호출별 사용량을 남기기 전에 받은 응답(2026-10-12 비교 실험)의 합계. 실행 번호 → 방식 → 합계
        self.totals: dict[str, dict] = data.get("totals", {})

    def get(self, key: str) -> dict | None:
        return self.responses.get(key)

    def put(self, key: str, *, run: str, label: str, result: dict, usage: dict) -> dict:
        """result 는 채점에 쓰는 모양으로 검증을 마친 답, usage 는 그 호출의 사용량(llm.ask)이다."""
        self.responses[key] = {"label": label, "run": run, "received_at": datetime.now(timezone.utc).isoformat(),
                               "result": result, "usage": usage}
        body = json.dumps({"_역할": ROLE, "responses": self.responses, "totals": self.totals},
                          ensure_ascii=False, indent=1)
        # 다른 이름으로 다 쓴 뒤에 바꿔 끼운다. 쓰는 도중에 멈춰도 앞서 받은 응답이 깨지지 않는다
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.responses[key]


def confirm_spend(new: dict[str, int], reused: int, per_call_usd: dict[str, float | None]) -> None:
    """API 를 부르기 전에 무엇을 새로 부르고 얼마쯤 드는지 보여 준다. 새로 부를 것이 있으면 y 를 받아야 넘어간다.

    new 는 이름별 새 호출 수, per_call_usd 는 같은 이름의 지난 건당 비용(모르면 None)이다.
    """
    calls = sum(new.values())
    print(f"  응답 캐시 — 받아 둔 응답 {reused}건을 다시 쓰고, 새로 부를 호출은 {calls}건이다.")
    if not calls:
        print("  모델에 보낼 내용이 받아 둔 응답과 같다. API 를 부르지 않는다 — 이번 실행 비용 $0.\n")
        return
    costs = []
    for name, n in new.items():
        if n:
            price = per_call_usd.get(name)
            costs.append(None if price is None else n * price)
            print(f"    {name} {n}건  " + ("비용 모름 — 견줄 지난 기록이 없다" if price is None else f"약 ${n * price:.2f}"))
    if None not in costs:
        print(f"  예상 비용 약 ${sum(costs):.2f}  (지난 기록의 건당 비용으로 어림한 값)")
    try:
        answer = input("  API 를 부르면 돈이 든다. 계속하려면 y: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        raise SystemExit("  멈췄다. API 를 부르지 않았다.")
    print()


def cost_summary(model: str, used: list[dict], totals: dict) -> dict:
    """응답 묶음을 받는 데 든 비용과 지연. 받아 둔 응답도 받을 때의 사용량으로 센다 — 이번 실행에서 쓴 돈이 아니다."""
    n = len(used)
    usages = [r["usage"] for r in used]
    if all(usages):
        costs = [cost_usd(model, u) for u in usages]
        cost = None if None in costs else sum(costs)  # 단가를 모르는 모델이면 비용을 적지 않는다
        lat = sorted(u["latency_ms"] for u in usages)
        return {"n": n, "total_usd": None if cost is None else round(cost, 4),
                "per_case_usd": None if cost is None else round(cost / n, 4), "latency_median_ms": lat[n // 2]}
    # 2026-10-12 비교 실험 응답은 호출별 사용량 없이 방식별 합계만 남아 있다. 그 묶음을 통째로 쓸 때만
    # 그 합계를 옮겨 적는다. 일부가 새 응답으로 바뀌었으면 나눌 수 없으니 비워 둔다
    sources = {(r["run"], r["label"]) for r in used}
    if not any(usages) and len(sources) == 1:
        run, label = next(iter(sources))
        total = totals.get(run, {}).get(label)
        if total and total["n"] == n:
            return dict(total)
    return {"n": n, "total_usd": None, "per_case_usd": None, "latency_median_ms": None}
