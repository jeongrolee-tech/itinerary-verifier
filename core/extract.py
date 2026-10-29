"""자연어 일정을 판정 코어가 읽는 구조로 옮긴다. 없는 값은 채우지 않는다."""

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
# 시각 패턴과 체류 범위는 API 가 강제하지 않아 받자마자 llm.ask 에서 검증한다.
Clock = Annotated[str, StringConstraints(pattern=rf"^{HHMM}$")]
Minutes = Annotated[int, Field(ge=1, le=24 * 60)]


class Stop(BaseModel):
    raw: str = Field(description="이 스톱에 해당하는 입력 원문 조각")
    place: str = Field(description="입력에 적힌 장소명 그대로. 바꾸거나 정식 명칭으로 고치지 않는다")
    start: Clock | None = Field(description="HH:MM (24시간). 입력에 시각이 없으면 null")
    start_source: Source = Field(
        description="explicit=오전·오후 같은 말이 붙은 시각, '17시 30분' 같은 24시간 표기, 앞 시각을 이어받아 정해지는 시각 / inferred='점심 먹고' 나 기댈 앞 시각 없는 '4시' 처럼 추정 / missing=없음"
    )
    # 말하지 않은 체류는 None 이다. 기본값을 채우면 사용자가 말한 값과 구분할 수 없다.
    dwell_minutes: Minutes | None = Field(description="체류시간(분). 입력에 없으면 null. 임의로 채우지 않는다")
    # 동네만 말했으면 area 로 둔다. 가게를 임의로 고르면 다른 곳의 운영시간으로 판정한다.
    scope: Literal["place", "area"] = Field(
        description="place=궁·시장·건물처럼 위치가 하나로 정해지는 곳. 그 안의 어느 가게인지 정해지지 않아도 place / area='명동', '북촌' 처럼 여러 골목에 걸친 동네만 지목한 경우"
    )
    scope_note: str | None = Field(description="area 인 경우 무엇이 특정되지 않았는지. 아니면 null")
    # 밖에서만 머무는 일정을 휴무일 위반으로 판정하지 않으려고 둔다.
    outside_only: bool = Field(
        description="사용자가 그 장소에 들어가지 않고 앞이나 주변에서만 머문다고 했으면 true. 말하지 않았으면 false"
    )

    # 시각과 출처가 어긋나면 사용자가 말한 값인지 판정 코어가 잘못 읽는다.
    @model_validator(mode="after")
    def _start_matches_source(self) -> Stop:
        if (self.start is None) != (self.start_source == "missing"):
            raise ValueError(f"start 가 {self.start!r} 인데 start_source 가 {self.start_source} 다")
        return self


class HardConstraint(BaseModel):
    raw: str = Field(description="해당 입력 원문 조각")
    type: Literal["TRAIN_DEPARTURE", "FLIGHT_DEPARTURE", "ARRIVE_BY", "OTHER"]
    place: str
    # 시각이 없어도 조건은 남긴다. 형식에서 막으면 꼭 타야 할 열차가 결과에서 빠진다.
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


# 판정 코어에 넣는 일정
# 추출 결과와 화면에서 고친 일정을 같은 모델로 검증한다. 체류 -60 이나 "60" 은 막는다.

StrictMinutes = Annotated[int, Field(ge=1, le=24 * 60, strict=True)]


class ItineraryStop(BaseModel):
    place: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    start: Clock | None = None
    start_source: Source
    dwell_minutes: StrictMinutes | None = None
    scope: Literal["place", "area"] = "place"
    scope_note: str | None = None
    outside_only: bool = False

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
    avoid_taxi: bool = False

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
    """빈 칸은 키를 뺀다. 처음 일정과 고친 일정의 모양이 같아야 바뀐 칸을 센다."""
    d = it.model_dump(mode="json")
    d["stops"] = [{k: v for k, v in s.items() if v is not None} for s in d["stops"]]
    d["hard_constraints"] = [{k: v for k, v in h.items() if v is not None} for h in d["hard_constraints"]]
    return d


# LLM 은 규칙 적용이 흔들려 옮겨 적기만 시키고 판정은 코드가 한다.
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
    """today 는 본문에 넣는다. 시스템 프롬프트가 고정돼야 프롬프트 캐시가 붙는다."""
    # 옮겨 적기라 effort 를 낮춘다. 사고 토큰도 출력 값으로 청구된다.
    return {"model": model, "effort": effort, "system": SYSTEM,
            "content": f"오늘은 {today} 이다.\n\n{text}", "schema": Extraction}


def extract(text: str, client: anthropic.Anthropic | None = None,
            model: str = DEFAULT_MODEL, *, today: str,
            effort: str | None = "low") -> tuple[Extraction, dict]:
    """모델이 거부하면 llm.Refusal 이 난다."""
    return ask(client or anthropic.Anthropic(), **request(text, model, today=today, effort=effort))


# flight_boarding 은 기준을 확보하지 못해 policy 에 없다. 그래서 pass 가 나오지 않는다.
BUFFER_RULES = {"TRAIN_DEPARTURE": "rail_boarding", "FLIGHT_DEPARTURE": "flight_boarding",
                "ARRIVE_BY": "transit_leg", "OTHER": "transit_leg"}


def to_itinerary(ex: Extraction) -> dict:
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
