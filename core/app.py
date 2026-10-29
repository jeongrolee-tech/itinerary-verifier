#!/usr/bin/env python3
"""데모 서버. 자연어 일정을 판정하고, 사용자가 고친 일정을 다시 판정한다."""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import date, timedelta
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import anthropic  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from extract import BUFFER_RULES, Itinerary, as_dict, extract, to_itinerary  # noqa: E402
from llm import InvalidOutput, Refusal  # noqa: E402
from recheck import recheck_revision  # noqa: E402
from report import questions, report, timeline  # noqa: E402
from routes import live_legs  # noqa: E402
from spend import Spend, counting  # noqa: E402
from verdict import FAIL, PASS, judge, weekday_of  # noqa: E402

HERE = Path(__file__).parent
load = lambda n: json.loads((HERE / n).read_text(encoding="utf-8"))  # noqa: E731
FACTS, POLICY = load("facts.json"), load("policy.json")


def normalized(revised: dict) -> dict:
    """화면에서 고친 일정을 판정 코어 모양으로 맞춘다."""
    hcs = [{"id": f"HC{i}", "source": "user_stated", **hc, "buffer_rule": BUFFER_RULES.get(hc.get("type"))}
           for i, hc in enumerate(revised.get("hard_constraints") or [], 1)]
    return {**revised, "hard_constraints": hcs}


def first_legs(legs: dict) -> dict:
    """화면이 돌려준 처음 검사의 이동시간 중 다시 쓸 구간을 고른다."""
    # TMAP 값은 약관상 24시간 넘게 쓸 수 없어 오늘 받은 구간만 쓴다.
    today = date.today().isoformat()
    return {k: v for k, v in legs.items()
            if isinstance(v, dict) and v.get("source") == "api_runtime" and v.get("checked_at") == today}


WEEKDAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def supported_places(facts: dict, policy: dict, today: str) -> list[dict]:
    """판정 코드가 휴무일과 입장을 확정하는 장소를 고른다."""
    # 목록을 적어 두면 데이터와 어긋나서 요일마다 판정 코드로 돌려 본다.
    holidays = set(facts["holidays"]["dates"])
    day, days = date.fromisoformat(today), {}
    while len(days) < 7:
        day += timedelta(days=1)
        if day.isoformat() not in holidays:
            days.setdefault(weekday_of(day.isoformat()), day.isoformat())
    holiday = min((d for d in holidays if d > today), default=None)

    def settled(place: str, visit_date: str) -> bool:
        """쉬는 날로 확정되는 것도 확정이다."""
        out = judge({"date": visit_date, "stops": [{"place": place, "start": "11:00", "dwell_minutes": 30}]},
                    facts, policy)
        status = {c["type"]: c["status"] for c in out["checks"] if c.get("target") == place}
        closed = status.get("CLOSED_DAY")
        return closed == FAIL or (closed == PASS and status.get("ADMISSION_NOT_POSSIBLE") in (PASS, FAIL))

    places = []
    for place in facts["places"]:
        unsettled = [wd for wd in WEEKDAY_ORDER if not settled(place, days[wd])]
        on_holiday = holiday is not None and settled(place, holiday)
        if len(unsettled) < len(WEEKDAY_ORDER) or on_holiday:
            # 되는 요일을 적으면 그날만 연다고 읽혀서 못 하는 요일을 준다.
            places.append({"place": place, "unsettled_weekdays": unsettled, "holiday": on_holiday})
    return places


class CheckIn(BaseModel):
    text: str


class RecheckIn(BaseModel):
    # 추출과 같은 일정 모델로 받아 체류 -60 같은 값은 판정 전에 422 로 거절한다.
    original: Itinerary  # 처음 추출한 일정이다. 꼭 지킨다던 것을 이것과 비교한다
    revised: Itinerary
    legs: dict = {}  # 처음 받은 이동시간이다. 서버는 저장하지 않고 화면이 들고 있다가 돌려준다


def create_app(claude=None, tmap_key: str | None = None, today: str | None = None) -> FastAPI:
    """테스트는 가짜 Claude 를 넣는다. today 는 연도 없는 날짜를 풀 때 기준이다."""
    app = FastAPI(title="itinerary-verifier 데모")
    # DNS 리바인딩으로 남의 페이지가 이 서버의 키를 쓰지 못하게 호스트를 제한한다.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    spent, lock = Spend(), threading.Lock()  # 서버를 켠 뒤 누적 비용이다. 요청은 여러 스레드에서 올 수 있다

    def tally(where: str, meter: Spend) -> None:
        """요청 하나에서 나간 돈을 서버 창에 찍고 누적에 더한다."""
        if not meter.calls():
            return
        with lock:
            spent.merge(meter)
            total = spent.line()
        try:
            print(f"  [비용] {where} — {meter.line()}\n         서버를 켠 뒤 누적 — {total}", flush=True)
        except (OSError, UnicodeError):
            # 서버 창이 UTF-8 이 아니면 못 찍는다. 그래도 돈을 낸 판정은 돌려준다.
            pass

    def judged_legs(itinerary: dict, meter: Spend) -> tuple[dict, dict]:
        """TMAP 으로 받은 구간과 받지 못한 이유를 돌려준다."""
        if not tmap_key:
            return {}, {}
        with counting(meter):
            legs, misses = live_legs(itinerary, tmap_key, FACTS, POLICY)
        reasons = {k: m.reason for k, m in misses.items()}
        # 택시를 못 받은 이유는 예외 객체라 화면으로 보내려고 이유만 꺼낸다.
        for k, leg in legs.items():
            if leg.get("taxi_miss"):
                reasons[f"{k} 택시"] = leg["taxi_miss"].reason
        return {k: {kk: vv for kk, vv in leg.items() if kk != "taxi_miss"} for k, leg in legs.items()}, reasons

    def with_legs(legs: dict) -> dict:
        """받은 구간이 저장해 둔 지하철 하한선 구간을 대신한다."""
        return {**FACTS, "legs": {**FACTS["legs"], **legs}}

    @app.get("/")
    def page():
        return FileResponse(HERE / "web" / "index.html")

    @app.get("/api/places")
    def places() -> dict:
        """화면 위에 알릴 지원 장소다. 돈이 드는 호출은 없다."""
        return {"places": supported_places(FACTS, POLICY, today or date.today().isoformat())}

    @app.post("/api/check")
    def check(body: CheckIn) -> dict:
        """자연어 일정을 구조화 추출하고 판정한다."""
        if claude is None:
            raise HTTPException(503, "Anthropic API 키가 없어 자연어 일정을 추출할 수 없습니다. 키를 넣고 앱을 다시 켜 주세요")
        if not body.text.strip():
            raise HTTPException(422, "일정을 적어 주세요")
        meter = Spend()
        try:
            try:
                ex, usage = extract(body.text, claude, today=today or date.today().isoformat())
            except Refusal as e:
                meter.claude("구조화 추출", e.usage)  # 거부나 형식 오류로 끝나도 청구될 수 있다
                raise HTTPException(422, f"일정을 추출하지 못했습니다 — 모델이 거부했습니다: {e}") from e
            except InvalidOutput as e:
                meter.claude("구조화 추출", e.usage)
                raise HTTPException(422, f"일정을 추출하지 못했습니다 — 모델 출력이 형식에 맞지 않았습니다: {e}") from e
            except anthropic.APIError as e:
                raise HTTPException(502, f"Claude API 호출이 실패했습니다 — {type(e).__name__}") from e
            meter.claude("구조화 추출", usage)
            itinerary = to_itinerary(ex)
            legs, misses = judged_legs(itinerary, meter)
        finally:
            tally("/api/check", meter)
        out = judge(itinerary, with_legs(legs), POLICY)
        return {"itinerary": itinerary, "notes": ex.notes, "legs": legs, "leg_misses": misses,
                "tmap": bool(tmap_key), "result": out, "report": report(out), "questions": questions(out),
                "timeline": timeline(itinerary, out)}

    @app.post("/api/recheck")
    def recheck(body: RecheckIn) -> dict:
        """고친 일정 전체를 다시 판정한다."""
        # 수정 전 판정이 처음과 같도록 원래 일정은 처음 받은 이동시간으로 판정한다.
        original, revised = as_dict(body.original), normalized(as_dict(body.revised))
        meter = Spend()
        try:
            legs, misses = judged_legs(revised, meter)
        finally:
            tally("/api/recheck", meter)
        r = recheck_revision(original, revised, with_legs(first_legs(body.legs)), POLICY,
                             revised_facts=with_legs(legs))
        return {"legs": legs, "leg_misses": misses, "tmap": bool(tmap_key),
                "changed_fields": r["changed_fields"],
                "verdict_before": r["before"]["summary"]["verdict"],
                "verdict_after": r["after"]["summary"]["verdict"],
                "needs_confirmation": r["needs_confirmation"],
                "result": r["after"], "report": report(r["after"]), "questions": questions(r["after"]),
                "timeline": timeline(revised, r["after"])}

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
    uvicorn.run(app, host="127.0.0.1", port=8001)  # 키를 든 서버라 내 PC 에서만 연다


if __name__ == "__main__":
    main()
