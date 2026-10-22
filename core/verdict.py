"""
판정 코어 — 순수 함수. 외부 API를 호출하지 않는다.

입력: 일정(itinerary) + 사실(facts) + 정책(policy)  — 전부 인자로 받는다
출력: 검사별 상태 + 필수 조건 평가 + 전체 판정

이 파일에 네트워크 호출이 들어가는 순간 고정 입력으로 테스트할 수 없게 된다.

읽는 순서 — 맨 아래 judge() 가 전체 흐름이다.
  입력 확인 → 날짜 · 요일 → 장소 준비 → 장소 검사(휴무 · 입장 · 체류) → 구간 검사(이동)
  → 필수 조건(열차 등) → 전체 판정
위에서부터 그 단계에 쓰는 작은 함수들이 차례로 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date, timedelta
from typing import Any

# ⭐ 이 프로젝트의 출발점. 검사 결과를 true/false 두 개로 두지 않는다.
#    "오류를 못 찾았다" 와 "문제가 없다" 는 다르기 때문이다.
#      pass   근거를 확보했고 조건을 만족했다
#      fail   확보한 근거에서 위반이 확인됐다
#      unknown        검사가 필요한데 끝내지 못했다   ← 이게 없으면 서비스가 거짓말을 한다
#      not_applicable 검사 자체가 성립하지 않는다     ← 불필요한 것과 못 끝낸 것을 섞지 않는다
PASS, FAIL, UNKNOWN, NA = "pass", "fail", "unknown", "not_applicable"

# date.weekday() 는 월요일이 0
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
# 판정 코어가 읽는 시각 형식. HH:MM 24시간 — 추출(extract.py)도 이 형식으로만 내보낸다
HHMM = r"([01]\d|2[0-3]):[0-5]\d"


# ── 시간 유틸 (전부 '분' 단위 정수로 다룬다) ──────────────────────────
def to_min(hhmm: str) -> int:
    """"14:30" → 870. 시각 계산은 전부 분 단위로 한다."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: float) -> str:
    """870 → "14:30". 분 아래는 버린다."""
    m = int(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def weekday_of(iso: str) -> str:
    """"2026-10-08" → "THU"."""
    return WEEKDAYS[Date.fromisoformat(iso).weekday()]


def shift_date(iso: str, days: int) -> str:
    """날짜를 days 만큼 옮긴다. 휴무일이 공휴일과 겹쳐 밀릴 때 쓴다."""
    return (Date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def evidence_usable_on(ev: dict | None, visit_date: str) -> bool:
    """오늘 조회했다는 사실만으로 다음 달 방문일에 쓸 수 있는 정보가 되지는 않는다."""
    # ⭐ 근거에는 '언제 조회했나' 만이 아니라 '언제까지 쓸 수 있나' 가 필요하다.
    #    2026-09-21 에 확인한 운영시간으로 2027-03-01 방문일을 판정할 수는 없다.
    #    Google Places 응답에는 이 개념이 아예 없다 (docs/data-policy.md 참조).
    if not ev:
        return False
    if ev.get("valid_from") and visit_date < ev["valid_from"]:
        return False
    if ev.get("valid_until") and visit_date > ev["valid_until"]:
        return False
    return True


def check(type_: str, target: str, status: str, **extra: Any) -> dict:
    """검사 결과 하나. severity(심각도)는 fail 일 때만 붙인다."""
    return {
        "type": type_,
        "target": target,
        "status": status,
        "severity": extra.pop("severity", "blocking") if status == FAIL else None,
        **extra,
    }


# ── 입력 확인 ───────────────────────────────────────────────────────
def input_checks(itinerary: dict) -> list[dict]:
    """
    판정을 시작하기 전에 반드시 있어야 하는 입력을 확인한다.

    추출 단계는 날짜나 시작 시각을 None 으로 남길 수 있다. 그 값을 그대로
    시간 계산에 넘기면 예외로 죽는다. 입력이 부족한 것도 검증 결과의
    일부이므로 오류를 내지 않고 unknown 검사로 돌려준다. "1:00 PM" 처럼
    읽을 수 없는 시각도 같다 — tests.json 이나 사용자가 고친 일정(recheck)은 추출의
    형식 검증을 거치지 않고 들어온다.
    """
    return (_date_input(itinerary.get("date"))
            + _stops_input(itinerary.get("stops") or [])
            + _hard_constraints_input(itinerary.get("hard_constraints") or []))


def _date_input(visit_date) -> list[dict]:
    """방문 날짜가 있고 YYYY-MM-DD 로 읽히나."""
    if not visit_date:
        return [check(
            "INPUT_REQUIRED", "일정 날짜", UNKNOWN,
            unknown_reason="MISSING_DATE",
            detail="방문 날짜가 없어 운영시간과 휴무일을 확인할 수 없다",
            how_to_resolve={"user": "방문할 날짜를 알려주세요"},
        )]
    try:
        Date.fromisoformat(visit_date)
    except (TypeError, ValueError):
        return [check(
            "INPUT_REQUIRED", "일정 날짜", UNKNOWN,
            unknown_reason="INVALID_DATE",
            detail=f"방문 날짜({visit_date})를 YYYY-MM-DD 형식으로 해석할 수 없다",
            how_to_resolve={"user": "방문 날짜를 YYYY-MM-DD 형식으로 알려주세요"},
        )]
    return []


def _stops_input(stops: list[dict]) -> list[dict]:
    """장소가 하나 이상 있고, 장소마다 이름과 HH:MM 시작 시각이 있나."""
    missing: list[dict] = []
    if not stops:
        missing.append(check(
            "INPUT_REQUIRED", "일정 장소", UNKNOWN,
            unknown_reason="MISSING_STOPS",
            detail="방문할 장소가 없어 일정의 실행 가능성을 확인할 수 없다",
            how_to_resolve={"user": "방문할 장소와 순서를 알려주세요"},
        ))

    for i, stop in enumerate(stops):
        name = stop.get("place") or f"stops[{i}]"
        if not stop.get("place"):
            missing.append(check(
                "INPUT_REQUIRED", name, UNKNOWN,
                unknown_reason="MISSING_PLACE",
                detail="방문 장소가 없다",
                how_to_resolve={"user": "방문할 장소를 알려주세요"},
            ))
        if not stop.get("start"):
            missing.append(check(
                "INPUT_REQUIRED", name, UNKNOWN,
                unknown_reason="MISSING_START_TIME",
                detail="방문 시작 시각이 없어 입장과 이동시간을 계산할 수 없다",
                how_to_resolve={"user": f"{name}에 몇 시에 도착할 예정인가요?"},
            ))
        elif not re.fullmatch(HHMM, str(stop["start"])):
            missing.append(check(
                "INPUT_REQUIRED", name, UNKNOWN,
                unknown_reason="INVALID_TIME",
                detail=f"방문 시작 시각({stop['start']})을 HH:MM(24시간)으로 해석할 수 없다",
                how_to_resolve={"user": f"{name}에 몇 시에 도착할 예정인가요?"},
            ))
    return missing


def _hard_constraints_input(constraints: list[dict]) -> list[dict]:
    """필수 조건마다 장소와 HH:MM 시각이 있나."""
    missing: list[dict] = []
    for i, constraint in enumerate(constraints, 1):
        if not constraint.get("place") or not constraint.get("time"):
            missing.append(check(
                "INPUT_REQUIRED", f"필수 일정 HC{i}", UNKNOWN,
                unknown_reason="INCOMPLETE_HARD_CONSTRAINT",
                detail="필수 일정에 장소 또는 시각이 없다",
                how_to_resolve={"user": "필수 일정의 장소와 시각을 알려주세요"},
            ))
        elif not re.fullmatch(HHMM, str(constraint["time"])):
            missing.append(check(
                "INPUT_REQUIRED", f"필수 일정 HC{i}", UNKNOWN,
                unknown_reason="INVALID_TIME",
                detail=f"필수 일정 시각({constraint['time']})을 HH:MM(24시간)으로 해석할 수 없다",
                how_to_resolve={"user": "필수 일정의 시각을 다시 알려주세요"},
            ))
    return missing


# ── 지원 범위 ───────────────────────────────────────────────────────
def support_scope(stop: dict, facts: dict) -> str:
    """장소가 지원 범위의 어디에 드는지 — supported(정보가 있는 장소) · unsupported(지원 목록 밖) · area(구역).

    구역인지는 추출(LLM)이 문장을 읽고 정하고, 목록에 있는지는 코드가 이름으로 비교한다(README 'LLM과 코드의 경계').
    facts 가 구역으로 아는 곳(북촌한옥마을)도 구역이다. 추출 채점(run_extract.py)이 같은 함수로 분류가 맞았는지 본다
    (2차 피드백 3번 '지원 범위 분류까지 채점').
    """
    known = facts["places"].get(stop.get("place"))
    if stop.get("scope") == "area" or (known or {}).get("scope") == "area":
        return "area"
    return "supported" if known else "unsupported"


def place_not_checked(type_: str, name: str, support: str) -> dict:
    """확인할 대상이 없는 장소의 검사. 구역이면 어디로 가는지 묻고, 지원 목록 밖이면 확인하지 않았다고 알린다."""
    # ⭐ 둘 다 unknown 이지만 푸는 쪽이 다르다. 구역은 사용자가 갈 곳을 알려 주면 풀리고, 지원 목록 밖 장소는
    #    우리가 데이터를 확보해야 풀린다. 국립중앙박물관에 "어디로 가시나요?" 를 물으면 답할 수 없는 질문이 된다.
    if support == "area":
        return check(type_, name, UNKNOWN, unknown_reason="PLACE_NOT_RESOLVED",
                     detail="동네만 정해져 있어 방문할 시설이나 점포를 특정하지 못했다",
                     how_to_resolve={"user": f"{name}에서 정확히 어디로 가시나요?"})
    return check(type_, name, UNKNOWN, unknown_reason="UNSUPPORTED_PLACE",
                 detail="지원하는 장소 목록에 없는 곳이라 휴무일과 운영시간을 확인하지 않았다",
                 how_to_resolve={"system": f"{name}의 휴무일과 운영시간을 공식 출처에서 확보"})


# ── 휴무일 ──────────────────────────────────────────────────────────
def closed_date_for(visit_date: str, cd: dict, holidays: dict) -> str | None:
    """
    기관마다 규칙이 다르다. 코드에 박지 않고 facts 의 exception_rule 로 분기한다.
      shift_to_next_nonholiday : 정기휴일이 공휴일이면 개방하고, 다음 첫 비공휴일이 휴일이 된다 (궁궐)
      open_if_holiday_no_shift : 정기휴일이 공휴일이면 개방하고, 밀지 않는다 (미술관)
      없음 (None · "none")       : 공휴일이어도 정기휴일에 쉰다. 밀지도 않는다
    """
    # ⭐ "월요일이면 휴관" 같은 규칙을 코드에 박으면 안 된다.
    #    2026-10-06 은 궁궐은 휴궁이고 미술관은 개관이다. 규칙이 기관마다 다르다.
    #    그래서 규칙 이름을 facts 에 두고 코드는 그것을 읽어 분기한다.
    #    골든 테스트 T05 와 T06 이 같은 날짜로 정답이 반대인 이유가 이것이다.
    target = WEEKDAYS.index(cd["weekly"][0])
    cur = Date.fromisoformat(visit_date).weekday()
    base = shift_date(visit_date, -((cur - target) % 7))  # 가장 가까운 과거(또는 당일) 정기휴일

    if base not in holidays["dates"] or cd.get("exception_rule") in (None, "none"):
        return base
    if cd.get("exception_rule") == "open_if_holiday_no_shift":
        return None  # 그 주에는 휴일이 없다

    d = shift_date(base, 1)
    for _ in range(14):
        if d not in holidays["dates"]:
            break
        d = shift_date(d, 1)
    return d


def check_closed_day(name: str, place: dict | None, visit_date: str, facts: dict, support: str) -> dict:
    """방문일이 휴무일인가. 근거를 쓸 수 있는지 먼저 보고, 고정 휴관일과 정기휴일을 차례로 본다."""
    if place is None:
        return place_not_checked("CLOSED_DAY", name, support)

    unusable = _closed_day_unusable(name, place, visit_date, facts)
    if unusable:
        return unusable

    cd = place["closed_days"]
    closed = closed_date_for(visit_date, cd, facts["holidays"])
    evidence = {
        "weekly": cd["weekly"],
        "fixed_dates": cd.get("fixed_dates"),
        "exception_rule": cd.get("exception_rule"),
        "effective_closed_date": closed,
        "source": cd.get("source"), "url": cd.get("url"),
        "checked_at": cd.get("checked_at"), "valid_until": cd.get("valid_until"),
    }
    if visit_date[5:] in (cd.get("fixed_dates") or []):
        return _fixed_date_closure(name, visit_date, cd, facts, evidence)

    if closed == visit_date:
        return check("CLOSED_DAY", name, FAIL, evidence=evidence,
                     detail=f"{visit_date}은 휴무일이다")
    return check("CLOSED_DAY", name, PASS, evidence=evidence)


def _holiday_dependent(cd: dict) -> bool:
    """휴무 규칙이 공휴일에 따라 달라지나 — 예외 규칙이 있으면 공휴일 캘린더가 필요하다."""
    return cd.get("exception_rule") not in (None, "none")


def _closed_day_unusable(name: str, place: dict, visit_date: str, facts: dict) -> dict | None:
    """휴무 정보가 없거나, 근거의 적용 기간 밖이거나, 공휴일 캘린더를 믿을 수 없으면 unknown. 쓸 수 있으면 None."""
    cd = place.get("closed_days")
    if not cd:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="NO_CLOSED_DAY_DATA",
                     detail=place.get("missing_note", "휴무일 정보를 확보하지 못했다"),
                     how_to_resolve=_no_data_resolution(place, "휴무일"))

    if not evidence_usable_on(cd, visit_date):
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"확보한 근거의 적용 기간(~{cd.get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "해당 장소의 휴무일 안내를 다시 확인"})

    if _holiday_dependent(cd) and not facts["holidays"]["verified"]:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="HOLIDAY_CALENDAR_UNVERIFIED",
                     detail="휴무일 예외 규칙이 공휴일 캘린더에 의존하는데, 그 값이 아직 검증되지 않았다",
                     how_to_resolve={"system": "공공데이터포털 특일 정보 API 호출"})
    if _holiday_dependent(cd) and not evidence_usable_on(facts["holidays"], visit_date):
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"공휴일 캘린더의 적용 기간(~{facts['holidays'].get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "방문 연도의 공휴일 캘린더를 다시 조회"})
    return None


def _no_data_resolution(place: dict, what: str) -> dict:
    """휴무일 · 운영시간 정보가 없을 때 누가 무엇을 하면 풀리나. 시장은 점포마다 달라 어느 점포인지 묻는다."""
    # 광장시장처럼 점포마다 시간이 다른 곳은 갈 점포를 알면 그 점포를 확인할 수 있다. 청계천 같은 길이나
    # 일반 시설에 "방문할 점포" 를 물으면 사용자가 답할 수 없다 — 그때는 우리가 정보를 확보해야 한다
    if place.get("type") == "market_meal":
        return {"user": "방문할 점포를 알려주세요", "system": f"시장 단위 {what} 정의 가능 여부 판단"}
    return {"system": f"이 장소의 {what}을 공식 출처에서 확보"}


def _fixed_date_closure(name: str, visit_date: str, cd: dict, facts: dict, evidence: dict) -> dict:
    """고정 휴관일(미술관 1월 1일)에 온 경우. 보통은 휴관이지만, 공휴일 정기휴일과 겹치면 규칙끼리 부딪힌다."""
    # ⭐ 고정일 휴관(미술관 1월 1일)은 요일 규칙과 따로 본다.
    #    다만 그날이 정기휴일 요일이면서 공휴일이면 공식 문구 두 개가 반대 결론을 낸다.
    #    2029-01-01(월) — "휴관 1월 1일" 이면 휴관, "월요일이 공휴일인 경우 정상 개관" 이면 개관.
    #    페이지에 우선순위가 없다. 어느 쪽으로 읽든 해석이지 확인한 사실이 아니므로
    #    그날은 확정하지 않는다.
    on_weekly = WEEKDAYS[Date.fromisoformat(visit_date).weekday()] in cd["weekly"]
    if _holiday_dependent(cd) and on_weekly and visit_date in facts["holidays"]["dates"]:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="RULE_CONFLICT",
                     detail=f"{visit_date}은 고정 휴관일이면서 공휴일인 정기휴일이다. "
                            f"공식 문구 두 개가 휴관과 개관으로 결론이 갈린다",
                     how_to_resolve={"system": "기관에 두 규칙의 우선순위 확인"},
                     reference=evidence)
    return check("CLOSED_DAY", name, FAIL, evidence=evidence,
                 detail=f"{visit_date}은 고정 휴관일({visit_date[5:]})이다")


# ── 운영시간 ────────────────────────────────────────────────────────
def hours_for(place: dict | None, visit_date: str, holidays: dict) -> dict | None:
    """그날 적용하는 시간표 한 줄. 공휴일 시간표 → 요일 · 월 시간표 순서로 고른다. 없으면 None."""
    if not place or not place.get("hours"):
        return None
    month = int(visit_date[5:7])
    wd = weekday_of(visit_date)
    is_holiday = visit_date in holidays["dates"]

    def month_ok(h):
        return not h.get("months") or month in h["months"]

    # ⭐ 공휴일 시간표가 요일 시간표보다 우선한다. 순서가 뒤바뀌면 틀린다.
    #    2026-10-09 한글날은 '금요일' 이지만 공휴일이라 19시에 닫는다.
    #    요일만 보면 금요일 시간표(21시)를 골라 18:30 입장을 통과시킨다 → 골든 테스트 T15
    if is_holiday:
        for h in place["hours"]:
            if h.get("holiday") and month_ok(h):
                return h
    for h in place["hours"]:
        if not month_ok(h):
            continue
        if h.get("weekdays") and wd not in h["weekdays"]:
            continue
        return h
    return None


def check_admission(name, place, visit_date, start, closed_status, holidays, support) -> dict:
    """도착 시각에 입장할 수 있나. 시간표 없이 결론이 나는 경우를 먼저 거르고, 그다음 시간표로 본다."""
    if closed_status == FAIL:
        return check("ADMISSION_NOT_POSSIBLE", name, NA,
                     reason="휴무일로 확정되어 운영시간 검사가 성립하지 않는다")

    # 휴무일 검사에서 장소를 특정하지 못했다면 운영시간이 없는 것이 아니라
    # 운영시간을 확인할 대상 자체가 없는 상태다. 두 경우를 구분해야 사용자가
    # 장소를 알려줘야 하는지, 시스템이 운영시간 데이터를 더 가져와야 하는지
    # 정확히 안내할 수 있다.
    if place is None:
        return place_not_checked("ADMISSION_NOT_POSSIBLE", name, support)

    without_hours = _admission_without_hours(name, place, visit_date, start, holidays)
    if without_hours:
        return without_hours
    return _admission_by_hours(name, place, visit_date, start, holidays)


def _admission_without_hours(name, place, visit_date, start, holidays) -> dict | None:
    """입장 방식(상시 개방 · 회차 입장)과 공휴일 캘린더만으로 결론이 나는 경우. 시간표를 봐야 하면 None."""
    adm = place.get("admission") or {}

    if adm and not evidence_usable_on(adm, visit_date):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"입장 방식 근거의 적용 기간(~{adm.get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "해당 장소의 입장 안내를 다시 확인"})

    # ⭐ not_applicable 도 근거가 필요하다.
    #    청계천을 "상시 개방이라 검사 불필요" 로 두고 싶지만, 그걸 공식 출처에서
    #    확인하지 않았다면 그것도 추측이다. 확인 전에는 unknown 이다.
    #    검사가 불필요하다는 주장조차 근거 없이 하지 않는다는 뜻이다.
    if adm.get("type") == "always_open":
        if adm.get("verified"):
            return check("ADMISSION_NOT_POSSIBLE", name, NA,
                         reason="상시 개방이라 운영시간 개념이 없다",
                         evidence={"source": adm.get("source"), "url": adm.get("url"),
                                   "checked_at": adm.get("checked_at"),
                                   "valid_until": adm.get("valid_until")})
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="UNVERIFIED_ALWAYS_OPEN",
                     detail=adm.get("unverified_note",
                                    "상시 개방으로 알려져 있으나 공식 근거를 확보하지 않았다"),
                     how_to_resolve={"system": "관리 주체의 공식 안내에서 상시 개방 여부 확인"})

    if not holidays.get("verified"):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="HOLIDAY_CALENDAR_UNVERIFIED",
                     detail="공휴일 여부에 따라 운영시간을 골라야 하지만 공휴일 캘린더가 검증되지 않았다",
                     how_to_resolve={"system": "공공데이터포털 특일 정보 API 호출"})
    if not evidence_usable_on(holidays, visit_date):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"공휴일 캘린더의 적용 기간(~{holidays.get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "방문 연도의 공휴일 캘린더를 다시 조회"})

    timed = timed_day(place, visit_date, holidays)
    if timed:
        return _timed_admission(name, timed, visit_date, start)
    return None


def timed_day(place: dict, visit_date: str, holidays: dict) -> dict | None:
    """그날이 회차 입장 날이면 입장 방식(admission)을, 아니면 None. 종묘 월 · 목 · 금 — 공휴일은 일반관람이다."""
    adm = place.get("admission") or {}
    if adm.get("type") != "timed_entry" or visit_date in holidays["dates"]:
        return None
    return adm if weekday_of(visit_date) in adm.get("timed_weekdays", []) else None


def _timed_admission(name, adm, visit_date, start) -> dict:
    """회차 입장 — 도착 뒤 첫 한국어 회차에 들어간다. 마지막 회차 뒤 도착은 위반, 마지막 회차 정각은 확인 못 함."""
    # ⭐ 회차 시간표를 연속 운영시간으로 바꾸지 않는다. 바꾸면 마지막 회차(16:20) 뒤 16:30 도착을 '입장마감 전' 으로
    #    통과시키고, 회차를 기다리는 시간도 놓친다. 기다리는 것은 개장 전 도착과 같다 — 그 회차 시각에 들어간 것으로
    #    보고 체류와 다음 이동을 거기서부터 센다(entry_at). 외국어 회차가 한국어 마지막 회차보다 늦게 남아 있으면
    #    그 회차로 들어갈 수 있는지 공식 안내가 말하지 않으므로 위반으로 확정하지 않는다
    season = "10-2" if int(visit_date[5:7]) in (10, 11, 12, 1, 2) else "3-9"
    times = (adm.get("entry_times_ko") or {}).get(season) or []
    others = [t for ts in (adm.get("entry_times_other_languages") or {}).values() for t in ts]
    evidence = {"admission_type": "timed_entry", "entry_kind": "회차", "planned_time": start, "entry_times": times,
                "tour_minutes": adm.get("dwell_fixed_minutes"), "rule_text": adm.get("rule_text"),
                "source": adm.get("source"), "url": adm.get("url"),
                "checked_at": adm.get("checked_at"), "valid_until": adm.get("valid_until")}
    if not times:
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN, evidence=evidence,
                     unknown_reason="NO_OPERATING_HOURS_DATA",
                     detail="그날 적용할 회차 시간표가 없다",
                     how_to_resolve={"system": "방문일에 적용되는 회차 시간표를 확인"})
    last = times[-1]
    if to_min(start) > to_min(last):
        if any(to_min(t) >= to_min(start) for t in others):
            return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN, evidence=evidence,
                         unknown_reason="NO_OPERATING_HOURS_DATA",
                         detail=f"한국어 마지막 회차({last}) 뒤 도착이다. 남은 외국어 회차로 들어갈 수 있는지 공식 안내가 없다",
                         how_to_resolve={"system": "외국어 회차에 다른 언어 관람객이 들어갈 수 있는지 확인"})
        return check("ADMISSION_NOT_POSSIBLE", name, FAIL, evidence=evidence,
                     detail=f"마지막 회차({last}) 뒤 도착 — 다른 언어 회차도 모두 그보다 이르다")
    if to_min(start) == to_min(last):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN, evidence=evidence,
                     unknown_reason="AT_LAST_ADMISSION",
                     detail=f"마지막 회차({last}) 정각 도착 — 공식 안내가 정각에 들어갈 수 있는지 말하지 않는다",
                     how_to_resolve={"user": f"{name} 마지막 회차({last}) 정각에 도착해요. 정각에 들어갈 수 있는지는 "
                                             f"공식 안내에 없어요. 조금 일찍 도착할 수 있나요?"})
    entry = next(t for t in times if to_min(t) >= to_min(start))
    evidence["entry_at"] = entry
    wait = to_min(entry) - to_min(start)
    return check("ADMISSION_NOT_POSSIBLE", name, PASS, evidence=evidence,
                 notice=(f"이날은 정해진 회차로만 들어간다 — {entry} 회차" + (f"까지 {wait}분 기다린다" if wait else "")
                         + f". 해설사를 따라 {adm.get('dwell_fixed_minutes')}분 정도 관람하고 일반관람은 안 된다. "
                         f"체류와 다음 이동은 {entry}부터 계산한다"))


def _admission_by_hours(name, place, visit_date, start, holidays) -> dict:
    """그날 시간표로 본다. 입장마감 뒤 도착이면 fail, 개장 전 도착이면 기다려서 개장 시각에 들어간다."""
    h = hours_for(place, visit_date, holidays)
    if h is None:
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="NO_OPERATING_HOURS_DATA",
                     detail=place.get("missing_note", "해당 날짜의 운영시간을 확보하지 못했다"),
                     how_to_resolve=_no_data_resolution(place, "운영시간"))
    if not evidence_usable_on(h, visit_date):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"운영시간 근거의 적용 기간(~{h.get('valid_until')})을 벗어났다")

    limit = h.get("last_admission") or h["close"]
    evidence = {
        "open": h["open"], "close": h["close"], "last_admission": h.get("last_admission"),
        "planned_time": start, "entry_at": start,
        "source": h.get("source"), "url": h.get("url"),
        "checked_at": h.get("checked_at"), "valid_until": h.get("valid_until"),
    }
    if to_min(start) > to_min(limit):
        return check("ADMISSION_NOT_POSSIBLE", name, FAIL, evidence=evidence,
                     detail=f"입장마감({limit}) 이후 도착")

    # ⭐ 입장마감 정각 도착은 공식 안내가 '…까지 입장' 이라고 규칙으로 적은 곳만 통과다.
    #    미술관은 '관람 종료 1시간 전까지 입장'(last_admission_rule)이라 '까지' 가 그 시각을 포함한다.
    #    궁궐 표는 '(입장마감 17:00)' 처럼 시각만 적어 정각에 들어가는지 말하지 않는다. 2026-10-09 한글날
    #    종묘는 17시에 입장을 막았다(블로그 후기의 현장 안내문 '5시부터 6시 사이에는 입장 불가').
    #    근거가 말하지 않는 것은 통과로도 위반으로도 확정하지 않는다
    if to_min(start) == to_min(limit) and h.get("last_admission") and not h.get("last_admission_rule"):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN, evidence=evidence,
                     unknown_reason="AT_LAST_ADMISSION",
                     detail=f"입장마감({limit}) 정각 도착 — 공식 안내가 정각에 들어갈 수 있는지 말하지 않는다",
                     how_to_resolve={"user": f"{name} 입장마감({limit}) 정각에 도착해요. 정각에 들어갈 수 있는지는 "
                                             f"공식 안내에 없어요. 조금 일찍 도착할 수 있나요?"})

    # ⭐ 개장 전 도착은 불가능이 아니다. 기다리면 개장 시각에 들어간다.
    #    8:30 에 경복궁(9:00 개장)에 오면 30분 기다려야 할 뿐이다. 지도 서비스도
    #    "9시에 엽니다" 를 안내로 보여줄 뿐 막지 않는다. 대신 입장 시각을 개장으로
    #    늦춰 체류와 다음 이동을 거기서부터 계산한다(entry_at). 그래서 늦어지면
    #    그 결과는 이동 검사에서 드러난다.
    if to_min(start) < to_min(h["open"]):
        evidence["entry_at"] = h["open"]
        return check("ADMISSION_NOT_POSSIBLE", name, PASS, evidence=evidence,
                     notice=f"개장({h['open']}) 전 도착 — {h['open']}까지 "
                            f"{to_min(h['open']) - to_min(start)}분 기다려야 한다. "
                            f"체류와 이동은 {h['open']}부터 계산한다")
    return check("ADMISSION_NOT_POSSIBLE", name, PASS, evidence=evidence)


def check_dwell(name, place, visit_date, start, dwell, admission_status, policy, holidays) -> dict:
    """
    입장 가능 여부와 계획한 체류를 마칠 수 있는지는 다른 문제다.
    입장마감 전에 들어가도 관람을 마치면 폐장 이후가 될 수 있다.
    체류시간을 사용자가 말하지 않았으면 이 검사는 판단하지 않는다.
    """
    if admission_status != PASS:
        return check("DWELL_NOT_COMPLETABLE", name, NA,
                     reason="입장 여부가 확정되지 않아 체류 검사가 성립하지 않는다")

    # ⭐ 9월 22일 비교 실험에서 이 프로젝트가 이긴 지점이다 (README '비교 실험').
    #    "16:30 에 경복궁" — 입장마감 17:00 전이라 들어갈 수는 있다. 그런데 몇 시간
    #    볼 건지 사용자가 말하지 않았다. 기본값 90분을 넣으면 18:00 폐장을 넘는다.
    #    우리가 정한 값으로 사용자 일정의 오류를 만들어내면 안 되므로 판단하지 않는다.
    #    같은 데이터를 준 LLM 은 이 규칙을 문서로 받고도 feasible 을 냈다 (T08). 10월 2일 실행에서는
    #    지켰지만 대신 하한선을 실제 이동시간처럼 썼다 (T01). 놓치는 규칙이 실행마다 바뀐다.
    if policy["require_user_dwell_for_completion_check"] and dwell["source"] != "user_stated":
        return check("DWELL_NOT_COMPLETABLE", name, UNKNOWN,
                     unknown_reason="NO_USER_DWELL",
                     detail="체류시간을 사용자가 말하지 않았다. 기본값으로 메워서 판정하지 않는다",
                     how_to_resolve={"user": f"{name}에서 얼마나 머무르실 예정인가요?"},
                     depends_on_assumptions=[dwell.get("assumption_id")])

    timed = timed_day(place, visit_date, holidays)
    if timed:
        return _dwell_on_tour(name, timed, start, dwell)

    h = hours_for(place, visit_date, holidays)
    end = to_min(start) + dwell["value"]
    evidence = {
        "dwell_minutes": dwell["value"], "dwell_source": dwell["source"],
        "end_time": to_hhmm(end), "closing_time": h["close"],
    }
    if end > to_min(h["close"]):
        return check("DWELL_NOT_COMPLETABLE", name, FAIL, evidence=evidence,
                     detail=f"계획한 체류 종료({to_hhmm(end)})가 폐장({h['close']}) 이후다")
    return check("DWELL_NOT_COMPLETABLE", name, PASS, evidence=evidence)


def _dwell_on_tour(name, adm, entry, dwell) -> dict:
    """회차 입장 날의 체류 — 해설 관람 시간 안이면 통과, 넘으면 그만큼 머물 수 있는지 확인 못 함."""
    # ⭐ 공식 문구는 '50분 정도' 라 확정 체류로 쓰지 않는다. 체류는 사용자가 말한 값이다. 그 값이 해설 시간 안이면
    #    회차가 끝나기 전에 마친다. 넘으면 일반관람이 안 되는 날이라 더 머물 수 있는지 공식 안내가 말하지 않는다
    tour = adm.get("dwell_fixed_minutes")
    end = to_min(entry) + dwell["value"]
    evidence = {"dwell_minutes": dwell["value"], "dwell_source": dwell["source"], "end_time": to_hhmm(end),
                "tour_minutes": tour, "rule_text": adm.get("rule_text"), "source": adm.get("source"),
                "url": adm.get("url"), "checked_at": adm.get("checked_at"), "valid_until": adm.get("valid_until")}
    if dwell["value"] <= tour:
        return check("DWELL_NOT_COMPLETABLE", name, PASS, evidence=evidence)
    return check("DWELL_NOT_COMPLETABLE", name, UNKNOWN, evidence=evidence,
                 unknown_reason="DWELL_BEYOND_TOUR",
                 detail=f"계획한 체류 {dwell['value']}분이 해설 관람({tour}분 정도)보다 길다. 일반관람이 안 되는 날이라 "
                        f"더 머물 수 있는지 공식 안내가 말하지 않는다",
                 how_to_resolve={"user": f"이날 {name}에서는 해설사를 따라 {tour}분 정도 관람해요. 그보다 오래 머물 수 "
                                         f"있는지는 공식 안내에 없어요. {tour}분 안으로 잡아도 될까요?"})


# ── 이동시간 ────────────────────────────────────────────────────────
def segment_key(segment, a: str, b: str) -> str:
    """판정할 때 받은 이동시간(TMAP)을 붙이는 이름 — 구간 번호(출발 장소 순번, 필수 일정이면 그 번호) · 출발 · 도착."""
    return f"{segment}:{a}|{b}"


def leg_for(facts: dict, key: str, a: str, b: str, visit_date: str, depart: str) -> dict | None:
    """A → B 구간의 이동시간. 방향이 있다 — A|B 가 있어도 B|A 는 따로 있어야 한다.

    판정할 때 받은 값(segment_key 로 붙은 구간)은 받은 날짜 · 출발 시각이 이 판정과 같을 때만 쓴다.
    아니면 저장해 둔 값(출발|도착 — 공공데이터 최소 이동시간 · 테스트용 이동시간)을 쓴다.
    """
    # ⭐ 예상 이동시간은 그 출발 시각의 교통이다. 같은 장소 사이를 오전 · 오후에 오가면 값이 다르다(3차 피드백:
    #    10:30 출발 45분 · 13:30 출발 10분). '출발|도착' 하나에 저장하면 나중 조회가 앞 조회를 덮어써 오전 구간을
    #    오후 값으로 통과시킨다. 그래서 구간 번호로 붙이고, 다시 쓰는 조건을 정한다 — 같은 구간(번호 · 출발 · 도착),
    #    같은 날짜, 같은 출발 시각. 수단은 구간에 함께 붙은 mode 와 택시 대안(alternatives)이 말해 준다.
    #    저장해 둔 값은 시각과 상관없는 최소 이동시간이나 테스트용 값이라 출발|도착으로 찾는다
    live = facts["legs"].get(key)
    if live and live.get("depart_at") == f"{visit_date}T{depart}":
        return live
    return facts["legs"].get(f"{a}|{b}")


def departure(st: dict) -> str:
    """장소를 나서는 시각 — 입장 시각에 사용자가 말한 체류를 더한다. 체류를 모르면 바로 나선다고 본다(0분)."""
    return to_hhmm(to_min(st["entry"]) + (st["dwell"]["value"] if st["dwell"]["source"] == "user_stated" else 0))


MODE_NAMES = {"metro": "지하철", "transit": "대중교통", "walk": "도보", "taxi": "택시"}
EVENT_NAMES = {"TRAIN_DEPARTURE": "열차 출발", "FLIGHT_DEPARTURE": "항공편 출발",
               "ARRIVE_BY": "도착 기한", "OTHER": "필수 일정"}


# ⭐ 대중교통으로 '된다' 가 안 나오면 택시 대안을 본다. pass 는 되는 수단이 하나라도 있으면
#    되므로(_travel 의 주석), 택시로 시간 안에 닿는 것도 '된다' 다. 대신 택시로 가야 한다는 것,
#    요금, 당일 교통에 따라 달라질 수 있다는 것을 알린다.
WHY_NOT_TRANSIT = {"LATE_BY_ESTIMATE": "늦을 가능성이 높아", "BUFFER_NOT_MET": "권장 여유가 모자라"}


def _taxi_notice(taxi: dict, lead: str) -> str:
    """택시 안내 문장 — 예상 시간과 요금, 당일 교통에 따라 달라진다는 말."""
    fare = f", 요금 약 {taxi['fare']:,}원" if taxi.get("fare") else ""
    return f"{lead} — 택시 예상 {taxi['minutes']}분{fare}. 당일 교통에 따라 달라질 수 있다"


def _add_notice(result: dict, text: str) -> dict:
    """결과에 안내 문장을 덧붙인다. 이미 있으면 ' / ' 로 잇는다."""
    result["notice"] = f"{result['notice']} / {text}" if result.get("notice") else text
    return result


def _with_taxi(result: dict, taxi: dict, assess) -> dict:
    """대중교통으로 '된다' 가 안 나온 결과(result)에 택시 대안을 적용한다.

    assess() 는 같은 구간을 택시 구간으로 판정한 결과를 돌려준다.
    결과 dict 는 이동 검사든 필수 조건의 policy 든 status · unknown_reason · notice 만 본다.
    """
    by_taxi = assess()
    if by_taxi["status"] == PASS:
        why = WHY_NOT_TRANSIT.get(result.get("unknown_reason"), "확인되지 않아")
        return _add_notice(by_taxi, _taxi_notice(
            taxi, f"대중교통으로는 {why} 택시 기준으로 확인했다. 택시로 가야 한다"))
    # 택시로도 늦거나 여유가 모자랄 때만 알린다. 체류시간을 몰라 보류한 것 등은 택시와 상관이 없다
    if by_taxi.get("unknown_reason") == "LATE_BY_ESTIMATE":
        _add_notice(result, _taxi_notice(taxi, "택시로도 시간 안에 닿기 어렵다"))
    elif by_taxi.get("unknown_reason") == "BUFFER_NOT_MET":
        _add_notice(result, _taxi_notice(taxi, "택시로도 권장 여유가 모자란다"))
    return result


def check_travel(frm, to, facts, policy, closed_statuses, visit_date) -> dict:
    """구간 하나의 이동 검사. 대중교통으로 '된다' 가 안 나오면 붙어 있는 택시 대안을 본다."""
    leg = leg_for(facts, segment_key(frm["index"], frm["name"], to["name"]), frm["name"], to["name"],
                  visit_date, departure(frm))
    result = _travel(frm, to, leg, policy, closed_statuses, visit_date)
    taxi = ((leg or {}).get("alternatives") or [None])[0]
    if taxi is None or result["status"] != UNKNOWN:
        return result
    return _with_taxi(result, taxi, lambda: _travel(frm, to, taxi, policy, closed_statuses, visit_date))


def _travel(frm, to, leg, policy, closed_statuses, visit_date) -> dict:
    """구간 하나를 leg 로 판정한다. 택시 대안은 보지 않는다 — check_travel 이 본다.

    순서: 양 끝이 휴무인가 → 이동 0분으로도 늦나 → 이 구간을 판정할 수 있나 → 이동시간으로 판정.
    """
    label = f"{frm['name']} → {to['name']}"
    if closed_statuses.get(frm["name"]) == FAIL or closed_statuses.get(to["name"]) == FAIL:
        return check("INSUFFICIENT_TRAVEL_TIME", label, NA,
                     reason="양 끝 중 한 곳이 휴무일이라 구간이 성립하지 않는다")

    # 다음 장소에 도착해야 하는 시각은 실제로 들어갈 수 있는 시각이다. 09:45 로 계획했어도
    # 10:00 에 열면 그 전에는 어차피 기다린다(check_admission). 10:00 까지만 가면 된다.
    target = to_min(to["entry"])
    by = to["entry"] if to["entry"] == to["start"] else f"{to['entry']}({to['entry_kind']})"
    stated_dwell = frm["dwell"]["source"] == "user_stated"

    return (_late_without_travel(label, frm, to, target, by, stated_dwell)
            or _leg_unusable(label, frm, to, leg, visit_date)
            or _travel_by_leg(label, frm, to, leg, policy, target, by, stated_dwell))


def _late_without_travel(label, frm, to, target, by, stated_dwell) -> dict | None:
    """이동 0분으로도 늦으면 fail. 아니면 None."""
    # ⭐ 이동시간이 0분이어도 늦으면 이동수단·이동시간 데이터와 상관없이 fail 이다.
    #    미술관 13:00 에 한 시간 보고 경복궁 13:50 이면 순간이동을 해도 늦는다.
    #    체류를 모르면 0분을 넣는다 — 다음 일정이 이번 일정보다 먼저 시작하는 경우다.
    earliest_departure = to_min(frm["entry"]) + (frm["dwell"]["value"] if stated_dwell else 0)
    if earliest_departure > target:
        return check("INSUFFICIENT_TRAVEL_TIME", label, FAIL,
                     evidence={"depart_at": to_hhmm(earliest_departure),
                               "planned_arrival": to["start"], "arrive_by": to["entry"],
                               "travel_minutes": 0},
                     detail=f"{frm['name']}에서 빨라도 {to_hhmm(earliest_departure)}에 나오는데 "
                            f"{to['name']}에는 {by}까지 가야 한다 — 이동시간이 0분이어도 늦는다")
    return None


def _leg_unusable(label, frm, to, leg, visit_date) -> dict | None:
    """이동시간으로 판정할 수 없는 구간이면 unknown — 구역 · 구간 없음 · 근거 기간 밖. 판정할 수 있으면 None."""
    # ⭐ 구역 일정은 대표 지점으로 판정하지 않는다 (장소 검사와 같은 원칙). "명동에서 쇼핑" 이면
    #    명동역이나 명동 중심점까지의 시간은 실제로 갈 가게까지의 시간이 아니다. 0분으로도 늦는
    #    경우만 위에서 확정하고, 나머지는 어디로 가는지 물어본다.
    area = next((s["name"] for s in (frm, to) if s.get("scope") == "area"), None)
    if area:
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="PLACE_NOT_RESOLVED",
                     detail=f"{area} 일정은 동네만 정해져 있어 이동시간을 판정하지 않는다",
                     how_to_resolve={"user": f"{area}에서 정확히 어디로 가시나요?"})

    if leg is None:
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="UNVERIFIED_TRAVEL_TIME",
                     detail="이 구간 이동시간을 확보하지 못했다",
                     how_to_resolve={"system": "TMAP 길찾기로 이 구간 이동시간 조회"})

    if not evidence_usable_on(leg, visit_date):
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"이동시간 근거의 적용 기간(~{leg.get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "해당 구간 이동시간을 다시 조회"})
    return None


def _mode_name(leg: dict) -> str:
    """구간의 이동수단을 화면에 쓰는 이름으로 — metro → 지하철."""
    return MODE_NAMES.get(leg.get("mode"), leg.get("mode") or "이동")


def _travel_by_leg(label, frm, to, leg, policy, target, by, stated_dwell) -> dict:
    """확보한 이동시간(leg)으로 판정한다. 늦음 · 체류시간 모름 · 여유 부족 · 하한선뿐 · 통과 중 하나다."""
    mode_name = _mode_name(leg)
    buffer = policy["buffer_minutes"]["transit_leg"]
    base = frm["dwell"]["value"]
    arrival = to_min(frm["entry"]) + base + leg["minutes"]
    evidence = {
        "depart_at": to_hhmm(to_min(frm["entry"]) + base),
        "travel_minutes": leg["minutes"], "buffer_minutes": buffer,
        "arrival": to_hhmm(arrival),
        "planned_arrival": to["start"], "arrive_by": to["entry"],
        "mode": leg.get("mode"),
        "method": leg.get("method"), "source": leg.get("source"),
        "snapshot_id": leg.get("snapshot_id"), "checked_at": leg.get("checked_at"),
        "is_lower_bound": leg.get("is_lower_bound", False),
        "excluded_from_travel_time": leg.get("excluded"),
    }

    # ⭐ 이동수단은 묻지도 보지도 않는다. 일정이 '되는지' 만 본다 (2026-10-05 결정).
    #      pass  되는 수단이 하나라도 있다   → 대중교통(아주 가까우면 걷기)이나 택시로 시간 안에 닿는다
    #      fail  어떤 수단으로도 안 된다     → 이동 0분으로도 늦을 때뿐이다
    #    지하철로 늦는다는 것은 "지하철로는 못 간다" 이지 "못 간다" 가 아니다. 택시로는
    #    갈 수 있을 수도 있다. 항공 최소 환승시간(MCT)도 수단이 정해진 연결에만 "불가능" 을
    #    적용하고, TripIt 은 수단을 모를 때 위험 경고만 한다. 사용자가 고른 수단으로 되는지는
    #    결과의 '검사하지 않는 것' 에 적는다 (report.NOT_CHECKED).
    if not stated_dwell:
        return _travel_without_dwell(label, frm, to, leg, mode_name, target, by, base, arrival, evidence)

    # ⭐ 하한선의 비대칭 — 한 방향으로만 알려 준다.
    #    저장해 둔 이동시간은 지하철 주행시간뿐이고 도보·환승·대기가 빠져 있다.
    #    빠진 몫이 얼마인지는 모른다. 그래도 부호는 안다 — 언제나 0 보다 크다.
    #
    #      하한선으로도 늦는다  → 지하철로는 확실히 늦다   빠진 값을 더하면 더 늦어질 뿐이다
    #      하한선으로는 도착한다 → 확정할 수 없다           나머지를 모른다
    #
    #    이게 "확보한 만큼만 말한다" 를 산수로 옮긴 것이다. 그래도 둘 다 unknown 이다. 지하철의
    #    하한선이지 모든 수단의 하한선이 아니라서, 지하철로 늦어도 택시로는 닿을 수 있다 (late).
    #
    # ⭐ 권장 여유는 fail 을 가르는 데 쓰지 않는다.
    #    "10분 여유를 두자" 는 우리가 정한 설정이지 사실이 아니다. 권장 여유까지 더해서
    #    늦는다고 fail 을 내면 설정값을 올리기만 해도 가능한 일정이 불가능이 된다.
    #    14:00 출발 · 14:15 시작 · 주행 6.5분이면 14:06 에 닿는다. 모자란 것은
    #    권장 여유이지 이동 자체가 아니다. 열차 조건과 같은 규칙이다.
    #
    #      이동만으로 늦는다              → unknown (LATE_BY_ESTIMATE · LATE_BY_LOWER_BOUND)
    #      닿지만 권장 여유를 못 채운다   → unknown (BUFFER_NOT_MET)  사용자가 판단한다
    #      권장 여유까지 채운다           → pass, 하한선이면 unknown (LOWER_BOUND_ONLY)
    if arrival > target:
        return _late_travel(label, to, leg, mode_name, f"{to_hhmm(arrival)} 도착 예상인데 {by}까지 가야 한다",
                            evidence)

    if arrival + buffer > target:
        spare = round(target - arrival)
        detail = (f"{by}보다 {spare}분 먼저 도착하지만, "
                  f"설정한 권장 여유 {buffer}분보다 {round(arrival + buffer - target)}분 부족하다")
        if leg.get("is_lower_bound"):
            detail += ". 최소 이동시간 기준이라 실제 도착은 이보다 늦다"
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="BUFFER_NOT_MET",
                     detail=detail,
                     how_to_resolve={"user": f"{to['name']} 시작 전 여유가 {spare}분뿐인데 괜찮으신가요?"},
                     reference=evidence)

    if leg.get("is_lower_bound"):
        missing = ", ".join(leg.get("excluded") or ["미확인 구성요소"])
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="LOWER_BOUND_ONLY",
                     detail=f"{mode_name} 탑승 시간({leg['minutes']}분)만으로는 "
                            f"도착하지만 {missing}이 빠져 있어 확정할 수 없다",
                     how_to_resolve={"system": "역 출입구 보행 경로·환승 이동시간·배차간격 확보"},
                     reference=evidence)
    return check("INSUFFICIENT_TRAVEL_TIME", label, PASS, evidence=evidence)


def _travel_without_dwell(label, frm, to, leg, mode_name, target, by, base, arrival, evidence) -> dict:
    """체류시간을 사용자가 말하지 않은 구간. 바로 나와도 늦으면 늦다고 알리고, 아니면 체류시간을 묻는다."""
    # ⭐ 사용자가 말하지 않은 체류시간으로는 판정하지 않는다 (T08 과 같은 원칙).
    #    fail 은 "어떻게 머물든 늦는다" 의 증명이어야 한다. 체류시간은 음수가 될 수
    #    없으므로 0분이 보장된 하한이다. 들어가자마자 나와도 늦을 때만 fail 이다.
    #    얼마나 오래 머물지는 모르므로 pass 도 낼 수 없다.
    #
    #    전에는 기본값을 ±50% 흔들어 둘 다 늦으면 fail 을 냈다. 하지만 궁궐 90분의
    #    45~135분은 우리 추측을 중심으로 한 범위일 뿐이다. 20분만 둘러보는 사람도
    #    있다. 기본값은 사용자에게 물어볼 때 "늦을 수 있다" 는 안내로만 쓴다.
    earliest = to_min(frm["entry"]) + leg["minutes"]
    if earliest > target:
        evidence.update(depart_at=frm["entry"], arrival=to_hhmm(earliest))
        return _late_travel(label, to, leg, mode_name,
                            f"{frm['name']}에서 바로 나와도 {to_hhmm(earliest)} 도착이라 {by}에 늦는다", evidence)

    detail = f"{frm['name']} 체류시간을 몰라 확정할 수 없다"
    if arrival > target:
        detail += f". 기본값 {base}분으로 보면 {to_hhmm(arrival)} 도착이라 늦을 수 있다"
    return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                 unknown_reason="NO_USER_DWELL",
                 detail=detail,
                 depends_on_assumptions=[frm["dwell"].get("assumption_id")],
                 how_to_resolve={"user": f"{frm['name']}에서 몇 시에 나오실 예정인가요?"},
                 reference=evidence)


def _late_travel(label, to, leg, mode_name, detail, evidence) -> dict:
    """이동시간으로는 늦는다 — 그래도 unknown 이다. 지하철 하한선인지 길찾기 예상인지에 따라 문장이 다르다."""
    # ⭐ 예상 이동시간으로는 fail 을 확정하지 않는다. 판정할 때 길찾기로 받는 시간은 가장
    #    그럴듯한 값이지 가장 빠른 값이 아니다 — 걸음이 빠르거나 앞 열차를 타면 더 일찍 닿는다.
    #    그래서 "늦을 가능성이 높다" 로 알리고 사용자에게 묻는다.
    if leg.get("is_lower_bound"):
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="LATE_BY_LOWER_BOUND",
                     detail=f"{mode_name}로 가면 {detail}. "
                            f"택시 등 다른 수단으로 갈 수 있는지는 확인하지 못했다",
                     how_to_resolve={"user": f"{mode_name}로는 {to['name']}에 늦어요. "
                                             f"시작을 늦추거나 더 일찍 출발할 수 있나요?"},
                     reference=evidence)
    return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                 unknown_reason="LATE_BY_ESTIMATE",
                 detail=f"{detail} — {mode_name} 예상 이동시간({leg['minutes']}분) 기준이라 확정하지 않는다",
                 how_to_resolve={"user": f"{to['name']}에 늦을 가능성이 높아요. "
                                         f"시작을 늦추거나 더 일찍 출발할 수 있나요?"},
                 reference=evidence)


# ── 필수 조건 (KTX 등) ──────────────────────────────────────────────
@dataclass(frozen=True)
class _Deadline:
    """필수 조건의 시각과 그 뜻. 열차 · 항공편은 출발 시각이라 그 시각에 닿으면 이미 놓친 것이다."""
    minutes: int      # 필수 조건 시각(분)
    departs: bool     # 출발 시각인가 — 열차 · 항공편
    name: str         # 화면에 쓰는 이름 — "열차 출발(20:30)"
    ok_arrival: str   # 확정하지 못한 것을 적을 때 쓰는 말

    def misses(self, arrival_min: float) -> bool:
        """그 시각에 닿으면 놓치는가. 출발 시각은 같은 시각도 놓친 것이고, 도착 기한은 그 시각까지 괜찮다."""
        return arrival_min >= self.minutes if self.departs else arrival_min > self.minutes


def evaluate_hard_constraint(hc, stops, facts, policy, visit_date) -> dict:
    """필수 조건 하나. 대중교통으로 '된다' 가 안 나오면 붙어 있는 택시 대안을 본다(check_travel 과 같다)."""
    leg = leg_for(facts, segment_key(hc["id"], stops[-1]["name"], hc["place"]), stops[-1]["name"], hc["place"],
                  visit_date, departure(stops[-1]))
    result = _hard_constraint(hc, stops, leg, policy, visit_date)
    taxi = ((leg or {}).get("alternatives") or [None])[0]
    if taxi is None or result["policy"]["status"] != UNKNOWN:
        return result
    result["policy"] = _with_taxi(result["policy"], taxi,
                                  lambda: _hard_constraint(hc, stops, taxi, policy, visit_date)["policy"])
    return result


def _hard_constraint(hc, stops, leg, policy, visit_date) -> dict:
    """
    사용자가 말한 것과 서비스가 정한 권장 기준을 섞지 않는다.

      - 열차 출발 시각      : 사용자가 말한 사실. 그대로 보존한다
      - 권장 도착 여유      : 우리가 정한 정책
      - 예상 도착 시각      : 계산값

    "설정한 여유가 N분 부족하다"까지가 확인되는 것이다. 그것만으로 실제 탑승
    불가능을 확정할 수 없고, 출발 전에 도착한다는 이유만으로 탑승 가능을
    보장할 수도 없다.
    """
    # ⭐ 사용자가 말한 사실과 우리가 정한 기준을 같은 자리에 섞지 않는다.
    #    event  = "20:30 에 KTX 가 떠난다"      사용자가 말한 것. 그대로 보존한다
    #    policy = "15분 전에 도착하는 게 좋다"   우리가 정한 것. 사실이 아니다
    #    이걸 합쳐서 "20:30 서울역 도착" 을 사용자 조건으로 저장하면, 나중에
    #    무엇이 사용자 요구였는지 알 수 없게 된다.
    event = {"type": hc["type"], "place": hc["place"], "time": hc["time"],
             "source": hc.get("source", "user_stated"), "raw": hc.get("raw")}
    return {"id": hc["id"], "event": event, "policy": _hc_policy(hc, stops[-1], leg, policy, visit_date)}


def _hc_policy(hc, last, leg, policy, visit_date) -> dict:
    """우리 기준(권장 도착 여유)으로 본 필수 조건 평가. 순서는 이동 검사와 같다 — 0분 → 판정 가능 → 이동시간."""
    # 권장 도착 여유. 정책에 기준이 없으면(항공편) None 이고, 그때는 pass 를 내지 않는다(아래 NO_BUFFER_RULE)
    buffer = policy["buffer_minutes"].get(hc.get("buffer_rule", "rail_boarding"))
    required = None if buffer is None else to_min(hc["time"]) - buffer

    policy_eval = {
        "buffer_minutes": buffer,
        "buffer_source": None if buffer is None else "system_default",
        "required_arrival": None if required is None else to_hhmm(required),
        # ⭐ status 와 severity 는 서로 다른 질문이다. 섞으면 위험한 문제가 숨는다.
        #      status   지금 정보로 확정할 수 있나   pass / fail / unknown / n.a.
        #      severity 실패하면 타격이 큰가        blocking / warning
        #    KTX 를 놓치는 건 정보가 부족해도 여전히 치명적이다. 그래서
        #    unknown 이라고 severity 를 낮추지 않는다. 낮추면 "중요도 낮음" 으로
        #    보여서 사용자가 확인을 건너뛴다.
        "severity": "blocking",
    }

    # 필수 조건마다 시각의 뜻이 다르다. 열차·항공편은 출발 시각이라 그 시각에 도착하면
    # 이미 놓친 것이다. "20:30 까지 도착" 은 20:30 에 닿으면 제시간이다.
    departs = hc["type"] in ("TRAIN_DEPARTURE", "FLIGHT_DEPARTURE")
    deadline = _Deadline(minutes=to_min(hc["time"]), departs=departs,
                         name=f"{EVENT_NAMES.get(hc['type'], '필수 일정')}({hc['time']})",
                         ok_arrival="실제 탑승 가능 여부" if departs else "실제로 제시간에 닿는지")
    stated_dwell = last["dwell"]["source"] == "user_stated"

    # check_travel 과 같다. 이동시간이 0분이어도 늦으면 수단과 상관없이 fail 이다.
    earliest_departure = to_min(last["entry"]) + (last["dwell"]["value"] if stated_dwell else 0)
    if deadline.misses(earliest_departure):
        policy_eval.update(
            status=FAIL,
            detail=f"{last['name']}에서 빨라도 {to_hhmm(earliest_departure)}에 나오는데 "
                   f"{deadline.name}에 늦는다 — 이동시간이 0분이어도 늦는다",
        )
        return policy_eval

    unusable = _hc_leg_unusable(hc, last, leg, visit_date)
    if unusable:
        policy_eval.update(unusable)
        return policy_eval
    return _hc_by_leg(policy_eval, last, leg, buffer, required, deadline, stated_dwell)


def _hc_leg_unusable(hc, last, leg, visit_date) -> dict | None:
    """마지막 장소에서 필수 조건 장소까지 이동시간으로 판정할 수 없으면 그 이유. 판정할 수 있으면 None."""
    # check_travel 과 같다. 마지막 일정이 구역이면 어디서 출발하는지 몰라 판정하지 않는다
    if last.get("scope") == "area":
        return {"status": UNKNOWN, "unknown_reason": "PLACE_NOT_RESOLVED",
                "detail": f"{last['name']} 일정은 동네만 정해져 있어 {hc['place']}까지 이동시간을 판정하지 않는다",
                "how_to_resolve": {"user": f"{last['name']}에서 정확히 어디서 출발하시나요?"}}
    if leg is None:
        return {"status": UNKNOWN, "unknown_reason": "UNVERIFIED_TRAVEL_TIME",
                "detail": f"{last['name']} → {hc['place']} 이동시간을 확보하지 못했다"}
    if not evidence_usable_on(leg, visit_date):
        return {"status": UNKNOWN, "unknown_reason": "EVIDENCE_EXPIRED",
                "detail": f"이동시간 근거의 적용 기간(~{leg.get('valid_until')})을 벗어난 방문일이다",
                "how_to_resolve": {"system": "해당 구간 이동시간을 다시 조회"}}
    return None


def _hc_by_leg(policy_eval, last, leg, buffer, required, deadline, stated_dwell) -> dict:
    """이동시간으로 본 필수 조건. 늦음 · 체류시간 모름 · 기준 없음(항공편) · 여유 부족 · 하한선뿐 · 통과 중 하나다."""
    mode_name = _mode_name(leg)
    base = last["dwell"]["value"]
    est = to_min(last["entry"]) + base + leg["minutes"]
    policy_eval.update(
        estimated_arrival=to_hhmm(est),
        travel_minutes=leg["minutes"],
        travel_source=leg.get("source"), travel_snapshot=leg.get("snapshot_id"),
        margin_vs_event=round(deadline.minutes - est),
        margin_vs_required=None if required is None else round(required - est),
        is_lower_bound=leg.get("is_lower_bound", False),
    )

    # 체류시간을 사용자가 말하지 않았으면 check_travel 과 같은 규칙이다.
    # 0분으로도 늦을 때만 fail 이고, 그 밖에는 확정하지 않는다. 기본값은 안내에만 쓴다.
    if not stated_dwell:
        return _hc_without_dwell(policy_eval, last, leg, mode_name, base, est, required, deadline)

    # 사용자가 반드시 지켜야 하는 것은 열차 출발 시각이다. 서비스가 정한
    # 15분 전 도착은 안전을 위한 권장 기준이지, 그 자체로 사용자 일정의
    # 확정 위반은 아니다.
    if deadline.misses(est):
        return _hc_late(policy_eval, leg, mode_name, deadline, f"예상 도착({to_hhmm(est)})이 {deadline.name}보다 늦다")
    if required is None:
        # ⭐ 권장 여유 기준이 없는 필수 조건은 pass 를 내지 않는다 (2차 피드백 1번 "가능하다고 단정해서도
        #    안 된다"). 항공편은 탑승 수속 · 보안 검색 마감이 항공사와 공항마다 달라 우리가 정한 값으로
        #    탈 수 있다고 할 수 없다. 출발 전에 닿는다는 것까지만 알린다. 이동 0분으로도 늦으면 위에서 fail 이다.
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="NO_BUFFER_RULE",
            detail=(f"{deadline.name} 전 {round(deadline.minutes - est)}분 도착 예상이지만, 탑승 수속 · 보안 검색에 필요한 "
                    f"시간 기준이 없어 탈 수 있는지 확정하지 않는다"),
            confirmed=f"현재 이동시간 기준으로는 {deadline.name} 전 {round(deadline.minutes - est)}분 도착한다",
            not_confirmed="탑승 수속 · 보안 검색을 마치고 탈 수 있는지",
            how_to_resolve={"user": "항공사 안내에서 탑승 수속 마감 시각을 확인해 주세요",
                            "system": "공항 · 항공사 공식 안내에서 권장 도착 시각 확보"},
        )
    elif est > required:
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="BUFFER_NOT_MET",
            detail=(
                f"{deadline.name} 전 {round(deadline.minutes - est)}분 도착 예상이지만, "
                f"설정한 권장 여유 {buffer}분보다 {round(est - required)}분 부족하다"
            ),
            confirmed=f"현재 이동시간 기준으로는 {deadline.name} 전 {round(deadline.minutes - est)}분 도착한다",
            not_confirmed=f"역 출입구 이동·탑승 절차를 포함한 {deadline.ok_arrival}",
            how_to_resolve={"user": f"{deadline.name} 전 여유가 {round(deadline.minutes - est)}분뿐인데 괜찮으신가요?",
                            "system": "역 출입구 보행 경로·탑승 절차·대기시간 확보"},
        )
    elif leg.get("is_lower_bound"):
        # 중요한 열차 일정인데 정보가 부족하다면 중요도가 낮은 문제가 아니라
        # 추가 확인이 필요한 중요한 문제다. severity 는 blocking 으로 둔다.
        missing = ", ".join(leg.get("excluded") or ["미확인 구성요소"])
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="LOWER_BOUND_ONLY",
            detail=f"최소 이동시간({leg['minutes']}분)으로는 권장 도착 시각에 맞지만 "
                   f"{missing}이 빠져 있다",
            confirmed=f"탑승 시간만으로는 {round(required - est)}분 여유가 있다",
            not_confirmed=deadline.ok_arrival,
            how_to_resolve={"system": "역 출입구 보행 경로·환승 이동시간·배차간격 확보"},
        )
    else:
        policy_eval["status"] = PASS
    return policy_eval


def _hc_without_dwell(policy_eval, last, leg, mode_name, base, est, required, deadline) -> dict:
    """마지막 장소 체류시간을 모르는 필수 조건. 바로 나와도 늦으면 늦다고 알리고, 아니면 체류시간을 묻는다."""
    earliest = to_min(last["entry"]) + leg["minutes"]
    if deadline.misses(earliest):
        policy_eval.update(
            estimated_arrival=to_hhmm(earliest),
            margin_vs_event=round(deadline.minutes - earliest),
            margin_vs_required=None if required is None else round(required - earliest),
        )
        return _hc_late(policy_eval, leg, mode_name, deadline,
                        f"{last['name']}에서 바로 나와도 {to_hhmm(earliest)} 도착이라 {deadline.name}에 늦는다")

    detail = f"{last['name']} 체류시간을 몰라 확정할 수 없다"
    if deadline.misses(est):
        detail += f". 기본값 {base}분으로 보면 {to_hhmm(est)} 도착이라 {deadline.name}에 늦을 수 있다"
    policy_eval.update(
        status=UNKNOWN,
        unknown_reason="NO_USER_DWELL",
        detail=detail,
        depends_on_assumptions=[last["dwell"].get("assumption_id")],
        how_to_resolve={"user": f"{last['name']}에서 몇 시에 나오실 예정인가요?"},
        confirmed=f"바로 나와도 {to_hhmm(earliest)} 이후에 도착한다",
        not_confirmed=f"{last['name']} 체류시간과 {deadline.ok_arrival}",
    )
    return policy_eval


def _hc_late(policy_eval, leg, mode_name, deadline, detail) -> dict:
    """필수 조건에 늦는다 — check_travel 의 늦음과 같은 규칙이다. fail 을 확정하지 않고 severity 는 blocking 그대로다."""
    # check_travel 의 late 와 같은 규칙이다. 예상 이동시간으로도, 지하철 하한선으로도 fail 을
    # 확정하지 않는다. severity 는 blocking 그대로다. 열차를 놓칠 수 있다는 경고가 사라지면 안 된다.
    if leg.get("is_lower_bound"):
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="LATE_BY_LOWER_BOUND",
            detail=f"{mode_name}로 가면 {detail}. 택시 등 다른 수단으로 갈 수 있는지는 확인하지 못했다",
            how_to_resolve={"user": f"{mode_name}로는 {deadline.name}에 늦어요. 더 일찍 출발할 수 있나요?"},
            not_confirmed="다른 이동수단으로 갔을 때의 도착 시각",
        )
    else:
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="LATE_BY_ESTIMATE",
            detail=f"{detail} — {mode_name} 예상 이동시간({leg['minutes']}분) 기준이라 확정하지 않는다",
            how_to_resolve={"user": f"{deadline.name}에 늦을 가능성이 높아요. 더 일찍 출발할 수 있나요?"},
            not_confirmed="실제 이동시간",
        )
    return policy_eval


# ── 본체 ────────────────────────────────────────────────────────────
def unconfirmed(result: dict, depends_on: list[dict]) -> dict:
    """
    추정한 입력에 기대는 pass/fail 을 unknown 으로 내린다. 계산한 결과는 참고로 남긴다.

    result 는 검사(check) 또는 필수 조건의 policy 평가다. 필수 조건은 severity 를 그대로 둔다.
    """
    if result["status"] not in (PASS, FAIL) or not depends_on:
        return result
    outcome = "통과로" if result["status"] == PASS else "위반으로"
    detail = f"추정한 입력으로 보면 {outcome} 나온다"
    if result.get("detail"):
        detail += f" — {result['detail']}"
    update = {
        "unknown_reason": "INFERRED_INPUT",
        "detail": detail + ". 추정한 값이 맞는지 확인하기 전에는 확정하지 않는다",
        "depends_on_assumptions": [a["id"] for a in depends_on],
        "how_to_resolve": {"user": " / ".join(a["ask_user"] for a in depends_on)},
    }
    if result.get("notice"):
        update["notice"] = result["notice"]
    if "type" in result:
        return check(result["type"], result["target"], UNKNOWN,
                     reference=result.get("evidence"), **update)
    return {**result, "status": UNKNOWN, **update}


def judge(itinerary: dict, facts: dict, policy: dict) -> dict:
    """일정 하나를 판정한다 — 이 파일의 본체.

    ① 입력이 성립하나 ② 날짜 · 요일 ③ 장소 준비(지원 범위 · 추정 시각 · 체류시간) ④ 장소 검사(휴무 · 입장 · 체류)
    ⑤ 구간 검사(이동) ⑥ 필수 조건(열차 등) ⑦ 전체 판정. 추정한 입력에 기대는 결과는 검사마다 바로 unknown 으로 내린다.
    """
    # ① ⭐ 입력이 비어 있을 때 시간 계산 예외를 내지 않는다.
    #    날짜·시각을 모르는 것은 "검사할 필요가 없다"가 아니라
    #    필요한 검사를 끝내지 못한 상태이므로 unknown 으로 남긴다.
    missing_inputs = input_checks(itinerary)
    if missing_inputs:
        return _missing_input_result(itinerary, missing_inputs)

    # ② 날짜를 추정했으면 기록하고, 말한 요일이 날짜와 다르면 어느 날인지 묻는다
    visit_date = itinerary["date"]
    assumptions = _date_assumptions(itinerary)
    stated_weekday = itinerary.get("weekday_stated")
    if stated_weekday and stated_weekday != weekday_of(visit_date):
        return _weekday_conflict_result(itinerary, facts, assumptions)

    # ⭐ LLM 이 추정한 입력에 기대는 판정은 pass 든 fail 이든 확정하지 않는다.
    #    "점심 먹고" → 12:00 은 LLM 의 짐작이다. 그 12:00 으로 "입장 불가" 를 내면
    #    추정치로 불가능을 단정한 것이고, "입장 가능" 을 내도 가능을 단정한 것이다.
    #    기본 체류시간과 같은 원칙이다. 결과는 참고로 남기고 추정한 값이 맞는지 묻는다.
    #
    #    연도는 다르다. "10월 8일 목요일" 처럼 요일을 말했고 그 요일이 맞으면(틀리면
    #    위에서 INPUT_CONFLICT) 날짜는 사용자 말로 확인된 것이다. 요일 없이 연도를
    #    채웠을 때만 추정으로 본다.
    date_dep = [] if stated_weekday else [a for a in assumptions if a["field"] == "date"]

    # ③ 장소마다 지원 범위 · 시작 시각 · 체류시간을 정리한다. 추정 시각과 기본 체류시간은 assumptions 에 남는다
    stops = []
    for i, s in enumerate(itinerary["stops"]):
        stops.append(_prepare_stop(i, s, facts, policy, assumptions))

    # ④ 장소 검사 ⑤ 구간 검사
    checks, closed_statuses = _place_checks(stops, visit_date, facts, policy, date_dep)
    checks += _travel_checks(stops, facts, policy, closed_statuses, visit_date)
    for n, c in enumerate(checks, 1):
        c["id"] = f"chk-{n:03d}"

    # ⑥ 필수 조건 ⑦ 전체 판정
    hard = _hard_constraints(itinerary, stops, facts, policy, visit_date)
    return {
        "date": visit_date,
        "date_source": itinerary.get("date_source", "explicit"),
        "weekday": weekday_of(visit_date),
        "weekday_stated": stated_weekday,
        "is_holiday": visit_date in facts["holidays"]["dates"],
        "summary": _summary(checks, hard),
        "assumptions": assumptions,
        "checks": checks,
        "hard_constraints": hard,
    }


def _missing_input_result(itinerary: dict, missing: list[dict]) -> dict:
    """필수 입력이 빠졌을 때의 결과. 계산하지 않고 무엇이 빠졌는지만 돌려준다."""
    for n, c in enumerate(missing, 1):
        c["id"] = f"chk-{n:03d}"
    counts = {s: sum(c["status"] == s for c in missing)
              for s in (PASS, FAIL, UNKNOWN, NA)}
    return {
        "date": itinerary.get("date"),
        "weekday": None,
        "is_holiday": None,
        "summary": {
            "verdict": "undetermined",
            "message": "필수 입력이 없어 판정을 보류했습니다",
            "counts": counts,
        },
        "assumptions": [],
        "checks": missing,
        "hard_constraints": [],
    }


def _date_assumptions(itinerary: dict) -> list[dict]:
    """자연어에서 연도를 추정해 채운 날짜면 기록한다(A1). 사용자 입력과 구분하기 위해서다."""
    if itinerary.get("date_source") != "inferred":
        return []
    return [{
        "id": "A1",
        "field": "date",
        "value": itinerary["date"],
        "source": "llm_inferred",
        "rule": "extract.inferred_date",
        "reason": "입력에 연도가 없어 구조화 추출 단계에서 가장 가까운 미래 날짜로 해석했다",
        "ask_user": "방문 날짜가 맞는지 확인해 주세요",
    }]


def _weekday_conflict_result(itinerary: dict, facts: dict, assumptions: list[dict]) -> dict:
    """말한 요일이 날짜와 다를 때의 결과. 불가능이 아니라 판정 보류이고, 어느 쪽이 맞는지 묻는다."""
    # 날짜와 요일이 맞지 않으면 사용자가 어느 날을 뜻했는지 모른다. "10월 8일 수요일" 은
    # 7일 수요일일 수도, 8일 목요일일 수도 있고, 어느 날이든 일정 자체는 문제없을 수 있다.
    visit_date, stated_weekday = itinerary["date"], itinerary["weekday_stated"]
    conflict = check(
        "INPUT_CONFLICT", "날짜와 요일", UNKNOWN,
        unknown_reason="MISMATCHED_WEEKDAY",
        detail=f"날짜 {visit_date}의 실제 요일은 {weekday_of(visit_date)}인데 "
               f"입력에는 {stated_weekday}로 적혀 있다",
        how_to_resolve={"user": "날짜와 요일 중 맞는 값을 확인해 주세요"},
    )
    conflict["id"] = "chk-001"
    return {
        "date": visit_date,
        "date_source": itinerary.get("date_source", "explicit"),
        "weekday": weekday_of(visit_date),
        "weekday_stated": stated_weekday,
        "is_holiday": visit_date in facts["holidays"]["dates"],
        "summary": {
            "verdict": "undetermined",
            "message": "입력한 날짜와 요일이 서로 맞지 않아 판정을 보류했습니다",
            "counts": {PASS: 0, FAIL: 0, UNKNOWN: 1, NA: 0},
        },
        "assumptions": assumptions,
        "checks": [conflict],
        "hard_constraints": [],
    }


def _prepare_stop(i: int, s: dict, facts: dict, policy: dict, assumptions: list[dict]) -> dict:
    """장소 하나를 판정용으로 정리한다. 추정한 시작 시각과 기본 체류시간을 쓰면 assumptions 에 덧붙인다."""
    # ⭐ area 일정은 대표 좌표나 비슷한 이름의 시설로 바꿔 판정하지 않는다.
    #    "북촌에서 점심"을 임의의 식당 운영시간으로 검사하면 다른 장소를
    #    검증한 셈이 된다. 사용자가 장소를 특정하기 전까지는 unknown 이다.
    support = support_scope(s, facts)
    scope = "area" if support == "area" else "place"
    place = facts["places"][s["place"]] if support == "supported" else None

    start_source = s.get("start_source", "explicit")
    start_dep = []
    if start_source == "inferred":
        aid = f"A{len(assumptions) + 1}"
        assumptions.append({
            "id": aid, "field": f"stops[{i}].start", "value": s["start"],
            "source": "llm_inferred", "rule": "extract.inferred_start",
            "reason": "입력의 문맥에서 시작 시각을 추정했다",
            "ask_user": f"{s['place']}에 {s['start']}에 도착하는 것이 맞나요?",
        })
        start_dep = [assumptions[-1]]
    # ⭐ 기본값을 쓰는 것 자체는 문제가 아니다. 쓴 것을 숨기는 게 문제다.
    #    source 로 user_stated / system_default 를 구분하고, 기본값을 쓸 때마다
    #    assumptions 에 '무슨 값을, 어떤 규칙으로, 왜' 넣었는지와 사용자에게
    #    물어볼 질문까지 남긴다. 그래야 판정 이유를 되짚을 수 있다.
    if s.get("dwell_minutes") is not None:
        dwell = {"value": s["dwell_minutes"], "source": "user_stated"}
    else:
        type_ = (place or {}).get("type", "street")
        value = policy["default_dwell_minutes"][type_]
        aid = f"A{len(assumptions) + 1}"
        assumptions.append({
            "id": aid, "field": f"stops[{i}].dwell_minutes", "value": value,
            "source": "system_default", "rule": f"default_dwell.{type_}",
            "reason": "체류시간 미입력. 장소 유형 기본값 적용",
            "ask_user": f"{s['place']}에서 얼마나 머무르실 예정인가요?",
        })
        dwell = {"value": value, "source": "system_default", "assumption_id": aid}
    return {"index": i, "name": s["place"], "place": place, "start": s["start"], "entry": s["start"],
            "start_source": start_source, "start_dep": start_dep, "scope": scope, "support": support,
            "scope_note": s.get("scope_note"), "dwell": dwell}


def _place_checks(stops, visit_date, facts, policy, date_dep) -> tuple[list[dict], dict[str, str]]:
    """장소마다 휴무 → 입장 → 체류를 본다. 개장 전 도착이면 입장 시각(entry)을 개장으로 늦춘다."""
    checks: list[dict] = []
    closed_statuses: dict[str, str] = {}

    # 추정 입력 처리는 검사 하나가 끝날 때마다 바로 한다. 휴무 결과를 입장 검사가,
    # 입장 결과를 체류 검사가 이어받기 때문에, 다 끝낸 뒤에 고치면 앞뒤가 어긋난다.
    for st in stops:
        c = unconfirmed(check_closed_day(st["name"], st["place"], visit_date, facts, st["support"]), date_dep)
        closed_statuses[st["name"]] = c["status"]
        a = unconfirmed(check_admission(st["name"], st["place"], visit_date, st["start"],
                                        c["status"], facts["holidays"], st["support"]),
                        date_dep + st["start_dep"])
        # 개장 전에 도착하면 개장 시각에, 회차 입장이면 다음 회차에 들어간다. 체류와 다음 이동은 거기서부터 센다.
        ev = a.get("evidence") or a.get("reference") or {}
        st["entry"], st["entry_kind"] = ev.get("entry_at", st["start"]), ev.get("entry_kind", "개장")
        d = unconfirmed(check_dwell(st["name"], st["place"], visit_date, st["entry"], st["dwell"],
                                    a["status"], policy, facts["holidays"]),
                        date_dep + st["start_dep"])
        checks += [c, a, d]
    return checks, closed_statuses


def _travel_checks(stops, facts, policy, closed_statuses, visit_date) -> list[dict]:
    """이웃한 장소 사이 구간마다 이동 검사. 마지막 장소 뒤는 구간이 없어 not_applicable 이다."""
    checks = []
    for i, st in enumerate(stops):
        if i == len(stops) - 1:
            checks.append(check("INSUFFICIENT_TRAVEL_TIME", f"{st['name']} → (없음)", NA,
                                reason="마지막 장소라 다음 구간이 없다"))
        else:
            checks.append(unconfirmed(
                check_travel(st, stops[i + 1], facts, policy, closed_statuses, visit_date),
                st["start_dep"] + stops[i + 1]["start_dep"]))
    return checks


def _hard_constraints(itinerary, stops, facts, policy, visit_date) -> list[dict]:
    """필수 조건마다 마지막 장소에서 제때 닿는지. 마지막 장소의 시각이 추정이면 결과를 확정하지 않는다."""
    hard = []
    for hc in itinerary.get("hard_constraints", []):
        h = evaluate_hard_constraint(hc, stops, facts, policy, visit_date)
        h["policy"] = unconfirmed(h["policy"], stops[-1]["start_dep"])
        hard.append(h)
    return hard


def _summary(checks: list[dict], hard: list[dict]) -> dict:
    """전체 판정과 사용자에게 보여 줄 문장, 상태별 개수."""
    # ⭐ 전체 판정은 '가장 나쁜 상태' 순서로 정한다. 다수결이 아니다.
    #    fail 이 하나라도 있으면 infeasible, 없어도 unknown 이 하나 있으면
    #    undetermined. 모든 검사가 통과해야만 feasible 이다.
    #    unknown 을 무시하고 "대부분 통과했으니 feasible" 을 내는 순간
    #    이 검증기는 확인하지 않은 것을 확인했다고 말하는 서비스가 된다.
    statuses = [c["status"] for c in checks] + [h["policy"]["status"] for h in hard]
    counts = {s: statuses.count(s) for s in (PASS, FAIL, UNKNOWN, NA)}
    verdict = ("infeasible" if counts[FAIL] else
               "undetermined" if counts[UNKNOWN] else "feasible")

    # ⭐ 사용자에게 "실행 가능한 일정입니다" 라고 말하지 않는다.
    #    예약·날씨·혼잡도는 검사 범위 밖이므로 그건 보장할 수 없는 말이다.
    #    확인한 범위를 문장에 그대로 담는다.
    message = {
        "feasible": "확인한 정보와 검사 범위 내에서 위반이 발견되지 않았습니다",
        "infeasible": "확인한 정보에서 실행할 수 없는 부분이 발견되었습니다",
        "undetermined": f"위반은 발견되지 않았지만 {counts[UNKNOWN]}개 항목을 확인하지 못했습니다",
    }[verdict]
    return {"verdict": verdict, "message": message, "counts": counts}
