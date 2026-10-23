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
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

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
        description="explicit=오전·오후 같은 말이 붙은 시각, '17시 30분' 같은 24시간 표기, 앞 시각을 이어받아 정해지는 시각 / inferred='점심 먹고' 나 기댈 앞 시각 없는 '4시' 처럼 추정 / missing=없음"
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
        description="place=궁·시장·건물처럼 위치가 하나로 정해지는 곳. 그 안의 어느 가게인지 정해지지 않아도 place / area='명동', '북촌' 처럼 여러 골목에 걸친 동네만 지목한 경우"
    )
    scope_note: str | None = Field(description="area 인 경우 무엇이 특정되지 않았는지. 아니면 null")
    # ⭐ 장소에 들어가지 않고 밖에서만 머무는 일정이 있다 — 문 닫은 걸 알고 경복궁 앞에서 쉬다 가기. 이 칸이 없으면
    #    휴무일을 위반으로 판정해 갈 수 있는 일정을 불가능이라고 한다(2026-10-23 사용자 결정). 택시 안 탐처럼
    #    사용자가 말한 조건이다
    outside_only: bool = Field(
        description="사용자가 그 장소에 들어가지 않고 앞이나 주변에서만 머문다고 했으면 true. 말하지 않았으면 false"
    )

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
    # ⭐ 시각을 몰라도 조건은 남긴다(3차 피드백). "서울역에서 KTX 를 꼭 타야 해요" 에 시각이 없다고 형식에서 막으면
    #    추출이 통째로 실패하거나 KTX 가 결과에서 사라진다. 시각은 null 로 두고 판정 단계가 묻는다.
    #    문맥에서 짐작한 시각은 장소 시각처럼 inferred 로 남긴다 — 판정 단계가 그 시각으로 확정하지 않는다
    time: Clock | None = Field(description="HH:MM (24시간). 입력에 시각이 없으면 null")
    time_source: Source = Field(description="explicit=입력에 적힌 시각 / inferred=문맥에서 추정 / missing=없음")

    @model_validator(mode="after")
    def _time_matches_source(self) -> HardConstraint:
        if (self.time is None) != (self.time_source == "missing"):
            raise ValueError(f"time 이 {self.time!r} 인데 time_source 가 {self.time_source} 다")
        return self


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
    avoid_taxi: bool = Field(description="사용자가 택시를 타지 않는다고 말했으면 true. 말하지 않았으면 false")
    notes: list[str] = Field(description="옮기면서 애매했던 점. 없으면 빈 배열")

    @model_validator(mode="after")
    def _date_matches_source(self) -> Extraction:
        if (self.date is None) != (self.date_source == "missing"):
            raise ValueError(f"date 가 {self.date} 인데 date_source 가 {self.date_source} 다")
        return self


# ── 판정 코어에 넣는 일정 ───────────────────────────────────────────
# ⭐ 추출 결과(to_itinerary)와 사용자가 화면 표에서 고친 일정(/api/recheck)은 같은 모델을 지난다(3차 피드백).
#    전에는 재검사 API 가 일정을 dict 로 받아 체류 -60 이 15:30 종료로 계산되어 '가능' 이 나왔고, 문자열 "60" 은
#    서버 오류 500 이었다. 체류를 모르는 것(null)은 잘못이 아니라 추가 질문과 보류다. 음수 · 0 · 하루를 넘는 값과
#    숫자가 아닌 값은 잘못된 요청이다. 숫자 문자열("60")을 숫자로 바꿔 받아 주지도 않는다(strict)

StrictMinutes = Annotated[int, Field(ge=1, le=24 * 60, strict=True)]


class ItineraryStop(BaseModel):
    place: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    start: Clock | None = None
    start_source: Source
    dwell_minutes: StrictMinutes | None = None
    scope: Literal["place", "area"] = "place"
    scope_note: str | None = None
    outside_only: bool = False  # 들어가지 않고 밖에서만 머문다고 했나

    @model_validator(mode="after")
    def _start_matches_source(self) -> ItineraryStop:
        if (self.start is None) != (self.start_source == "missing"):
            raise ValueError(f"start 가 {self.start!r} 인데 start_source 가 {self.start_source} 다")
        return self


class ItineraryConstraint(BaseModel):
    id: str | None = None
    type: Literal["TRAIN_DEPARTURE", "FLIGHT_DEPARTURE", "ARRIVE_BY", "OTHER"]
    place: str | None = None
    time: Clock | None = None
    time_source: Source
    raw: str | None = None
    source: Literal["user_stated"] = "user_stated"
    buffer_rule: str | None = None

    @model_validator(mode="after")
    def _time_matches_source(self) -> ItineraryConstraint:
        if (self.time is None) != (self.time_source == "missing"):
            raise ValueError(f"time 이 {self.time!r} 인데 time_source 가 {self.time_source} 다")
        return self


class Itinerary(BaseModel):
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    date_source: Source
    weekday_stated: Weekday | None = None
    stops: list[ItineraryStop]
    hard_constraints: list[ItineraryConstraint] = []
    avoid_taxi: bool = False  # 사용자가 택시를 안 탄다고 했나. 말하지 않았으면 택시도 되는 수단으로 본다

    @field_validator("date")
    @classmethod
    def _real_date(cls, value: str | None) -> str | None:
        if value is not None:
            dt.date.fromisoformat(value)  # 2026-02-30 같은 없는 날짜를 막는다
        return value

    @model_validator(mode="after")
    def _date_matches_source(self) -> Itinerary:
        if (self.date is None) != (self.date_source == "missing"):
            raise ValueError(f"date 가 {self.date} 인데 date_source 가 {self.date_source} 다")
        return self


def as_dict(it: Itinerary) -> dict:
    """판정 코어가 읽는 dict. 장소 · 필수 일정에서 값이 없는 칸은 키를 뺀다 — 처음 일정과 고친 일정의 모양이 같아야
    바뀐 칸을 바르게 센다(recheck.recheck_revision)."""
    d = it.model_dump(mode="json")
    d["stops"] = [{k: v for k, v in s.items() if v is not None} for s in d["stops"]]
    d["hard_constraints"] = [{k: v for k, v in h.items() if v is not None} for h in d["hard_constraints"]]
    return d


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
   "점심 먹고" → 12:00 은 추정이다. "오후 1시", "저녁 6시 반", "17시 30분" 은 explicit 이다.
   오전·오후 같은 말이 붙지 않은 시각은 같은 일정에서 앞에 나온 시각 뒤로 처음 오는 시각으로 읽고 explicit 으로 적는다.
   "오전 11시 박물관, 2시 공원" 의 2시는 14:00 이다. 앞에 기댈 시각이 없을 때만 일정에 가장 그럴듯한 시각으로 적고
   inferred 로 표시한다. "4시에 공원" → 16:00, inferred.

3. 장소명은 입력에 적힌 그대로 옮긴다. 정식 명칭으로 고치거나 지점을 붙이지 않는다.

4. "명동에서 쇼핑", "북촌에서 점심" 처럼 구역만 지목한 입력은 scope 를 area 로 적고,
   임의로 특정 점포를 고르지 않는다. 무엇이 특정되지 않았는지 scope_note 에 적는다.
   구역은 여러 골목에 걸친 동네다. 궁·시장·건물처럼 위치가 하나로 정해지는 곳은 그 안의 어느 가게인지
   정해지지 않아도 place 다. "남대문시장에서 점심" 은 place 다 — 어느 가게인지는 판정 단계가 묻는다.

5. hard_constraints 에는 사용자가 "꼭", "반드시" 같은 말로 지킨다고 한 조건만 넣는다.
   그냥 순서상 마지막인 일정은 필수 조건이 아니다.
   **필수 조건으로 뽑은 장소는 stops 에 중복해서 넣지 않는다.**
   예: "20:30 서울역에서 KTX를 탑니다. 이 기차는 꼭 타야 해요"
       → 서울역은 hard_constraints 에만. stops 에는 그 앞 일정까지만 넣는다.
   역·공항처럼 관람 대상이 아니라 교통편을 타러 가는 곳은 스톱이 아니다.
   필수 조건의 시각이 안 적혀 있어도 조건은 넣는다. time 은 null, time_source 는 missing 이다.
   시각을 문맥에서 추정했으면 time_source 를 inferred 로 적는다(2번과 같다).

6. 연도가 안 적혀 있으면 오늘 날짜를 기준으로 **가장 가까운 미래의 해당 날짜**로 적고,
   date_source 를 inferred 로 표시한다. 오늘 날짜는 입력 앞에 준다.
   날짜 자체가 없으면 그때는 null 이다.

7. 사용자가 요일을 말했으면 weekday_stated 에 그대로 옮긴다 (목요일 → THU).
   날짜와 요일이 맞는지는 네가 확인하지 않는다. 판정 단계에서 교차검증한다.

8. 사용자가 택시를 타지 않는다고 말했으면 avoid_taxi 를 true 로 적는다.
   "택시는 안 타요", "대중교통으로만 다녀요" 가 그렇다. 말하지 않았으면 false 다.

9. 사용자가 어떤 장소에 들어가지 않고 그 앞이나 주변에서만 머문다고 했으면 그 장소의 outside_only 를 true 로 적는다.
   "문 닫은 거 알아요, 경복궁 앞에서 쉬다 갈게요", "궁 담장 밖만 걸을게요" 가 그렇다. 이때 장소명은 그 장소 이름으로
   적는다 — "경복궁 앞" 은 경복궁이다. 말하지 않았으면 false 다.

형식이 맞는 것과 의미가 맞는 것은 다르다. 확실하지 않으면 null 과 notes 를 쓴다."""


def request(text: str, model: str = DEFAULT_MODEL, *, today: str, effort: str | None = "low") -> dict:
    """추출 한 번에 llm.ask 가 받는 인자. 평가(run_extract.py)는 이것으로 요청 지문을 만들고 그대로 부른다.

    today 는 연도가 빠진 날짜를 푸는 기준이다. 시스템 프롬프트가 아니라
    사용자 메시지에 넣는다 — 시스템 프롬프트를 고정해야 캐시가 붙는다.
    시스템 시계는 여기서 읽지 않고 부르는 쪽이 넣는다. 평가는 tests.json 의 기준 날짜를,
    실제로 쓸 때(verify.py)는 그날을 넣는다.
    """
    # 추출은 적힌 값을 옮기는 기계적인 작업이다. 깊게 생각할 필요가 없고,
    # 사고 토큰도 출력으로 과금되므로 effort 를 낮춘다.
    return {"model": model, "effort": effort, "system": SYSTEM,
            "content": f"오늘은 {today} 이다.\n\n{text}", "schema": Extraction}


def extract(text: str, client: anthropic.Anthropic | None = None,
            model: str = DEFAULT_MODEL, *, today: str,
            effort: str | None = "low") -> tuple[Extraction, dict]:
    """자연어 → Extraction. (결과, 사용량) 을 돌려준다. 모델이 거부하면 llm.Refusal 이 난다."""
    return ask(client or anthropic.Anthropic(), **request(text, model, today=today, effort=effort))


# 필수 조건마다 권장 도착 여유 기준(policy.json buffer_minutes). flight_boarding 은 정책에 일부러 두지 않았다 —
# 탑승 수속 기준을 확보하지 않아서다. 기준이 없으면 판정 코어가 pass 를 내지 않는다(NO_BUFFER_RULE).
BUFFER_RULES = {"TRAIN_DEPARTURE": "rail_boarding", "FLIGHT_DEPARTURE": "flight_boarding",
                "ARRIVE_BY": "transit_leg", "OTHER": "transit_leg"}


def to_itinerary(ex: Extraction) -> dict:
    """판정 코어가 읽는 모양으로 옮긴다. 값을 채우지 않는다. 사용자가 고친 일정과 같은 일정 모델(Itinerary)을 지난다."""
    return as_dict(Itinerary.model_validate({
        "date": ex.date.isoformat() if ex.date else None,
        "date_source": ex.date_source,
        "weekday_stated": ex.weekday_stated,
        "avoid_taxi": ex.avoid_taxi,
        "stops": [
            {k: v for k, v in {
                "place": s.place,
                "start": s.start,
                "start_source": s.start_source,
                "dwell_minutes": s.dwell_minutes,
                "scope": s.scope,
                "scope_note": s.scope_note,
                "outside_only": s.outside_only,
            }.items() if v is not None}
            for s in ex.stops
        ],
        "hard_constraints": [
            {"id": f"HC{i + 1}", "type": hc.type, "place": hc.place, "time": hc.time, "time_source": hc.time_source,
             "buffer_rule": BUFFER_RULES[hc.type],
             "source": "user_stated", "raw": hc.raw}
            for i, hc in enumerate(ex.hard_constraints)
        ],
    }))
