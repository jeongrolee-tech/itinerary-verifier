#!/usr/bin/env python3
"""일정 하나를 검증해 화면에만 찍는다. TMAP 값은 약관상 저장할 수 없어 기록도 없다."""

import json
import os
import sys
from datetime import date
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from extract import extract, to_itinerary  # noqa: E402
from llm import InvalidOutput, Refusal  # noqa: E402
from report import render, report  # noqa: E402
from routes import live_legs  # noqa: E402
from spend import Spend, counting  # noqa: E402
from verdict import EVENT_NAMES, MODE_NAMES, judge  # noqa: E402

HERE = Path(__file__).parent


def verify(text: str, claude, tmap_key: str, facts: dict, policy: dict, today: str,
           meter: Spend | None = None) -> str:
    """자연어 일정을 화면에 보여 줄 글로 만든다. meter 를 주면 돈이 드는 호출을 센다."""
    try:
        ex, usage = extract(text, claude, today=today)
    except (Refusal, InvalidOutput) as e:
        if meter:
            meter.claude("구조화 추출", e.usage)  # 거부나 형식 오류로 끝나도 청구될 수 있다
        raise
    if meter:
        meter.claude("구조화 추출", usage)
    itinerary = to_itinerary(ex)
    with counting(meter):
        legs, misses = live_legs(itinerary, tmap_key, facts, policy)
    # 받은 구간이 지하철 하한선을 대신하고, 받지 못한 구간은 저장해 둔 값을 쓴다.
    out = judge(itinerary, {**facts, "legs": {**facts["legs"], **legs}}, policy)

    lines = ["[추출한 일정]", f"  날짜 {itinerary['date'] or '없음'}"]
    for s in itinerary["stops"]:
        dwell = f"{s['dwell_minutes']}분" if "dwell_minutes" in s else "체류 미입력"
        lines.append(f"  {s.get('start', '--:--')}  {s['place']}  {dwell}")
    for hc in itinerary["hard_constraints"]:
        lines.append(f"  [필수 일정] {hc['time']}  {hc['place']}  {EVENT_NAMES.get(hc['type'], hc['type'])}")
    lines += [f"  메모: {n}" for n in ex.notes]

    if legs or misses:
        lines += ["", "[이동시간 — TMAP 예상, 저장하지 않는다]"]
        for k, leg in legs.items():
            line = f"  {k.split(':', 1)[-1].replace('|', ' → ')}  {MODE_NAMES.get(leg['mode'], leg['mode'])} {leg['minutes']}분"
            for t in leg.get("alternatives") or []:
                fare = f"(약 {t['fare']:,}원)" if t.get("fare") else ""
                line += f" · 택시 {t['minutes']}분{fare}"
            if leg.get("taxi_miss"):
                line += f" · 택시 받지 못함 [{leg['taxi_miss'].reason}]"
            lines.append(line)
        for k, miss in misses.items():
            lines.append(f"  {k.split(':', 1)[-1].replace('|', ' → ')}  받지 못함 [{miss.reason}] {miss.detail}")

    lines += ["", render(report(out))]
    return "\n".join(lines)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    text = " ".join(sys.argv[1:]).strip() or input("일정: ").strip()
    if not text:
        raise SystemExit("일정이 비어 있다.")
    load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))
    claude = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY") or getpass("Anthropic API 키 입력: ").strip())
    tmap_key = os.environ.get("TMAP_KEY") or getpass("TMAP 앱 키 입력: ").strip()
    meter = Spend()
    try:
        print(verify(text, claude, tmap_key, load("facts.json"), load("policy.json"), date.today().isoformat(),
                     meter))
    except (Refusal, InvalidOutput) as e:
        why = "모델이 거부했다" if isinstance(e, Refusal) else "모델 출력이 형식에 맞지 않았다"
        print(f"일정을 추출하지 못했다 — {why}: {e}")
        raise SystemExit(1) from e
    finally:
        print(f"\n{meter.summary()}")


if __name__ == "__main__":
    main()
