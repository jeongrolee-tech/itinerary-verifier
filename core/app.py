#!/usr/bin/env python3
"""
데모 — 자연어 입력 → 빠진 정보 질문 → 근거별 판정 → 사용자 수정 → 재검사 (2차 피드백 4번)

    python core/app.py          http://127.0.0.1:8000 에서 화면이 열린다

멘토 4번 "얇은 API와 화면으로 … 실제로 보여주세요. 현재 Python 판정 코어에는 FastAPI 정도면 충분합니다."
API 두 개와 HTML 한 장이다. 옮기기는 extract, 길찾기는 routes, 판정은 verdict, 재검사는 recheck, 질문 모으기는
report 가 하고 여기서는 잇기만 한다.

키는 서버가 읽고 브라우저로 보내지 않는다. 환경변수(ANTHROPIC_API_KEY · TMAP_KEY)에 없으면 실행할 때 묻는다.
TMAP 키가 없으면 저장해 둔 지하철 하한선으로만 판정한다. 아무것도 저장하지 않는다 — TMAP 이동시간은 판정할
때만 쓰고, 원래 일정을 처음과 같은 조건으로 다시 판정하도록 화면이 잠깐 들고 있다가 돌려준다(docs/data-policy.md).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from extract import BUFFER_RULES, extract, to_itinerary  # noqa: E402
from llm import InvalidOutput, Refusal  # noqa: E402
from recheck import recheck_revision  # noqa: E402
from report import questions, report  # noqa: E402
from routes import live_legs  # noqa: E402
from verdict import input_checks, judge, weekday_of  # noqa: E402

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))  # noqa: E731
FACTS, POLICY = load("facts.json"), load("policy.json")


def normalized(revised: dict) -> dict:
    """화면에서 고친 일정을 판정 코어 모양으로. 필수 조건의 권장 여유 기준은 종류로 정한다(추출과 같은 BUFFER_RULES)."""
    hcs = [{"id": f"HC{i}", "source": "user_stated", **hc, "buffer_rule": BUFFER_RULES.get(hc.get("type"))}
           for i, hc in enumerate(revised.get("hard_constraints") or [], 1)]
    return {**revised, "hard_constraints": hcs}


def first_legs(legs: dict) -> dict:
    """화면이 돌려준 처음 검사의 이동시간 중 다시 쓸 것. TMAP 에서 오늘 받은 구간만이다."""
    # TMAP 값은 받은 뒤 24시간 넘게 쓸 수 없다(docs/data-policy.md). 화면을 켜 둔 채 다음 날 다시 검사하면
    # 어제 받은 값으로 판정하게 되므로 받은 날이 오늘인 것만 쓴다. TMAP 에서 받은 것이 아닌 구간도 받지 않는다
    today = date.today().isoformat()
    return {k: v for k, v in legs.items()
            if isinstance(v, dict) and v.get("source") == "api_runtime" and v.get("checked_at") == today}


def needs_routes(itinerary: dict) -> bool:
    """판정 코어가 구간을 보는 일정인가. 입력이 모자라거나 날짜와 요일이 어긋나면 판정 코어는 거기서 멈추고 묻는다."""
    # 그럴 때 길찾기를 부르면 쓰지 않을 이동시간에 호출만 쓴다. 표에서 고친 일정은 추출의 형식 검증을 거치지
    # 않으므로, 읽을 수 없는 날짜 · 시각이 길찾기까지 가서 멈추지 않게 막는 것도 이 확인이다
    if input_checks(itinerary):
        return False
    stated = itinerary.get("weekday_stated")
    return not stated or stated == weekday_of(itinerary["date"])


class CheckIn(BaseModel):
    text: str


class RecheckIn(BaseModel):
    original: dict  # 처음 옮긴 일정 — 사용자가 말한 것. 바뀐 칸과 '꼭 지킨다던 것' 을 이것과 비교한다
    revised: dict   # 사용자가 표에서 고친 일정
    legs: dict = {}  # 처음 검사에서 받은 이동시간 — 원래 일정을 처음과 같은 조건으로 다시 판정한다


def create_app(claude=None, tmap_key: str | None = None, today: str | None = None) -> FastAPI:
    """앱을 만든다. 실행(main)은 진짜 키를, 테스트는 가짜 Claude 를 넣는다. today 는 연도를 푸는 기준 날짜다."""
    app = FastAPI(title="itinerary-verifier 데모")
    # 내 PC 주소로 온 요청만 받는다. 다른 사이트가 자기 도메인을 127.0.0.1 로 돌려 놓고(DNS 리바인딩) 이 서버를 부르면
    # 브라우저는 같은 사이트로 보고 막지 않는다. 그러면 남의 페이지가 이 키로 Claude 와 TMAP 을 부를 수 있다
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    def judged_legs(itinerary: dict) -> tuple[dict, dict]:
        """TMAP 으로 받은 구간과 받지 못한 이유. 키가 없거나 판정 코어가 구간을 보지 않는 일정이면 둘 다 비어 있다."""
        if not tmap_key or not needs_routes(itinerary):
            return {}, {}
        legs, misses = live_legs(itinerary, tmap_key, FACTS, POLICY)
        reasons = {k: m.reason for k, m in misses.items()}
        # 택시를 받지 못한 이유는 구간 안에 예외 객체로 들어 있다. 화면으로 보내려고 이유만 꺼낸다
        for k, leg in legs.items():
            if leg.get("taxi_miss"):
                reasons[f"{k} 택시"] = leg["taxi_miss"].reason
        return {k: {kk: vv for kk, vv in leg.items() if kk != "taxi_miss"} for k, leg in legs.items()}, reasons

    def with_legs(legs: dict) -> dict:
        """받은 구간이 저장해 둔 지하철 하한선 구간을 대신한다(verify.py 와 같다)."""
        return {**FACTS, "legs": {**FACTS["legs"], **legs}}

    @app.get("/")
    def page():
        return FileResponse(HERE / "web" / "index.html")

    @app.post("/api/check")
    def check(body: CheckIn) -> dict:
        """자연어 일정을 옮기고 판정한다. 확인하지 못한 것마다 무엇을 물어야 하는지가 같이 간다."""
        if claude is None:
            raise HTTPException(503, "Anthropic API 키가 없어 자연어를 옮길 수 없습니다. 키를 넣고 앱을 다시 켜 주세요")
        if not body.text.strip():
            raise HTTPException(422, "일정을 적어 주세요")
        try:
            ex, _ = extract(body.text, claude, today=today or date.today().isoformat())
        except Refusal as e:
            raise HTTPException(422, f"일정을 옮기지 못했습니다 — 모델이 거부했습니다: {e}") from e
        except InvalidOutput as e:
            raise HTTPException(422, f"일정을 옮기지 못했습니다 — 모델 출력이 형식에 맞지 않았습니다: {e}") from e
        except anthropic.APIError as e:
            raise HTTPException(502, f"Claude API 호출이 실패했습니다 — {type(e).__name__}") from e
        itinerary = to_itinerary(ex)
        legs, misses = judged_legs(itinerary)
        out = judge(itinerary, with_legs(legs), POLICY)
        return {"itinerary": itinerary, "notes": ex.notes, "legs": legs, "leg_misses": misses,
                "tmap": bool(tmap_key), "result": out, "report": report(out), "questions": questions(out)}

    @app.post("/api/recheck")
    def recheck(body: RecheckIn) -> dict:
        """고친 일정 전체를 다시 판정한다. 바뀐 장소만이 아니라 날짜 · 체류 · 이동 · 필수 조건을 모두 다시 본다."""
        # ⭐ 원래 일정은 처음 검사에서 받은 이동시간으로, 고친 일정은 고친 출발 시각으로 새로 받은 이동시간으로
        #    판정한다. 둘을 섞으면 같은 구간을 다른 시각의 이동시간으로 판정해 '수정 전' 판정이 처음 본 것과
        #    달라질 수 있고, 새로 받지 못한 구간에 다른 출발 시각의 값이 들어간다.
        revised = normalized(body.revised)
        legs, misses = judged_legs(revised)
        r = recheck_revision(body.original, revised, with_legs(first_legs(body.legs)), POLICY,
                             revised_facts=with_legs(legs))
        return {"legs": legs, "leg_misses": misses, "tmap": bool(tmap_key),
                "changed_fields": r["changed_fields"],
                "verdict_before": r["before"]["summary"]["verdict"],
                "verdict_after": r["after"]["summary"]["verdict"],
                "needs_confirmation": r["needs_confirmation"],
                "result": r["after"], "report": report(r["after"]), "questions": questions(r["after"])}

    return app


def main() -> None:
    import uvicorn

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    key = os.environ.get("ANTHROPIC_API_KEY") or getpass("Anthropic API 키 입력: ").strip()
    tmap_key = (os.environ.get("TMAP_KEY")
                or getpass("TMAP 앱 키 입력 (없으면 Enter — 저장해 둔 지하철 최소 이동시간으로만 판정): ").strip())
    print(f"  Anthropic 키 {'있음' if key else '없음 — 자연어 입력이 막힌다'} · "
          f"TMAP 키 {'있음' if tmap_key else '없음 — 저장해 둔 지하철 최소 이동시간으로만 판정한다'}")
    app = create_app(anthropic.Anthropic(api_key=key) if key else None, tmap_key or None)
    uvicorn.run(app, host="127.0.0.1", port=8000)  # 내 PC 에서만 열린다


if __name__ == "__main__":
    main()
