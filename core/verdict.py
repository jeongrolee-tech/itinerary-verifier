"""판정 코어. 외부 호출이 없는 순수 함수다. 전체 흐름은 맨 아래 judge() 에 있다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date, timedelta
from typing import Any

# 못 끝낸 검사(unknown)와 할 필요 없는 검사(not_applicable)를 나눈다.
PASS, FAIL, UNKNOWN, NA = "pass", "fail", "unknown", "not_applicable"

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
HHMM = r"([01]\d|2[0-3]):[0-5]\d"


# 시간 계산
def to_min(hhmm: str) -> int:
    """"14:30" → 870."""
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
    return (Date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def evidence_usable_on(ev: dict | None, visit_date: str) -> bool:
    # 조회한 날이 아니라 적용 기간으로 본다. 9월에 본 운영시간을 내년 방문에 쓰지 않는다.
    if not ev:
        return False
    if ev.get("valid_from") and visit_date < ev["valid_from"]:
        return False
    if ev.get("valid_until") and visit_date > ev["valid_until"]:
        return False
    return True


def _provenance(ev: dict) -> dict:
    """근거의 출처 · 주소 · 확인한 날 · 적용 기간이다."""
    return {k: ev.get(k) for k in ("source", "url", "checked_at", "valid_until")}


def check(type_: str, target: str, status: str, **extra: Any) -> dict:
    return {
        "type": type_,
        "target": target,
        "status": status,
        "severity": "blocking" if status == FAIL else None,
        **extra,
    }


# 입력 확인
# 이 셋이 빠지면 휴무일과 운영시간을 하나도 볼 수 없다.
WHOLE_ITINERARY_INPUTS = {"MISSING_DATE", "INVALID_DATE", "MISSING_STOPS"}


def input_checks(itinerary: dict) -> list[dict]:
    """빠진 입력은 예외 대신 unknown 검사로 돌려준다."""
    return (_date_input(itinerary.get("date"))
            + _stops_input(itinerary.get("stops") or [])
            + _hard_constraints_input(itinerary.get("hard_constraints") or []))


def _date_input(visit_date) -> list[dict]:
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
    missing: list[dict] = []
    if not stops:
        missing.append(check(
            "INPUT_REQUIRED", "일정 장소", UNKNOWN,
            unknown_reason="MISSING_STOPS",
            detail="방문할 장소가 없어 일정의 실행 가능성을 확인할 수 없다",
            how_to_resolve={"user": "방문할 장소와 순서를 알려주세요"},
        ))

    for i, stop in enumerate(stops):
        missing += _stop_input(i, stop)
    return missing


def _stop_input(i: int, stop: dict) -> list[dict]:
    missing: list[dict] = []
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
    missing: list[dict] = []
    for i, constraint in enumerate(constraints, 1):
        if not constraint.get("place") or not constraint.get("time"):
            name = EVENT_NAMES.get(constraint.get("type"), "필수 일정")
            ask = (f"{constraint['place']} {name} 시각을 알려주세요" if constraint.get("place")
                   else f"{name}의 장소와 시각을 알려주세요")
            missing.append(check(
                "INPUT_REQUIRED", f"필수 일정 HC{i}", UNKNOWN,
                unknown_reason="INCOMPLETE_HARD_CONSTRAINT",
                detail="필수 일정에 장소 또는 시각이 없다",
                how_to_resolve={"user": ask},
            ))
        elif not re.fullmatch(HHMM, str(constraint["time"])):
            missing.append(check(
                "INPUT_REQUIRED", f"필수 일정 HC{i}", UNKNOWN,
                unknown_reason="INVALID_TIME",
                detail=f"필수 일정 시각({constraint['time']})을 HH:MM(24시간)으로 해석할 수 없다",
                how_to_resolve={"user": "필수 일정의 시각을 다시 알려주세요"},
            ))
    return missing


# 지원 범위
def support_scope(stop: dict, facts: dict) -> str:
    """추출 채점도 이 함수로 분류가 맞았는지 본다."""
    known = facts["places"].get(stop.get("place"))
    if stop.get("scope") == "area" or (known or {}).get("scope") == "area":
        return "area"
    return "supported" if known else "unsupported"


def place_not_checked(type_: str, name: str, support: str) -> dict:
    # 동네는 사용자에게 갈 곳을 묻고, 지원 목록 밖 장소는 우리가 데이터를 확보한다.
    if support == "area":
        return check(type_, name, UNKNOWN, unknown_reason="PLACE_NOT_RESOLVED",
                     detail="동네만 정해져 있어 방문할 시설이나 점포를 특정하지 못했다",
                     how_to_resolve={"user": f"{name}에서 정확히 어디로 가시나요?"})
    return check(type_, name, UNKNOWN, unknown_reason="UNSUPPORTED_PLACE",
                 detail="지원하는 장소 목록에 없는 곳이라 휴무일과 운영시간을 확인하지 않았다",
                 how_to_resolve={"system": f"{name}의 휴무일과 운영시간을 공식 출처에서 확보"})


# 휴무일
def closed_date_for(visit_date: str, cd: dict, holidays: dict) -> str | None:
    # 정기휴일이 공휴일이면 궁궐은 휴일을 미루고 미술관은 연다. 그래서 규칙을 facts 에 둔다.
    target = WEEKDAYS.index(cd["weekly"][0])
    cur = Date.fromisoformat(visit_date).weekday()
    base = shift_date(visit_date, -((cur - target) % 7))  # 방문일과 같거나 앞선 가장 가까운 정기휴일.

    if base not in holidays["dates"] or cd.get("exception_rule") in (None, "none"):
        return base
    if cd.get("exception_rule") == "open_if_holiday_no_shift":
        return None  # 그 주에는 휴일이 없다.

    d = shift_date(base, 1)
    for _ in range(14):
        if d not in holidays["dates"]:
            break
        d = shift_date(d, 1)
    return d


def check_closed_day(name: str, place: dict | None, visit_date: str, facts: dict, support: str) -> dict:
    if place is None:
        return place_not_checked("CLOSED_DAY", name, support)

    unusable = _closed_day_unusable(name, place, visit_date, facts)
    if unusable:
        return unusable

    cd = place["closed_days"]
    closed = closed_date_for(visit_date, cd, facts["holidays"])
    evidence = {"weekly": cd["weekly"], "fixed_dates": cd.get("fixed_dates"),
                "exception_rule": cd.get("exception_rule"), "effective_closed_date": closed, **_provenance(cd)}
    if visit_date[5:] in (cd.get("fixed_dates") or []):
        return _fixed_date_closure(name, visit_date, cd, facts, evidence)

    if closed == visit_date:
        weekly = WEEKDAY_NAMES[cd["weekly"][0]]
        why = (f"매주 {weekly}요일" if weekday_of(visit_date) in cd["weekly"]
               else f"{weekly}요일 정기휴일이 공휴일과 겹쳐 밀린 날")
        return check("CLOSED_DAY", name, FAIL, evidence=evidence,
                     detail=f"{visit_date}은 휴무일이다", notice=_closed_notice(name, why))
    return check("CLOSED_DAY", name, PASS, evidence=evidence)


WEEKDAY_NAMES = dict(zip(WEEKDAYS, "월화수목금토일"))


def _closed_notice(name: str, why: str) -> str:
    return (f"이날은 {name} 휴무일이에요({why}). 다른 날로 옮기거나 다른 곳을 고르세요. "
            "입장하지 않고 밖에서만 머물 거라면 그렇게 알려 주세요")


def _holiday_dependent(cd: dict) -> bool:
    return cd.get("exception_rule") not in (None, "none")


def _closed_day_unusable(name: str, place: dict, visit_date: str, facts: dict) -> dict | None:
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
    # 갈 점포를 묻는 곳은 시장뿐이다. 길이나 일반 시설에 물으면 사용자가 답할 수 없다.
    if place.get("type") == "market_meal":
        return {"user": "방문할 점포를 알려주세요", "system": f"시장 단위 {what} 정의 가능 여부 판단"}
    return {"system": f"이 장소의 {what}을 공식 출처에서 확보"}


def _fixed_date_closure(name: str, visit_date: str, cd: dict, facts: dict, evidence: dict) -> dict:
    # '1월 1일 휴관'과 '월요일이 공휴일이면 개관'이 부딪히는 날은 확정하지 않는다.
    on_weekly = weekday_of(visit_date) in cd["weekly"]
    if _holiday_dependent(cd) and on_weekly and visit_date in facts["holidays"]["dates"]:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="RULE_CONFLICT",
                     detail=f"{visit_date}은 고정 휴관일이면서 공휴일인 정기휴일이다. "
                            f"공식 문구 두 개가 휴관과 개관으로 결론이 갈린다",
                     how_to_resolve={"system": "기관에 두 규칙의 우선순위 확인"},
                     reference=evidence)
    month, day = (int(x) for x in visit_date[5:].split("-"))
    return check("CLOSED_DAY", name, FAIL, evidence=evidence,
                 detail=f"{visit_date}은 고정 휴관일({visit_date[5:]})이다",
                 notice=_closed_notice(name, f"매년 {month}월 {day}일"))


# 운영시간
def hours_for(place: dict | None, visit_date: str, holidays: dict) -> dict | None:
    if not place or not place.get("hours"):
        return None
    month = int(visit_date[5:7])
    wd = weekday_of(visit_date)
    is_holiday = visit_date in holidays["dates"]

    def month_ok(h):
        return not h.get("months") or month in h["months"]

    # 공휴일 시간표가 먼저다. 금요일인 한글날은 21시가 아니라 19시에 닫는다.
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
    if closed_status == FAIL:
        return check("ADMISSION_NOT_POSSIBLE", name, NA,
                     reason="휴무일로 확정되어 운영시간 검사가 성립하지 않는다")

    # 확인할 장소가 없는 것과 운영시간 데이터가 없는 것은 보류 이유를 나눈다.
    if place is None:
        return place_not_checked("ADMISSION_NOT_POSSIBLE", name, support)

    without_hours = _admission_without_hours(name, place, visit_date, start, holidays)
    if without_hours:
        return without_hours
    return _admission_by_hours(name, place, visit_date, start, holidays)


def _admission_without_hours(name, place, visit_date, start, holidays) -> dict | None:
    adm = place.get("admission") or {}

    if adm and not evidence_usable_on(adm, visit_date):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"입장 방식 근거의 적용 기간(~{adm.get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "해당 장소의 입장 안내를 다시 확인"})

    # 상시 개방이라 볼 필요가 없다는 것도 공식 근거가 있어야 not_applicable 이다.
    if adm.get("type") == "always_open":
        if adm.get("verified"):
            return check("ADMISSION_NOT_POSSIBLE", name, NA, reason="상시 개방이라 운영시간 개념이 없다",
                         evidence=_provenance(adm))
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
    """회차로만 들어가는 날이면 admission 을 돌려준다. 공휴일은 회차 요일이어도 일반관람이다."""
    adm = place.get("admission") or {}
    if adm.get("type") != "timed_entry" or visit_date in holidays["dates"]:
        return None
    return adm if weekday_of(visit_date) in adm.get("timed_weekdays", []) else None


def _timed_admission(name, adm, visit_date, start) -> dict:
    # 회차를 연속 운영시간으로 바꾸지 않는다. 바꾸면 마지막 회차 뒤 도착도 통과한다.
    season = "10-2" if int(visit_date[5:7]) in (10, 11, 12, 1, 2) else "3-9"
    times = (adm.get("entry_times_ko") or {}).get(season) or []
    others = [t for ts in (adm.get("entry_times_other_languages") or {}).values() for t in ts]
    evidence = {"admission_type": "timed_entry", "entry_kind": "회차", "planned_time": start, "entry_times": times,
                "tour_minutes": adm.get("dwell_fixed_minutes"), "rule_text": adm.get("rule_text"), **_provenance(adm)}
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
    evidence = {"open": h["open"], "close": h["close"], "last_admission": h.get("last_admission"),
                "planned_time": start, "entry_at": start, **_provenance(h)}
    if to_min(start) > to_min(limit):
        return check("ADMISSION_NOT_POSSIBLE", name, FAIL, evidence=evidence,
                     detail=f"입장마감({limit}) 이후 도착")

    # 입장마감 정각은 '…까지 입장'이라고 적힌 곳만 통과다. 시각만 적힌 곳은 확정하지 않는다.
    if to_min(start) == to_min(limit) and h.get("last_admission") and not h.get("last_admission_rule"):
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN, evidence=evidence,
                     unknown_reason="AT_LAST_ADMISSION",
                     detail=f"입장마감({limit}) 정각 도착 — 공식 안내가 정각에 들어갈 수 있는지 말하지 않는다",
                     how_to_resolve={"user": f"{name} 입장마감({limit}) 정각에 도착해요. 정각에 들어갈 수 있는지는 "
                                             f"공식 안내에 없어요. 조금 일찍 도착할 수 있나요?"})

    # 개장 전 도착은 불가능이 아니다. 기다렸다가 개장 시각에 들어간 것으로 본다.
    if to_min(start) < to_min(h["open"]):
        evidence["entry_at"] = h["open"]
        return check("ADMISSION_NOT_POSSIBLE", name, PASS, evidence=evidence,
                     notice=f"개장({h['open']}) 전 도착 — {h['open']}까지 "
                            f"{to_min(h['open']) - to_min(start)}분 기다려야 한다. "
                            f"체류와 이동은 {h['open']}부터 계산한다")
    return check("ADMISSION_NOT_POSSIBLE", name, PASS, evidence=evidence)


def check_dwell(name, place, visit_date, start, dwell, admission, policy, holidays) -> dict:
    """입장할 수 있어도 폐장 전에 체류를 마치는지는 따로 본다."""
    # 입장을 확인 못 했으면 체류도 미확인이다. 못 들어갈 때만 해당 없음이다.
    if admission["status"] == UNKNOWN:
        return check("DWELL_NOT_COMPLETABLE", name, UNKNOWN,
                     unknown_reason=admission["unknown_reason"],
                     detail="입장을 확인하지 못해 체류를 마칠 수 있는지도 확인하지 못했다",
                     how_to_resolve=admission.get("how_to_resolve"),
                     depends_on_assumptions=admission.get("depends_on_assumptions"))
    if admission["status"] != PASS:
        return check("DWELL_NOT_COMPLETABLE", name, NA,
                     reason="들어갈 수 없는 날이나 시각이라 체류 검사가 성립하지 않는다")

    # 사용자가 말하지 않은 체류는 기본값으로 메워 판정하지 않는다.
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
    # 해설 '50분 정도'는 체류로 쓰지 않는다. 체류는 사용자가 말한 값이다.
    tour = adm.get("dwell_fixed_minutes")
    end = to_min(entry) + dwell["value"]
    evidence = {"dwell_minutes": dwell["value"], "dwell_source": dwell["source"], "end_time": to_hhmm(end),
                "tour_minutes": tour, "rule_text": adm.get("rule_text"), **_provenance(adm)}
    if dwell["value"] <= tour:
        return check("DWELL_NOT_COMPLETABLE", name, PASS, evidence=evidence)
    return check("DWELL_NOT_COMPLETABLE", name, UNKNOWN, evidence=evidence,
                 unknown_reason="DWELL_BEYOND_TOUR",
                 detail=f"계획한 체류 {dwell['value']}분이 해설 관람({tour}분 정도)보다 길다. 일반관람이 안 되는 날이라 "
                        f"더 머물 수 있는지 공식 안내가 말하지 않는다",
                 how_to_resolve={"user": f"이날 {name}에서는 해설사를 따라 {tour}분 정도 관람해요. 그보다 오래 머물 수 "
                                         f"있는지는 공식 안내에 없어요. {tour}분 안으로 잡아도 될까요?"})


# 이동시간
def segment_key(segment, a: str, b: str) -> str:
    """판정 때 받은 TMAP 이동시간의 키. segment 는 출발 장소 순번이나 필수 일정 id 다."""
    return f"{segment}:{a}|{b}"


def leg_for(facts: dict, key: str, a: str, b: str, visit_date: str, depart: str) -> dict | None:
    """방향이 있다. A|B 가 있어도 B|A 는 따로 있어야 한다."""
    # TMAP 값은 그 출발 시각의 교통이라 같은 구간, 날짜, 출발 시각일 때만 다시 쓴다.
    live = facts["legs"].get(key)
    if live and live.get("depart_at") == f"{visit_date}T{depart}":
        return live
    return facts["legs"].get(f"{a}|{b}")


def departure(st: dict) -> str:
    return to_hhmm(to_min(st["entry"]) + (st["dwell"]["value"] if st["dwell"]["source"] == "user_stated" else 0))


MODE_NAMES = {"metro": "지하철", "transit": "대중교통", "walk": "도보", "taxi": "택시"}
EVENT_NAMES = {"TRAIN_DEPARTURE": "열차 출발", "FLIGHT_DEPARTURE": "항공편 출발",
               "ARRIVE_BY": "도착 기한", "OTHER": "필수 일정"}
# flight_boarding 은 기준을 확보하지 못해 policy 에 없다. 그래서 pass 가 나오지 않는다.
BUFFER_RULES = {"TRAIN_DEPARTURE": "rail_boarding", "FLIGHT_DEPARTURE": "flight_boarding",
                "ARRIVE_BY": "transit_leg", "OTHER": "transit_leg"}


# 대중교통으로 안 되면 택시 대안을 본다. 택시로 닿으면 통과하고 택시로 가야 한다고 알린다.
WHY_NOT_TRANSIT = {"LATE_BY_ESTIMATE": "늦을 가능성이 높아", "BUFFER_NOT_MET": "권장 여유가 모자라"}


def _taxi_notice(taxi: dict, lead: str) -> str:
    fare = f", 요금 약 {taxi['fare']:,}원" if taxi.get("fare") else ""
    return f"{lead} — 택시 예상 {taxi['minutes']}분{fare}. 당일 교통에 따라 달라질 수 있다"


def _add_notice(result: dict, text: str) -> dict:
    result["notice"] = f"{result['notice']} / {text}" if result.get("notice") else text
    return result


def _with_taxi(result: dict, taxi: dict, assess) -> dict:
    """result 는 대중교통으로 낸 결과다. assess() 는 같은 구간을 택시로 판정한다."""
    by_taxi = assess()
    if by_taxi["status"] == PASS:
        why = WHY_NOT_TRANSIT.get(result.get("unknown_reason"), "확인되지 않아")
        return _add_notice(by_taxi, _taxi_notice(
            taxi, f"대중교통으로는 {why} 택시 기준으로 확인했다. 택시로 가야 한다"))
    # 체류를 몰라 보류한 것 등은 택시와 상관없어 알리지 않는다.
    if by_taxi.get("unknown_reason") == "LATE_BY_ESTIMATE":
        _add_notice(result, _taxi_notice(taxi, "택시로도 시간 안에 닿기 어렵다"))
    elif by_taxi.get("unknown_reason") == "BUFFER_NOT_MET":
        _add_notice(result, _taxi_notice(taxi, "택시로도 권장 여유가 모자란다"))
    return result


def taxi_alternative(leg: dict | None, avoid_taxi: bool) -> dict | None:
    # 이동수단은 묻지 않지만 사용자가 먼저 택시를 뺐으면 그 말은 지킨다.
    return None if avoid_taxi else ((leg or {}).get("alternatives") or [None])[0]


def check_travel(frm, to, facts, policy, visit_date, avoid_taxi=False) -> dict:
    leg = leg_for(facts, segment_key(frm["index"], frm["name"], to["name"]), frm["name"], to["name"],
                  visit_date, departure(frm))
    result = _travel(frm, to, leg, policy, visit_date)
    taxi = taxi_alternative(leg, avoid_taxi)
    if taxi is None or result["status"] != UNKNOWN:
        return result
    return _with_taxi(result, taxi, lambda: _travel(frm, to, taxi, policy, visit_date))


def _travel(frm, to, leg, policy, visit_date) -> dict:
    # 휴무일인 곳이 끼어도 이동은 똑같이 본다. 휴무는 휴무일 검사가 알린다.
    label = f"{frm['name']} → {to['name']}"

    # 목표는 계획 시각이 아니라 입장 시각이다. 개장 전에 닿아도 어차피 기다린다.
    target = to_min(to["entry"])
    by = to["entry"] if to["entry"] == to["start"] else f"{to['entry']}({to['entry_kind']})"
    stated_dwell = frm["dwell"]["source"] == "user_stated"

    return (_late_without_travel(label, frm, to, target, by, stated_dwell)
            or _leg_unusable(label, frm, to, leg, visit_date)
            or _travel_by_leg(label, frm, to, leg, policy, target, by, stated_dwell))


def _late_without_travel(label, frm, to, target, by, stated_dwell) -> dict | None:
    # 이동 0분으로도 늦을 때만 수단과 상관없이 위반이다. 체류를 모르면 0분으로 본다.
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
    # 동네 일정은 역이나 중심점까지 시간으로 판정하지 않고 어디로 가는지 묻는다.
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
    return MODE_NAMES.get(leg.get("mode"), leg.get("mode") or "이동")


def _pass_basis(leg: dict, buffer: int, goal: str) -> str:
    # 예상 이동시간으로도 통과를 내지만 무엇에 기댄 통과인지 적는다.
    estimate = "TMAP 예상 " if leg.get("source") == "api_runtime" else ""
    return f"{estimate}{_mode_name(leg)} {leg['minutes']}분에 권장 여유 {buffer}분을 더해도 {goal}"


def _travel_by_leg(label, frm, to, leg, policy, target, by, stated_dwell) -> dict:
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

    # 이동수단은 보지 않는다. 되는 수단이 하나라도 있으면 통과다.
    if not stated_dwell:
        return _travel_without_dwell(label, frm, to, leg, mode_name, target, by, base, arrival, evidence)

    if arrival > target:
        return _late_travel(label, to, leg, mode_name, f"{to_hhmm(arrival)} 도착 예상인데 {by}까지 가야 한다",
                            evidence)

    # 권장 여유는 우리 설정이라 위반에 쓰지 않는다. 모자라면 묻는다.
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

    # 최소 이동시간은 한 방향만 말한다. 닿는다고 나와도 도보와 환승이 빠져 확정하지 않는다.
    if leg.get("is_lower_bound"):
        missing = ", ".join(leg.get("excluded") or ["미확인 구성요소"])
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="LOWER_BOUND_ONLY",
                     detail=f"{mode_name} 탑승 시간({leg['minutes']}분)만으로는 "
                            f"도착하지만 {missing}이 빠져 있어 확정할 수 없다",
                     how_to_resolve={"system": "역 출입구 보행 경로·환승 이동시간·배차간격 확보"},
                     reference=evidence)
    return check("INSUFFICIENT_TRAVEL_TIME", label, PASS, evidence=evidence,
                 detail=_pass_basis(leg, buffer, f"{by}까지 닿는다"))


def _travel_without_dwell(label, frm, to, leg, mode_name, target, by, base, arrival, evidence) -> dict:
    # 모르는 체류는 확실한 하한인 0분으로만 본다. 기본값은 물어볼 때 안내로만 쓴다.
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


def _late(leg, mode_name, detail, goal, fix) -> dict:
    """다음 장소와 필수 일정이 같이 쓴다."""
    # 예상 이동시간은 가장 빠른 값이 아니라서 늦어도 위반을 확정하지 않는다.
    if leg.get("is_lower_bound"):
        return {"unknown_reason": "LATE_BY_LOWER_BOUND",
                "detail": f"{mode_name}로 가면 {detail}. 택시 등 다른 수단으로 갈 수 있는지는 확인하지 못했다",
                "how_to_resolve": {"user": f"{mode_name}로는 {goal}에 늦어요. {fix}"}}
    return {"unknown_reason": "LATE_BY_ESTIMATE",
            "detail": f"{detail} — {mode_name} 예상 이동시간({leg['minutes']}분) 기준이라 확정하지 않는다",
            "how_to_resolve": {"user": f"{goal}에 늦을 가능성이 높아요. {fix}"}}


def _late_travel(label, to, leg, mode_name, detail, evidence) -> dict:
    return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                 **_late(leg, mode_name, detail, to["name"], "시작을 늦추거나 더 일찍 출발할 수 있나요?"),
                 reference=evidence)


# 필수 조건
@dataclass(frozen=True)
class _Deadline:
    minutes: int
    departs: bool
    name: str
    ok_arrival: str  # 확정하지 못한 것을 적을 때 쓰는 말.

    def misses(self, arrival_min: float) -> bool:
        """출발 시각은 그 시각에 닿아도 놓친 것이고, 도착 기한은 그 시각까지 괜찮다."""
        return arrival_min >= self.minutes if self.departs else arrival_min > self.minutes


def evaluate_hard_constraint(hc, stops, facts, policy, visit_date, avoid_taxi=False) -> dict:
    leg = leg_for(facts, segment_key(hc["id"], stops[-1]["name"], hc["place"]), stops[-1]["name"], hc["place"],
                  visit_date, departure(stops[-1]))
    result = _hard_constraint(hc, stops, leg, policy, visit_date)
    taxi = taxi_alternative(leg, avoid_taxi)
    if taxi is None or result["policy"]["status"] != UNKNOWN:
        return result
    result["policy"] = _with_taxi(result["policy"], taxi,
                                  lambda: _hard_constraint(hc, stops, taxi, policy, visit_date)["policy"])
    return result


def _hard_constraint(hc, stops, leg, policy, visit_date) -> dict:
    # 사용자가 말한 시각(event)과 우리가 정한 여유(policy)를 한자리에 섞지 않는다.
    return {"id": hc["id"], "event": event_of(hc), "policy": _hc_policy(hc, stops[-1], leg, policy, visit_date)}


def event_of(hc: dict) -> dict:
    return {"type": hc["type"], "place": hc.get("place"), "time": hc.get("time"),
            "source": hc.get("source", "user_stated"), "raw": hc.get("raw")}


def _hc_policy(hc, last, leg, policy, visit_date) -> dict:
    # 항공편처럼 정책에 기준이 없으면 None 이다.
    buffer = policy["buffer_minutes"].get(hc.get("buffer_rule") or BUFFER_RULES.get(hc["type"]))
    required = None if buffer is None else to_min(hc["time"]) - buffer

    policy_eval = {
        "buffer_minutes": buffer,
        "buffer_source": None if buffer is None else "system_default",
        "required_arrival": None if required is None else to_hhmm(required),
        # status 는 확정 여부, severity 는 놓쳤을 때의 타격이다. unknown 이어도 낮추지 않는다.
        "severity": "blocking",
    }

    departs = hc["type"] in ("TRAIN_DEPARTURE", "FLIGHT_DEPARTURE")
    deadline = _Deadline(minutes=to_min(hc["time"]), departs=departs,
                         name=f"{EVENT_NAMES.get(hc['type'], '필수 일정')}({hc['time']})",
                         ok_arrival="실제 탑승 가능 여부" if departs else "실제로 제시간에 닿는지")
    stated_dwell = last["dwell"]["source"] == "user_stated"

    # 이동 검사와 같다. 이동 0분으로도 늦을 때만 위반이다.
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

    if not stated_dwell:
        return _hc_without_dwell(policy_eval, last, leg, mode_name, base, est, required, deadline)

    # 지켜야 하는 것은 출발 시각이다. 15분 전 도착은 권장일 뿐 위반 기준이 아니다.
    if deadline.misses(est):
        return _hc_late(policy_eval, leg, mode_name, deadline, f"예상 도착({to_hhmm(est)})이 {deadline.name}보다 늦다")
    if required is None:
        # 항공편은 수속 마감이 항공사마다 달라 기준이 없으니 통과를 내지 않는다.
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
        policy_eval.update(status=PASS, detail=_pass_basis(
            leg, buffer, f"{deadline.name} 전에 닿는다" if deadline.departs else f"{deadline.name}까지 닿는다"))
    return policy_eval


def _hc_without_dwell(policy_eval, last, leg, mode_name, base, est, required, deadline) -> dict:
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
    policy_eval.update(status=UNKNOWN, **_late(leg, mode_name, detail, deadline.name, "더 일찍 출발할 수 있나요?"),
                       not_confirmed="다른 이동수단으로 갔을 때의 도착 시각" if leg.get("is_lower_bound")
                       else "실제 이동시간")
    return policy_eval


# 본체
def unconfirmed(result: dict, depends_on: list[dict]) -> dict:
    """추정한 입력에 기대는 pass, fail 을 unknown 으로 내리고 계산 결과는 참고로 남긴다."""
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
    # 장소 하나의 입력이 빠지면 그 장소와 앞뒤 구간만 미확인으로 두고 나머지는 검사한다.
    missing_inputs = input_checks(itinerary)
    if any(c["unknown_reason"] in WHOLE_ITINERARY_INPUTS for c in missing_inputs):
        return _missing_input_result(itinerary, missing_inputs)

    visit_date = itinerary["date"]
    assumptions = _date_assumptions(itinerary)
    stated_weekday = itinerary.get("weekday_stated")
    if stated_weekday and stated_weekday != weekday_of(visit_date):
        return _weekday_conflict_result(itinerary, facts, assumptions)

    # 추정 입력에 기대는 결과는 확정하지 않는다. 말한 요일이 맞으면 날짜는 확인된 것이다.
    date_dep = [] if stated_weekday else [a for a in assumptions if a["field"] == "date"]

    # 이름이나 시각이 빠진 장소는 None 으로 두고 입력 검사로 묻는다.
    stop_inputs = [_stop_input(i, s) for i, s in enumerate(itinerary["stops"])]
    seq = [None if stop_inputs[i] else _prepare_stop(i, s, facts, policy, assumptions)
           for i, s in enumerate(itinerary["stops"])]

    place_checks = _place_checks([st for st in seq if st], visit_date, facts, policy, date_dep)
    untimed = {i: _untimed_place_checks(s, stop_inputs[i][0], visit_date, facts, policy, date_dep)
               for i, s in enumerate(itinerary["stops"]) if stop_inputs[i] and s.get("place")}
    # 시각이 빠진 장소는 들어갈 시간대를 계산할 수 있으면 묻지 않고 알린다.
    windows = {i: w for i in untimed
               if (w := _windowed(i, itinerary, seq, stop_inputs, facts, policy, date_dep, untimed[i]))}
    untimed.update({i: w["place"] for i, w in windows.items()})
    checks = _in_itinerary_order(seq, stop_inputs, untimed, place_checks)
    checks += _travel_checks(seq, stop_inputs, facts, policy, visit_date,
                             itinerary.get("avoid_taxi", False), itinerary.get("hard_constraints") or [], windows)
    for n, c in enumerate(checks, 1):
        c["id"] = f"chk-{n:03d}"

    hard = _hard_constraints(itinerary, seq, stop_inputs, facts, policy, visit_date, assumptions)
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
    # 어느 날을 뜻했는지 몰라 불가능이 아니라 보류로 두고 묻는다.
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
    # 동네 일정은 대표 좌표나 비슷한 이름의 시설로 바꿔 판정하지 않는다.
    support = support_scope(s, facts)
    scope = "area" if support == "area" else "place"
    place = facts["places"][s["place"]] if support == "supported" else None

    start_dep = []
    if s.get("start_source") == "inferred":
        aid = f"A{len(assumptions) + 1}"
        assumptions.append({
            "id": aid, "field": f"stops[{i}].start", "value": s["start"],
            "source": "llm_inferred", "rule": "extract.inferred_start",
            "reason": "입력의 문맥에서 시작 시각을 추정했다",
            "ask_user": f"{s['place']}에 {s['start']}에 도착하는 것이 맞나요?",
        })
        start_dep = [assumptions[-1]]
    # 기본값을 쓰면 무엇을 왜 넣었는지와 물어볼 말을 assumptions 에 남긴다.
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
            "start_dep": start_dep, "scope": scope, "support": support, "dwell": dwell,
            "outside_only": bool(s.get("outside_only"))}


def _place_checks(stops, visit_date, facts, policy, date_dep) -> list[dict]:
    checks: list[dict] = []

    # 뒤 검사가 앞 결과를 이어받아서 unconfirmed 는 검사마다 바로 한다.
    for st in stops:
        if st["outside_only"]:
            checks += _outside_checks(st["name"], st["place"], visit_date, facts, st["support"])
            continue
        c = unconfirmed(check_closed_day(st["name"], st["place"], visit_date, facts, st["support"]), date_dep)
        a = unconfirmed(check_admission(st["name"], st["place"], visit_date, st["start"],
                                        c["status"], facts["holidays"], st["support"]),
                        date_dep + st["start_dep"])
        # 체류와 다음 이동은 개장이나 회차에 맞춘 실제 입장 시각부터 센다.
        ev = a.get("evidence") or a.get("reference") or {}
        st["entry"], st["entry_kind"] = ev.get("entry_at", st["start"]), ev.get("entry_kind", "개장")
        d = unconfirmed(check_dwell(st["name"], st["place"], visit_date, st["entry"], st["dwell"],
                                    a, policy, facts["holidays"]),
                        date_dep + st["start_dep"])
        checks += [c, a, d]
    return checks


def _untimed_place_checks(s: dict, missing: dict, visit_date: str, facts: dict, policy: dict,
                          date_dep: list[dict]) -> list[dict]:
    # 시각이 없어도 휴무일은 날짜로 확인한다. 입장과 체류만 시각을 묻는다.
    name = s["place"]
    support = support_scope(s, facts)
    place = facts["places"][name] if support == "supported" else None
    if s.get("outside_only"):
        return _outside_checks(name, place, visit_date, facts, support)
    closed = unconfirmed(check_closed_day(name, place, visit_date, facts, support), date_dep)
    if closed["status"] == FAIL:
        entry = check("ADMISSION_NOT_POSSIBLE", name, NA, reason="휴무일로 확정되어 운영시간 검사가 성립하지 않는다")
    else:
        entry = check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN, unknown_reason=missing["unknown_reason"],
                      detail=missing["detail"], how_to_resolve=missing["how_to_resolve"])
    # 체류는 입장을 따라가니 시각과 체류 없이도 정해진다.
    return [closed, entry, check_dwell(name, place, visit_date, None, None, entry, policy, facts["holidays"])]


def _outside_checks(name: str, place: dict | None, visit_date: str, facts: dict, support: str) -> list[dict]:
    # 들어가지 않는다고 했으면 휴무여도 위반이 아니다. 쉬는 날이면 알리기만 한다.
    reason = "입장하지 않고 밖에서만 머문다고 했다"
    closed = check("CLOSED_DAY", name, NA, reason=reason)
    if check_closed_day(name, place, visit_date, facts, support)["status"] == FAIL:
        closed["notice"] = f"이날은 {name} 휴무일이에요 — 입장하지 않는다고 해서 위반으로 보지 않았어요"
    return [closed, check("ADMISSION_NOT_POSSIBLE", name, NA, reason=reason),
            check("DWELL_NOT_COMPLETABLE", name, NA, reason=reason)]


# 시각이 빠진 장소의 시간대
# 가장 이른 입장은 앞 장소 출발 + 이동 + 권장 여유다. 시각은 채우지 않고 이동 0분으로도 안 될 때만 위반이다.

def _window_candidate(i: int, itinerary: dict, seq: list, stop_inputs: list, facts: dict) -> bool:
    s, visit_date, holidays = itinerary["stops"][i], itinerary["date"], facts["holidays"]
    if [c["unknown_reason"] for c in stop_inputs[i]] != ["MISSING_START_TIME"]:
        return False  # 시각만 빠진 경우에 계산한다.
    if s.get("outside_only") or s.get("dwell_minutes") is None or support_scope(s, facts) != "supported":
        return False  # 나설 시각이나 운영시간을 모른다.
    place = facts["places"][s["place"]]
    if hours_for(place, visit_date, holidays) is None or timed_day(place, visit_date, holidays):
        return False
    prev = seq[i - 1] if i > 0 else None
    nxt = seq[i + 1] if i + 1 < len(seq) else None
    if (i > 0 and prev is None) or (i + 1 < len(seq) and nxt is None):
        return False  # 연달아 빠지면 가장 이른 시각을 정할 수 없다.
    if prev and (prev["start_dep"] or prev["scope"] == "area" or prev["dwell"]["source"] != "user_stated"):
        return False  # 앞 장소를 나서는 시각이 추정이거나 모른다.
    if nxt and (nxt["start_dep"] or nxt["scope"] == "area"):
        return False
    lo, hi = to_min(prev["start"]) if prev else 0, to_min(nxt["start"]) if nxt else 24 * 60
    # 사이에 필수 일정이 끼거나 낄지 모르면 실제 동선을 모른다.
    return not any(not re.fullmatch(HHMM, str(hc.get("time") or "")) or lo <= to_min(hc["time"]) < hi
                   for hc in itinerary.get("hard_constraints") or [])


def _earliest_arrival(i: int, itinerary: dict, seq: list, facts: dict, policy: dict) -> int | None:
    """첫 장소면 문 여는 시각이다. 최소 이동시간뿐이면 가장 이른 시각을 정할 수 없어 None 이다."""
    s, visit_date = itinerary["stops"][i], itinerary["date"]
    if i == 0:
        return to_min(hours_for(facts["places"][s["place"]], visit_date, facts["holidays"])["open"])
    prev = seq[i - 1]
    depart = departure(prev)
    leg = leg_for(facts, segment_key(prev["index"], prev["name"], s["place"]), prev["name"], s["place"],
                  visit_date, depart)
    if leg is None or leg.get("is_lower_bound") or not evidence_usable_on(leg, visit_date):
        return None
    arrive = to_min(depart) + leg["minutes"] + policy["buffer_minutes"]["transit_leg"]
    return arrive if arrive < 24 * 60 else None


def _windowed(i, itinerary, seq, stop_inputs, facts, policy, date_dep, fallback) -> dict | None:
    """가장 이른 입장으로 장소와 앞뒤 이동을 판정한다. None 이면 fallback 대로 시각을 묻는다."""
    if not _window_candidate(i, itinerary, seq, stop_inputs, facts):
        return None
    arrive = _earliest_arrival(i, itinerary, seq, facts, policy)
    if arrive is None:
        return None
    s, visit_date, avoid_taxi = itinerary["stops"][i], itinerary["date"], itinerary.get("avoid_taxi", False)
    prev = seq[i - 1] if i > 0 else None
    nxt = seq[i + 1] if i + 1 < len(seq) else None

    def at(minute, dep):
        st = _prepare_stop(i, {**s, "start": to_hhmm(minute), "start_source": "explicit"}, facts, policy, [])
        place = _place_checks([st], visit_date, facts, policy, dep)
        return (st, place, prev and check_travel(prev, st, facts, policy, visit_date, avoid_taxi),
                nxt and check_travel(st, nxt, facts, policy, visit_date, avoid_taxi))

    st, place, before, after = at(arrive, [])
    if place[0]["status"] == FAIL:
        return None  # 쉬는 날은 시각과 상관없어 휴무 위반인 fallback 을 쓴다.
    if all(c["status"] == PASS for c in [*place, before, after] if c):
        if date_dep:  # 추정한 날짜로는 장소 검사를 확정하지 않는다.
            place = at(arrive, date_dep)[1]
        place[1] = _with_window(place[1], st, prev, nxt, after, visit_date, facts, policy)
        return {"place": place, "before": before, "after": after}
    return _no_window(s["place"], prev, arrive, at, fallback, date_dep)


def _with_window(admission, st, prev, nxt, after, visit_date, facts, policy) -> dict:
    """시간대 끝은 입장마감, 폐장 전 체류 완료, 다음 장소에 맞출 시각 중 가장 이른 것이다."""
    h, dwell = hours_for(st["place"], visit_date, facts["holidays"]), st["dwell"]["value"]
    limit = h.get("last_admission") or h["close"]
    bounds = [(to_min(limit), bool(h.get("last_admission")) and not h.get("last_admission_rule")),
              (to_min(h["close"]) - dwell, False)]
    if nxt:  # 이동시간은 가장 이른 입장 기준이라 늦게 나서면 달라질 수 있다.
        travel = after["evidence"]["travel_minutes"]
        bounds.append((to_min(nxt["entry"]) - policy["buffer_minutes"]["transit_leg"] - travel - dwell, False))
    latest, before = min(bounds, key=lambda b: (b[0], not b[1]))  # 같은 시각이면 '그 전'이 더 엄하다.
    span = f"{st['entry']}부터 {to_hhmm(latest)} 전에" if before else f"{st['entry']}~{to_hhmm(latest)} 사이에"
    how = f"{prev['name']}에서 나와 바로 가는 것으로" if prev else "문 여는 시각부터"
    admission = _add_notice(admission, f"{st['name']}에는 {span} 들어가면 돼요. "
                                       f"들어갈 시각을 말하지 않아서 {how} 계산했어요")
    admission["window"] = {"from": st["entry"], "to": to_hhmm(latest), "before": before}
    return admission


def _no_window(name, prev, arrive, at, fallback, date_dep) -> dict | None:
    """이동 0분으로 닿아도 안 되는 검사만 위반으로 확정하고 나머지는 시각을 묻는다."""
    st, place, _, after = at(to_min(departure(prev)) if prev else arrive, date_dep)
    lead = f"{name}에 들어갈 시각을 말하지 않아 가장 이른 입장 시각({st['entry']})으로 봤다. "
    out = {"place": list(fallback), "before": None, "after": None}
    if place[1]["status"] == FAIL:  # 못 들어가면 체류는 해당 없음이다.
        out["place"][1:] = [{**place[1], "detail": lead + place[1]["detail"]}, place[2]]
    elif place[2]["status"] == FAIL:
        out["place"][2] = {**place[2], "detail": lead + place[2]["detail"]}
    if after and after["status"] == FAIL:
        out["after"] = {**after, "detail": lead + after["detail"]}
    return out if out["after"] or out["place"] != fallback else None


def _timed_stops(itinerary: dict, facts: dict, policy: dict) -> tuple[list, list]:
    """judge 와 똑같이 장소를 준비한다. _place_checks 는 entry 를 채우려고만 부른다."""
    stop_inputs = [_stop_input(i, s) for i, s in enumerate(itinerary["stops"])]
    seq = [None if stop_inputs[i] else _prepare_stop(i, s, facts, policy, [])
           for i, s in enumerate(itinerary["stops"])]
    _place_checks([st for st in seq if st], itinerary["date"], facts, policy, [])
    return stop_inputs, seq


def window_candidate(itinerary: dict, i: int, facts: dict, policy: dict) -> bool:
    """길찾기가 장소 i 앞 구간의 이동시간을 물을지 정한다."""
    stop_inputs, seq = _timed_stops(itinerary, facts, policy)
    return _window_candidate(i, itinerary, seq, stop_inputs, facts)


def window_departure(itinerary: dict, i: int, facts: dict, policy: dict) -> str | None:
    """길찾기가 뒤 구간을 물을 출발 시각. 판정 때와 같아야 받은 이동시간을 쓴다."""
    stop_inputs, seq = _timed_stops(itinerary, facts, policy)
    if not _window_candidate(i, itinerary, seq, stop_inputs, facts):
        return None
    arrive = _earliest_arrival(i, itinerary, seq, facts, policy)
    if arrive is None:
        return None
    st = _prepare_stop(i, {**itinerary["stops"][i], "start": to_hhmm(arrive), "start_source": "explicit"},
                       facts, policy, [])
    _place_checks([st], itinerary["date"], facts, policy, [])  # entry 를 개장이나 회차 시각에 맞춘다.
    leave = departure(st)
    return leave if to_min(leave) < 24 * 60 else None  # 자정을 넘겨 나서면 그날 길을 묻지 않는다.


def _in_itinerary_order(seq, stop_inputs, untimed, place_checks) -> list[dict]:
    """시각이 빠진 장소는 untimed 를, 이름이 빠진 장소는 입력 검사를 넣는다."""
    out, it = [], iter(place_checks)
    for i, (st, inputs) in enumerate(zip(seq, stop_inputs)):
        out += untimed[i] if i in untimed else inputs if st is None else [next(it), next(it), next(it)]
    return out


def _travel_checks(seq, stop_inputs, facts, policy, visit_date, avoid_taxi, hard_constraints, windows) -> list[dict]:
    """한쪽 장소가 빠진 구간은 windows 결과를 쓰거나 그 장소의 입력 질문으로 묻는다."""
    checks = []
    for i, st in enumerate(seq):
        if i == len(seq) - 1:
            if st:
                checks.append(check("INSUFFICIENT_TRAVEL_TIME", f"{st['name']} → (없음)", NA,
                                    reason="마지막 장소라 다음 구간이 없다"))
        elif st and seq[i + 1] and (hc := crossing_constraint(hard_constraints, st, seq[i + 1])):
            checks.append(_across_constraint(st, seq[i + 1], hc))
        elif st and seq[i + 1]:
            checks.append(unconfirmed(
                check_travel(st, seq[i + 1], facts, policy, visit_date, avoid_taxi),
                st["start_dep"] + seq[i + 1]["start_dep"]))
        elif (w := windows.get(i + 1)) and w["before"]:
            checks.append(w["before"])
        elif (w := windows.get(i)) and w["after"]:
            checks.append(w["after"])
        else:
            missing = (stop_inputs[i] or stop_inputs[i + 1])[0]
            checks.append(check("INSUFFICIENT_TRAVEL_TIME", f"{_stop_name(seq, stop_inputs, i)} → "
                                f"{_stop_name(seq, stop_inputs, i + 1)}", UNKNOWN,
                                unknown_reason=missing["unknown_reason"],
                                detail="앞이나 뒤 장소의 입력이 빠져 이동을 판정하지 못한다",
                                how_to_resolve=missing.get("how_to_resolve")))
    return checks


def crossing_constraint(hard_constraints: list[dict], a: dict, b: dict) -> dict | None:
    """길찾기도 이 함수로 같은 구간을 건너뛴다."""
    # 사이에 필수 일정이 끼면 a 에서 b 로 바로 가지 않으니 이 구간은 판정하지 않는다.
    return next((hc for hc in hard_constraints if re.fullmatch(HHMM, str(hc.get("time") or ""))
                 and to_min(a["start"]) <= to_min(hc["time"]) < to_min(b["start"])), None)


def _across_constraint(frm: dict, to: dict, hc: dict) -> dict:
    name = f"{EVENT_NAMES.get(hc['type'], '필수 일정')}({hc['time']} {hc.get('place') or '장소 모름'})"
    return check("INSUFFICIENT_TRAVEL_TIME", f"{frm['name']} → {to['name']}", UNKNOWN,
                 unknown_reason="MIDDAY_HARD_CONSTRAINT",
                 detail=f"사이에 {name}이 있어 이 구간만으로는 실제 이동을 알 수 없다",
                 how_to_resolve={"system": "필수 일정을 그 시각 앞뒤 장소와 잇는 검사 구현"})


def _stop_name(seq, stop_inputs, i) -> str:
    return seq[i]["name"] if seq[i] else stop_inputs[i][0]["target"]


def _hard_constraints(itinerary, seq, stop_inputs, facts, policy, visit_date, assumptions) -> list[dict]:
    hard, from_last = [], []
    stops = [st for st in seq if st]
    for n, hc in enumerate(itinerary.get("hard_constraints", [])):
        # 장소나 시각이 빠져도 필수 일정은 결과에서 빼지 않고 남겨서 묻는다.
        missing = _hard_constraints_input([hc])
        if missing:
            hard.append({"id": hc["id"], "event": event_of(hc), "policy": {
                "status": UNKNOWN, "unknown_reason": missing[0]["unknown_reason"], "severity": "blocking",
                "detail": missing[0]["detail"], "how_to_resolve": missing[0]["how_to_resolve"]}})
            from_last.append(hard[-1])  # 시각이나 장소를 몰라 어디에 낄지 모른다.
            continue
        hc_dep = _hard_constraint_assumption(n, hc, assumptions)
        if is_midday(hc, itinerary["stops"]):
            hard.append(_midday_result(hc))
            continue
        if seq[-1] is None:
            missing = stop_inputs[-1][0]
            hard.append({"id": hc["id"], "event": event_of(hc), "policy": {
                "status": UNKNOWN, "unknown_reason": missing["unknown_reason"], "severity": "blocking",
                "detail": "마지막 장소의 입력이 빠져 필수 일정에 제때 닿는지 판정하지 못한다",
                "how_to_resolve": missing.get("how_to_resolve")}})
        else:
            h = evaluate_hard_constraint(hc, stops, facts, policy, visit_date, itinerary.get("avoid_taxi", False))
            h["policy"] = unconfirmed(h["policy"], stops[-1]["start_dep"] + hc_dep)
            hard.append(h)
        from_last.append(hard[-1])
    # 하나씩은 마지막 장소에서 바로 가도 둘 이상을 한 사람이 다 지킬 수 있는지는 모른다.
    if len(from_last) > 1:
        for h in from_last:
            h["policy"] = _not_together(h["policy"], len(from_last))
    return hard


def _not_together(p: dict, n: int) -> dict:
    """하나만 봐도 놓치는 위반은 그대로 두고, 따로 계산한 통과는 참고로 남긴다."""
    if p["status"] == FAIL:
        return p
    notice = f"필수 일정이 {n}개라 서로 함께 지킬 수 있는지는 아직 검사하지 않아요. 직접 확인해 주세요"
    if p["status"] != PASS:
        return _add_notice(p, notice)
    return _add_notice({
        **p, "status": UNKNOWN, "unknown_reason": "MULTIPLE_HARD_CONSTRAINTS",
        "detail": f"필수 일정이 {n}개다. 하나씩은 제때 닿지만 한 사람이 모두 지킬 수 있는지는 확인하지 못했다",
        "confirmed": f"따로 보면 {p['detail']}", "not_confirmed": "필수 일정을 모두 함께 지킬 수 있는지",
        "how_to_resolve": {"system": "필수 일정 여러 개를 잇는 검사 구현"}}, notice)


def _hard_constraint_assumption(n: int, hc: dict, assumptions: list[dict]) -> list[dict]:
    # "저녁 기차"를 19:00 으로 읽은 시각도 장소 시각처럼 확정하지 않는다.
    if hc.get("time_source") != "inferred":
        return []
    name = EVENT_NAMES.get(hc["type"], "필수 일정")
    assumptions.append({
        "id": f"A{len(assumptions) + 1}", "field": f"hard_constraints[{n}].time", "value": hc["time"],
        "source": "llm_inferred", "rule": "extract.inferred_hard_constraint_time",
        "reason": "입력의 문맥에서 필수 일정 시각을 추정했다",
        "ask_user": f"{hc['place']} {name} 시각이 {hc['time']}이 맞나요?",
    })
    return [assumptions[-1]]


def is_midday(hc: dict, stops: list[dict]) -> bool:
    """같은 시각에 시작하는 장소는 겹치는 것이라 중간으로 보지 않는다."""
    # 필수 일정은 마지막 장소에서 출발한다고 계산하므로 하루 마지막 교통편에만 맞다.
    return any(re.fullmatch(HHMM, str(s.get("start") or "")) and to_min(s["start"]) > to_min(hc["time"])
               for s in stops)


def _midday_result(hc: dict) -> dict:
    name = f"{EVENT_NAMES.get(hc['type'], '필수 일정')}({hc['time']} {hc['place']})"
    return {"id": hc["id"], "event": event_of(hc), "policy": {
        "status": UNKNOWN, "unknown_reason": "MIDDAY_HARD_CONSTRAINT", "severity": "blocking",
        "detail": f"{name}이 하루 중간에 있다. 필수 일정은 아직 하루 마지막 교통편만 판정한다",
        "how_to_resolve": {"system": "필수 일정을 그 시각 앞뒤 장소와 잇는 검사 구현"},
        "notice": f"{name}은 하루 중간이라 아직 검사하지 않아요. 앞 장소에서 제때 닿는지는 직접 확인해 주세요"}}


def verdict_of(statuses: list[str]) -> str:
    """다수결이 아니라 가장 나쁜 상태로 정한다. unknown 이 하나만 있어도 feasible 이 아니다."""
    return "infeasible" if FAIL in statuses else "undetermined" if UNKNOWN in statuses else "feasible"


def _summary(checks: list[dict], hard: list[dict]) -> dict:
    statuses = [c["status"] for c in checks] + [h["policy"]["status"] for h in hard]
    counts = {s: statuses.count(s) for s in (PASS, FAIL, UNKNOWN, NA)}
    verdict = verdict_of(statuses)

    # 예약이나 날씨는 보지 않으니 "가능하다"고 하지 않고 확인한 범위만 말한다.
    message = {
        "feasible": "확인한 정보와 검사 범위 내에서 위반이 발견되지 않았습니다",
        "infeasible": "확인한 정보에서 실행할 수 없는 부분이 발견되었습니다",
        "undetermined": f"위반은 발견되지 않았지만 {counts[UNKNOWN]}개 항목을 확인하지 못했습니다",
    }[verdict]
    return {"verdict": verdict, "message": message, "counts": counts}
