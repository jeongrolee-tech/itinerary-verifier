"""
추출 단계 — 자연어 일정을 판정 코어가 읽는 구조로 옮긴다.

여기서 하는 일은 **옮겨 적는 것**이지 채우는 것이 아니다.
입력에 없는 값은 만들지 않고 null 로 둔다. 기본값을 채우는 것은 판정 코어의 정책이고,
여기서 채우면 사용자가 말한 값과 구분이 안 된다.

    from extract import extract
    result, usage = extract("10월 8일 목요일 오후 1시에 경복궁 두 시간...", today="2026-09-22")
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

import anthropic
from pydantic import BaseModel, Field, StringConstraints, model_validator

from llm import ask
from model_info import DEFAULT_MODEL
from verdict import HHMM

Source = Literal["explicit", "inferred", "missing"]
Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
# ⭐ 형식이 맞아야 판정 코어와 길찾기가 읽는다. "1:00 PM" 이나 "2026-02-30" 을 그대로 넘기면 계산하다
#    멈추거나 엉뚱한 시각으로 판정한다 (2차 피드백 4번). 날짜 형식과 요일 목록은 스키마에 넣어 API 가
#    지키게 하고, 시각 패턴과 체류 범위는 API 가 설명으로만 받으므로 받은 뒤에 한 번 더 검증한다(llm.ask).
Clock = Annotated[str, StringConstraints(pattern=rf"^{HHMM}$")]
Minutes = Annotated[int, Field(ge=1, le=24 * 60)]


class Stop(BaseModel):
    raw: str = Field(description="이 스톱에 해당하는 입력 원문 조각")
    place: str = Field(description="입력에 적힌 장소명 그대로. 바꾸거나 정식 명칭으로 고치지 않는다")
    start: Clock | None = Field(description="HH:MM (24시간). 입력에 시각이 없으면 null")
    start_source: Source = Field(
        description="explicit=시각이 적혀 있음 / inferred='점심 먹고' 처럼 문맥에서 추정 / missing=없음"
    )
    # ⭐ int 가 아니라 int | None 인 것이 핵심이다.
    #    "광장시장에서 저녁" 에 60분을 넣어버리면, 나중에 그 60분이 사용자가
    #    말한 값인지 우리가 채운 값인지 구분할 수 없다. 없으면 null 로 둔다.
    #    기본값을 채우는 일은 판정 단계(verdict.py)의 정책이다.
    dwell_minutes: Minutes | None = Field(description="체류시간(분). 입력에 없으면 null. 임의로 채우지 않는다")
    # ⭐ "장소를 찾은 것" 과 "그 활동을 검증한 것" 은 다르다.
    #    "명동에서 쇼핑" 을 임의로 특정 상점에 연결하면, 그 상점의 운영시간으로
    #    판정해놓고 사용자가 갈 곳은 다른 데가 된다. 구역은 area 로 남긴다.
    scope: Literal["place", "area"] = Field(
        description="place=특정 시설이나 점포 / area='명동', '북촌' 처럼 구역만 지목한 경우"
    )
    scope_note: str | None = Field(description="area 인 경우 무엇이 특정되지 않았는지. 아니면 null")

    # 값과 출처가 맞아야 한다. 시각이 있는데 출처가 missing 이면 판정 코어가 그 시각을 사용자가 말한
    # 값처럼 쓰고, 없는데 explicit 이면 무엇을 사용자가 말했는지 기록이 틀린다
    @model_validator(mode="after")
    def _start_matches_source(self) -> Stop:
        if (self.start is None) != (self.start_source == "missing"):
            raise ValueError(f"start 가 {self.start!r} 인데 start_source 가 {self.start_source} 다")
        return self


class HardConstraint(BaseModel):
    raw: str = Field(description="해당 입력 원문 조각")
    type: Literal["TRAIN_DEPARTURE", "FLIGHT_DEPARTURE", "ARRIVE_BY", "OTHER"]
    place: str
    time: Clock = Field(description="HH:MM (24시간)")


class Extraction(BaseModel):
    date: dt.date | None = Field(description="YYYY-MM-DD. 날짜 자체가 없으면 null")
    date_source: Source
    weekday_stated: Weekday | None = Field(
        description="사용자가 말한 요일을 그대로. '목요일' → THU. 안 말했으면 null. 맞는지는 판정 단계에서 확인한다"
    )
    stops: list[Stop]
    hard_constraints: list[HardConstraint] = Field(
        description="'꼭 타야 해요' 처럼 사용자가 반드시 지켜야 한다고 말한 조건만. 일반 스톱은 넣지 않는다"
    )
    notes: list[str] = Field(description="옮기면서 애매했던 점. 없으면 빈 배열")

    @model_validator(mode="after")
    def _date_matches_source(self) -> Extraction:
        if (self.date is None) != (self.date_source == "missing"):
            raise ValueError(f"date 가 {self.date} 인데 date_source 가 {self.date_source} 다")
        return self


# ⭐ LLM 에게 판정을 시키지 않는다. 옮겨 적는 일만 시킨다.
#    LLM 은 자연어를 구조로 바꾸는 데 강하지만, 규칙을 일관되게 적용하는 데는
#    약하다. 비교 실험에서 같은 정책 문서를 주고도 규칙 하나를 건너뛰었다.
#    그래서 경계를 이렇게 둔다 — 추출은 LLM, 근거 충분성 판단과 판정은 코드.
SYSTEM = """너는 여행 일정 텍스트를 구조화하는 추출기다. 판정은 하지 않는다.

지켜야 할 것:

1. 입력에 없는 값을 만들지 않는다. 시각이나 체류시간이 안 적혀 있으면 null 로 둔다.
   "한 시간 정도"는 60분으로 적어도 되지만, 아무 말도 없으면 null 이다.
   장소 유형으로 체류시간을 추정하지 않는다. 그건 판정 단계의 정책이다.

2. 시각을 문맥에서 추정했으면 start_source 를 inferred 로 적는다.
   "점심 먹고" → 12:00 은 추정이다. "오후 1시" 는 explicit 이다.

3. 장소명은 입력에 적힌 그대로 옮긴다. 정식 명칭으로 고치거나 지점을 붙이지 않는다.

4. "명동에서 쇼핑", "북촌에서 점심" 처럼 구역만 지목한 입력은 scope 를 area 로 적고,
   임의로 특정 점포를 고르지 않는다. 무엇이 특정되지 않았는지 scope_note 에 적는다.

5. hard_constraints 에는 사용자가 "꼭", "반드시" 같은 말로 지킨다고 한 조건만 넣는다.
   그냥 순서상 마지막인 일정은 필수 조건이 아니다.
   **필수 조건으로 뽑은 장소는 stops 에 중복해서 넣지 않는다.**
   예: "20:30 서울역에서 KTX를 탑니다. 이 기차는 꼭 타야 해요"
       → 서울역은 hard_constraints 에만. stops 에는 그 앞 일정까지만 넣는다.
   역·공항처럼 관람 대상이 아니라 교통편을 타러 가는 곳은 스톱이 아니다.

6. 연도가 안 적혀 있으면 오늘 날짜를 기준으로 **가장 가까운 미래의 해당 날짜**로 적고,
   date_source 를 inferred 로 표시한다. 오늘 날짜는 입력 앞에 준다.
   날짜 자체가 없으면 그때는 null 이다.

7. 사용자가 요일을 말했으면 weekday_stated 에 그대로 옮긴다 (목요일 → THU).
   날짜와 요일이 맞는지는 네가 확인하지 않는다. 판정 단계에서 교차검증한다.

형식이 맞는 것과 의미가 맞는 것은 다르다. 확실하지 않으면 null 과 notes 를 쓴다."""


def extract(text: str, client: anthropic.Anthropic | None = None,
            model: str = DEFAULT_MODEL, *, today: str,
            effort: str | None = "low") -> tuple[Extraction, dict]:
    """자연어 → Extraction. (결과, 사용량) 을 돌려준다. 모델이 거부하면 llm.Refusal 이 난다.

    today 는 연도가 빠진 날짜를 푸는 기준이다. 시스템 프롬프트가 아니라
    사용자 메시지에 넣는다 — 시스템 프롬프트를 고정해야 캐시가 붙는다.
    시스템 시계는 여기서 읽지 않고 부르는 쪽이 넣는다. 평가는 tests.json 의 기준 날짜를,
    실제로 쓸 때(verify.py)는 그날을 넣는다.
    """
    # 추출은 적힌 값을 옮기는 기계적인 작업이다. 깊게 생각할 필요가 없고,
    # 사고 토큰도 출력으로 과금되므로 effort 를 낮춘다.
    return ask(client or anthropic.Anthropic(), model=model, effort=effort, system=SYSTEM,
               content=f"오늘은 {today} 이다.\n\n{text}", schema=Extraction)


# 필수 조건마다 권장 도착 여유 기준(policy.json buffer_minutes). flight_boarding 은 정책에 일부러 두지 않았다 —
# 탑승 수속 기준을 확보하지 않아서다. 기준이 없으면 판정 코어가 pass 를 내지 않는다(NO_BUFFER_RULE).
BUFFER_RULES = {"TRAIN_DEPARTURE": "rail_boarding", "FLIGHT_DEPARTURE": "flight_boarding",
                "ARRIVE_BY": "transit_leg", "OTHER": "transit_leg"}


def to_itinerary(ex: Extraction) -> dict:
    """판정 코어가 읽는 모양으로 옮긴다. 값을 채우지 않는다."""
    return {
        "date": ex.date.isoformat() if ex.date else None,
        "date_source": ex.date_source,
        "weekday_stated": ex.weekday_stated,
        "stops": [
            {k: v for k, v in {
                "place": s.place,
                "start": s.start,
                "start_source": s.start_source,
                "dwell_minutes": s.dwell_minutes,
                "scope": s.scope,
                "scope_note": s.scope_note,
            }.items() if v is not None}
            for s in ex.stops
        ],
        "hard_constraints": [
            {"id": f"HC{i + 1}", "type": hc.type, "place": hc.place, "time": hc.time,
             "buffer_rule": BUFFER_RULES[hc.type],
             "source": "user_stated", "raw": hc.raw}
            for i, hc in enumerate(ex.hard_constraints)
        ],
    }
