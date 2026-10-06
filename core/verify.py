#!/usr/bin/env python3
"""
일정 하나 검증 — 자연어 일정을 옮기고(Claude), 구간마다 이동시간을 받고(TMAP), 판정한다.

    python core/verify.py "10월 8일 목요일 오후 1시에 서울시립미술관 서소문본관 한 시간, 3시에 경복궁 두 시간"
    python core/verify.py              → 일정을 물어본다

키는 환경변수(ANTHROPIC_API_KEY · TMAP_KEY)에 있으면 그걸 쓰고, 없으면 물어본다. 입력은 화면에 안 찍힌다.
TMAP 에서 받은 좌표와 이동시간은 화면에만 보여 주고 저장하지 않는다 — 약관상 24시간 넘게 쓸 수 없다.
그래서 기록 파일도 남기지 않는다. 평가 기록은 run.py · run_extract.py · compare.py 가 남긴다.
"""

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
from verdict import MODE_NAMES, judge  # noqa: E402

HERE = Path(__file__).parent


def verify(text: str, claude, tmap_key: str, facts: dict, policy: dict, today: str) -> str:
    """자연어 일정 → 화면에 보여 줄 글. 아무것도 저장하지 않는다."""
    ex, _ = extract(text, claude, today=today)
    itinerary = to_itinerary(ex)
    legs, misses = live_legs(itinerary, tmap_key)
    # 받은 구간이 저장해 둔 지하철 하한선 구간을 대신한다. 받지 못한 구간은 저장해 둔 것이 있으면 그걸 쓴다.
    out = judge(itinerary, {**facts, "legs": {**facts["legs"], **legs}}, policy)

    lines = ["[옮긴 일정]", f"  날짜 {itinerary['date'] or '없음'}"]
    for s in itinerary["stops"]:
        dwell = f"{s['dwell_minutes']}분" if "dwell_minutes" in s else "체류 미입력"
        lines.append(f"  {s.get('start', '--:--')}  {s['place']}  {dwell}")
    for hc in itinerary["hard_constraints"]:
        lines.append(f"  [필수] {hc['time']}  {hc['place']}  {hc['type']}")
    lines += [f"  메모: {n}" for n in ex.notes]

    if legs or misses:
        lines += ["", "[이동시간 — TMAP 예상, 저장하지 않는다]"]
        for k, leg in legs.items():
            line = f"  {k.replace('|', ' → ')}  {MODE_NAMES.get(leg['mode'], leg['mode'])} {leg['minutes']}분"
            for t in leg.get("alternatives") or []:
                fare = f"(약 {t['fare']:,}원)" if t.get("fare") else ""
                line += f" · 택시 {t['minutes']}분{fare}"
            if leg.get("taxi_miss"):
                line += f" · 택시 받지 못함 [{leg['taxi_miss'].reason}]"
            lines.append(line)
        for k, miss in misses.items():
            lines.append(f"  {k.replace('|', ' → ')}  받지 못함 [{miss.reason}] {miss.detail}")

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
    try:
        print(verify(text, claude, tmap_key, load("facts.json"), load("policy.json"), date.today().isoformat()))
    except Refusal as e:
        raise SystemExit(f"일정을 옮기지 못했다 — 모델이 거부했다: {e}") from e
    except InvalidOutput as e:
        raise SystemExit(f"일정을 옮기지 못했다 — 모델 출력이 형식에 맞지 않았다: {e}") from e


if __name__ == "__main__":
    main()
