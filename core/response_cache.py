"""돈을 내고 받은 LLM 응답을 저장해 같은 요청은 다시 부르지 않는다."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from llm import request_params
from model_info import BATCH_DISCOUNT, PRICES, cost_usd
from runmeta import fingerprint

PATH = Path(__file__).parent / "response-cache.json"
ROLE = ("응답 캐시. 평가 실행(run_extract.py · compare.py)이 받은 LLM 응답을 요청 지문별로 둔다. "
        "같은 요청이면 다시 부르지 않고 여기 것을 쓴다. 손으로 고치지 않는다.")


def request_key(**ask_args) -> str:
    """모델에 보내는 것으로만 만든다. 코드나 화면 글이 바뀌어도 지문은 같다."""
    return fingerprint(json.dumps(request_params(**ask_args), ensure_ascii=False, sort_keys=True))


class ResponseCache:
    """평가 실행만 쓴다. 사용자가 넣은 일정은 어디에도 저장하지 않는다."""

    def __init__(self, path: Path = PATH):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.responses: dict[str, dict] = data.get("responses", {})
        # 호출별 사용량이 없는 옛 응답은 실행과 방식별 합계만 있다.
        self.totals: dict[str, dict] = data.get("totals", {})

    def get(self, key: str) -> dict | None:
        return self.responses.get(key)

    def put(self, key: str, *, run: str, label: str, result: dict, usage: dict) -> dict:
        """받는 즉시 파일에 쓴다. 도중에 멈춰도 돈을 낸 응답은 남는다."""
        self.responses[key] = {"label": label, "run": run, "received_at": datetime.now(timezone.utc).isoformat(),
                               "result": result, "usage": usage}
        body = json.dumps({"_역할": ROLE, "responses": self.responses, "totals": self.totals},
                          ensure_ascii=False, indent=1)
        # 임시 파일에 다 쓰고 바꿔 끼운다. 쓰다 멈춰도 캐시 파일이 깨지지 않는다.
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.path)
        return self.responses[key]

    def estimate_usd(self, model: str, label: str, n: int, batch: bool = False) -> float | None:
        """평균은 실제보다 적게 나와 가장 긴 응답 기준으로 넉넉히 어림한다."""
        if not n:
            return 0.0
        usages = [r["usage"] for r in self.responses.values()
                  if r["label"] == label and r["usage"] and r["usage"]["model"] == model]
        p = PRICES.get(model)
        if not usages or p is None:
            return None
        system = max(u["cache_read"] + u["cache_write"] for u in usages)
        per_call = (max(u["input_tokens"] for u in usages) * p["input"] + system * p["cache_read"]
                    + max(u["output_tokens"] for u in usages) * p["output"]) / 1e6
        first = per_call + system * (p["cache_write"] - p["cache_read"]) / 1e6
        return first + (n - 1) * per_call * (BATCH_DISCOUNT if batch else 1)


def confirm_spend(new: dict[str, int], reused: int, estimated_usd: dict[str, float | None]) -> None:
    """부르기 전에 새 호출 건수와 예상 비용을 보여 주고, 새 호출이 있으면 y 를 받는다."""
    calls = sum(new.values())
    print(f"  응답 캐시 — 받아 둔 응답 {reused}건을 다시 쓰고, 새로 부를 호출은 {calls}건이다.")
    if not calls:
        print("  모델에 보낼 내용이 받아 둔 응답과 같다. API 를 부르지 않는다 — 이번 실행 비용 $0.\n")
        return
    costs = []
    for name, n in new.items():
        if n:
            cost = estimated_usd.get(name)
            costs.append(cost)
            print(f"    {name} {n}건  " + ("비용 모름 — 견줄 응답이 없다" if cost is None else f"약 ${cost:.2f}"))
    if None not in costs:
        print(f"  예상 비용 약 ${sum(costs):.2f}  (받아 둔 응답 중 가장 긴 입력 · 출력에 첫 호출의 프롬프트 캐시 쓰기를"
              " 더해 넉넉히 어림한 값)")
    try:
        answer = input("  API 를 부르면 돈이 든다. 계속하려면 y: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        raise SystemExit("  멈췄다. API 를 부르지 않았다.")
    print()


def cost_summary(model: str, used: list[dict], totals: dict) -> dict:
    """응답을 받을 때 든 비용과 지연이다. 이번 실행에서 나간 돈이 아니다."""
    n = len(used)
    usages = [r["usage"] for r in used if r["usage"]]
    # 사용량이 없는 옛 응답은 묶음 합계를 쓴다. 묶음 일부만 썼으면 나눌 수 없어 비워 둔다.
    batches = Counter((r["run"], r["label"]) for r in used if not r["usage"])
    whole = {b: totals.get(b[0], {}).get(b[1]) for b in batches}
    if any(not t or t["n"] != batches[b] for b, t in whole.items()):
        return {"n": n, "total_usd": None, "per_case_usd": None, "latency_median_ms": None}
    if len(whole) == 1 and not usages:
        return dict(next(iter(whole.values())))
    costs = [cost_usd(model, u) for u in usages] + [t["total_usd"] for t in whole.values()]
    cost = None if None in costs else sum(costs)
    # 중앙값은 호출별 지연이 다 있을 때만 낸다. 배치 응답은 지연을 모른다.
    known = [u["latency_ms"] for u in usages if u["latency_ms"] is not None]
    lat = sorted(known)[n // 2] if len(known) == n else None
    return {"n": n, "total_usd": None if cost is None else round(cost, 4),
            "per_case_usd": None if cost is None else round(cost / n, 4), "latency_median_ms": lat}
