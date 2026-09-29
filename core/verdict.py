"""
판정 코어 — 순수 함수. 외부 API를 호출하지 않는다.

입력: 일정(itinerary) + 사실(facts) + 정책(policy)  — 전부 인자로 받는다
출력: 검사별 상태 + 필수 조건 평가 + 전체 판정

이 파일에 네트워크 호출이 들어가는 순간 고정 입력으로 테스트할 수 없게 된다.
"""

from __future__ import annotations

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


# ── 시간 유틸 (전부 '분' 단위 정수로 다룬다) ──────────────────────────
def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: float) -> str:
    m = int(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"


def weekday_of(iso: str) -> str:
    return WEEKDAYS[Date.fromisoformat(iso).weekday()]


def shift_date(iso: str, days: int) -> str:
    return (Date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def is_last_wednesday(iso: str) -> bool:
    """문화가 있는 날 — 매달 마지막 수요일. 이 날은 종묘가 일반관람으로 열린다."""
    d = Date.fromisoformat(iso)
    return d.weekday() == 2 and (d + timedelta(days=7)).month != d.month


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
    return {
        "type": type_,
        "target": target,
        "status": status,
        "severity": extra.pop("severity", "blocking") if status == FAIL else None,
        **extra,
    }


def input_checks(itinerary: dict) -> list[dict]:
    """
    판정을 시작하기 전에 반드시 있어야 하는 입력을 확인한다.

    추출 단계는 날짜나 시작 시각을 None 으로 남길 수 있다. 그 값을 그대로
    시간 계산에 넘기면 예외로 죽는다. 입력이 부족한 것도 검증 결과의
    일부이므로 오류를 내지 않고 unknown 검사로 돌려준다.
    """
    missing: list[dict] = []

    visit_date = itinerary.get("date")
    if not visit_date:
        missing.append(check(
            "INPUT_REQUIRED", "일정 날짜", UNKNOWN,
            unknown_reason="MISSING_DATE",
            detail="방문 날짜가 없어 운영시간과 휴무일을 확인할 수 없다",
            how_to_resolve={"user": "방문할 날짜를 알려주세요"},
        ))
    else:
        try:
            Date.fromisoformat(visit_date)
        except (TypeError, ValueError):
            missing.append(check(
                "INPUT_REQUIRED", "일정 날짜", UNKNOWN,
                unknown_reason="INVALID_DATE",
                detail=f"방문 날짜({visit_date})를 YYYY-MM-DD 형식으로 해석할 수 없다",
                how_to_resolve={"user": "방문 날짜를 YYYY-MM-DD 형식으로 알려주세요"},
            ))

    stops = itinerary.get("stops") or []
    if not stops:
        missing.append(check(
            "INPUT_REQUIRED", "일정 스톱", UNKNOWN,
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

    for i, constraint in enumerate(itinerary.get("hard_constraints") or [], 1):
        if not constraint.get("place") or not constraint.get("time"):
            missing.append(check(
                "INPUT_REQUIRED", f"필수 조건 HC{i}", UNKNOWN,
                unknown_reason="INCOMPLETE_HARD_CONSTRAINT",
                detail="필수 조건에 장소 또는 시각이 없다",
                how_to_resolve={"user": "필수 이동수단의 장소와 출발 시각을 알려주세요"},
            ))

    return missing


# ── 휴무일 ──────────────────────────────────────────────────────────
def closed_date_for(visit_date: str, cd: dict, holidays: dict) -> str | None:
    """
    기관마다 규칙이 다르다. 코드에 박지 않고 facts 의 exception_rule 로 분기한다.
      shift_to_next_nonholiday : 정기휴일이 공휴일이면 개방하고, 다음 첫 비공휴일이 휴일이 된다 (궁궐)
      open_if_holiday_no_shift : 정기휴일이 공휴일이면 개방하고, 밀지 않는다 (미술관)
    """
    # ⭐ "월요일이면 휴관" 같은 규칙을 코드에 박으면 안 된다.
    #    2026-10-06 은 궁궐은 휴궁이고 미술관은 개관이다. 규칙이 기관마다 다르다.
    #    그래서 규칙 이름을 facts 에 두고 코드는 그것을 읽어 분기한다.
    #    골든 테스트 T05 와 T06 이 같은 날짜로 정답이 반대인 이유가 이것이다.
    target = WEEKDAYS.index(cd["weekly"][0])
    cur = Date.fromisoformat(visit_date).weekday()
    base = shift_date(visit_date, -((cur - target) % 7))  # 가장 가까운 과거(또는 당일) 정기휴일

    if base not in holidays["dates"]:
        return base
    if cd.get("exception_rule") == "open_if_holiday_no_shift":
        return None  # 그 주에는 휴일이 없다

    d = shift_date(base, 1)
    for _ in range(14):
        if d not in holidays["dates"]:
            break
        d = shift_date(d, 1)
    return d


def check_closed_day(name: str, place: dict | None, visit_date: str, facts: dict) -> dict:
    if place is None:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="PLACE_NOT_RESOLVED",
                     detail="장소를 특정하지 못했다")

    cd = place.get("closed_days")
    if not cd:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="NO_CLOSED_DAY_DATA",
                     detail=place.get("missing_note", "휴무일 정보를 확보하지 못했다"),
                     how_to_resolve={"user": "방문할 점포를 알려주세요",
                                     "system": "시장 단위 휴무일 정의 가능 여부 판단"})

    if not evidence_usable_on(cd, visit_date):
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"확보한 근거의 적용 기간(~{cd.get('valid_until')})을 벗어난 방문일이다")

    holiday_dependent = cd.get("exception_rule") not in (None, "none")
    if holiday_dependent and not facts["holidays"]["verified"]:
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="HOLIDAY_CALENDAR_UNVERIFIED",
                     detail="휴무일 예외 규칙이 공휴일 캘린더에 의존하는데, 그 값이 아직 검증되지 않았다",
                     how_to_resolve={"system": "공공데이터포털 특일 정보 API 호출"})
    if holiday_dependent and not evidence_usable_on(facts["holidays"], visit_date):
        return check("CLOSED_DAY", name, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"공휴일 캘린더의 적용 기간(~{facts['holidays'].get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "방문 연도의 공휴일 캘린더를 다시 조회"})

    closed = closed_date_for(visit_date, cd, facts["holidays"])
    evidence = {
        "weekly": cd["weekly"],
        "fixed_dates": cd.get("fixed_dates"),
        "exception_rule": cd.get("exception_rule"),
        "effective_closed_date": closed,
        "source": cd.get("source"), "url": cd.get("url"),
        "checked_at": cd.get("checked_at"), "valid_until": cd.get("valid_until"),
    }

    # ⭐ 고정일 휴관(미술관 1월 1일)은 요일 규칙과 따로 본다.
    #    다만 그날이 정기휴일 요일이면서 공휴일이면 공식 문구 두 개가 반대 결론을 낸다.
    #    2029-01-01(월) — "휴관 1월 1일" 이면 휴관, "월요일이 공휴일인 경우 정상 개관" 이면 개관.
    #    페이지에 우선순위가 없다. 어느 쪽으로 읽든 해석이지 확인한 사실이 아니므로
    #    그날은 확정하지 않는다.
    if visit_date[5:] in (cd.get("fixed_dates") or []):
        on_weekly = WEEKDAYS[Date.fromisoformat(visit_date).weekday()] in cd["weekly"]
        if holiday_dependent and on_weekly and visit_date in facts["holidays"]["dates"]:
            return check("CLOSED_DAY", name, UNKNOWN,
                         unknown_reason="RULE_CONFLICT",
                         detail=f"{visit_date}은 고정 휴관일이면서 공휴일인 정기휴일이다. "
                                f"공식 문구 두 개가 휴관과 개관으로 결론이 갈린다",
                         how_to_resolve={"system": "기관에 두 규칙의 우선순위 확인"},
                         reference=evidence)
        return check("CLOSED_DAY", name, FAIL, evidence=evidence,
                     detail=f"{visit_date}은 고정 휴관일({visit_date[5:]})이다")

    if closed == visit_date:
        return check("CLOSED_DAY", name, FAIL, evidence=evidence,
                     detail=f"{visit_date}은 휴무일이다")
    return check("CLOSED_DAY", name, PASS, evidence=evidence)


# ── 운영시간 ────────────────────────────────────────────────────────
def hours_for(place: dict | None, visit_date: str, holidays: dict) -> dict | None:
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


def check_admission(name, place, visit_date, start, closed_status, holidays) -> dict:
    if closed_status == FAIL:
        return check("ADMISSION_NOT_POSSIBLE", name, NA,
                     reason="휴무일로 확정되어 운영시간 검사가 성립하지 않는다")

    # 휴무일 검사에서 장소를 특정하지 못했다면 운영시간이 없는 것이 아니라
    # 운영시간을 확인할 대상 자체가 없는 상태다. 두 경우를 구분해야 사용자가
    # 장소를 알려줘야 하는지, 시스템이 운영시간 데이터를 더 가져와야 하는지
    # 정확히 안내할 수 있다.
    if place is None:
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="PLACE_NOT_RESOLVED",
                     detail="구역만 입력되어 방문할 시설이나 점포를 특정하지 못했다",
                     how_to_resolve={"user": "방문할 시설이나 점포를 알려주세요"})

    adm = (place or {}).get("admission") or {}

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

    # ⭐ 같은 unknown 이라도 이유가 다르면 따로 적는다.
    #      NO_OPERATING_HOURS_DATA     정보를 못 구했다        → 데이터를 확보하면 해결
    #      UNSUPPORTED_ADMISSION_TYPE  정보는 구했는데 검사가 없다 → 코드를 고쳐야 해결
    #    종묘 평일은 회차 입장(09:20, 10:20 … 16:20)이라 연속 구간으로 못 적는다.
    #    연속 구간처럼 적으면 16:20 과 16:40 사이 도착을 통과시켜 버린다.
    if adm.get("type") == "timed_entry":
        general_day = (visit_date in holidays["dates"]) or is_last_wednesday(visit_date)
        if weekday_of(visit_date) in adm.get("timed_weekdays", []) and not general_day:
            return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                         unknown_reason="UNSUPPORTED_ADMISSION_TYPE",
                         detail=adm.get("unsupported_note",
                                        "회차 입장 검사가 구현되지 않았다"),
                         evidence={"admission_type": "timed_entry",
                                   "entry_times": adm.get("entry_times_ko"),
                                   "dwell_fixed_minutes": adm.get("dwell_fixed_minutes"),
                                   "rule_text": adm.get("rule_text"),
                                   "source": adm.get("source"), "url": adm.get("url"),
                                   "checked_at": adm.get("checked_at")},
                         how_to_resolve={"system": "회차 입장 검사 구현 — 도착 시각이 어느 회차에 "
                                                   "들어가는지와 고정 체류 50분을 함께 본다"})

    h = hours_for(place, visit_date, holidays)
    if h is None:
        return check("ADMISSION_NOT_POSSIBLE", name, UNKNOWN,
                     unknown_reason="NO_OPERATING_HOURS_DATA",
                     detail=(place or {}).get("missing_note", "해당 날짜의 운영시간을 확보하지 못했다"))
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

    # ⭐ 이 프로젝트가 비교 실험에서 이긴 지점이다 (docs 및 last-compare.json 참조).
    #    "16:30 에 경복궁" — 입장마감 17:00 전이라 들어갈 수는 있다. 그런데 몇 시간
    #    볼 건지 사용자가 말하지 않았다. 기본값 90분을 넣으면 18:00 폐장을 넘는다.
    #    우리가 정한 값으로 사용자 일정의 오류를 만들어내면 안 되므로 판단하지 않는다.
    #    같은 데이터를 준 LLM 은 이 규칙을 문서로 받고도 feasible 을 냈다 (T08).
    if policy["require_user_dwell_for_completion_check"] and dwell["source"] != "user_stated":
        return check("DWELL_NOT_COMPLETABLE", name, UNKNOWN,
                     unknown_reason="NO_USER_DWELL",
                     detail="체류시간을 사용자가 말하지 않았다. 기본값으로 메워서 판정하지 않는다",
                     how_to_resolve={"user": f"{name}에서 얼마나 머무르실 예정인가요?"},
                     depends_on_assumptions=[dwell.get("assumption_id")])

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


# ── 이동시간 ────────────────────────────────────────────────────────
def leg_between(facts: dict, a: str, b: str) -> dict | None:
    return facts["legs"].get(f"{a}|{b}")


MODE_NAMES = {"metro": "지하철"}
EVENT_NAMES = {"TRAIN_DEPARTURE": "열차 출발", "FLIGHT_DEPARTURE": "항공편 출발",
               "ARRIVE_BY": "도착 기한", "OTHER": "필수 조건"}


def check_travel(frm, to, facts, policy, closed_statuses, visit_date, travel_mode=None) -> dict:
    label = f"{frm['name']} → {to['name']}"
    if closed_statuses.get(frm["name"]) == FAIL or closed_statuses.get(to["name"]) == FAIL:
        return check("INSUFFICIENT_TRAVEL_TIME", label, NA,
                     reason="양 끝 중 한 곳이 휴무일이라 구간이 성립하지 않는다")

    # 다음 장소에 도착해야 하는 시각은 실제로 들어갈 수 있는 시각이다. 09:45 로 계획했어도
    # 10:00 에 열면 그 전에는 어차피 기다린다(check_admission). 10:00 까지만 가면 된다.
    target = to_min(to["entry"])
    by = to["entry"] if to["entry"] == to["start"] else f"{to['entry']}(개장)"
    stated_dwell = frm["dwell"]["source"] == "user_stated"

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

    leg = leg_between(facts, frm["name"], to["name"])
    if leg is None:
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="UNVERIFIED_TRAVEL_TIME",
                     detail="이 구간 이동시간을 확보하지 못했다",
                     how_to_resolve={"system": "도보 구간이면 TMAP, 대중교통이면 Routes API 호출"})

    if not evidence_usable_on(leg, visit_date):
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="EVIDENCE_EXPIRED",
                     detail=f"이동시간 근거의 적용 기간(~{leg.get('valid_until')})을 벗어난 방문일이다",
                     how_to_resolve={"system": "해당 구간 이동시간을 다시 조회"})

    # ⭐ 근사값으로 pass 를 내지 않는다.
    #    직선거리로 "3분이면 가니까 여유 10분은 충분" 은 실제 보행 경로·출입구·
    #    우회를 반영하지 않은 계산이다. 참고 안내로만 쓰고 판정은 보류한다.
    if leg.get("method") == "haversine_estimate":
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="ESTIMATE_ONLY",
                     detail="직선거리 기반 근사값뿐이다. 실제 보행 경로·출입구·우회가 반영되지 않아 판정을 보류한다",
                     reference={"travel_minutes": leg["minutes"]})

    leg_mode = leg.get("mode")
    mode_name = MODE_NAMES.get(leg_mode, leg_mode or "이동")
    if travel_mode and travel_mode != leg_mode:
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="UNVERIFIED_TRAVEL_TIME",
                     detail=f"{MODE_NAMES.get(travel_mode, travel_mode)} 이동시간을 확보하지 못했다. "
                            f"확보한 것은 {mode_name} 주행시간뿐이다",
                     how_to_resolve={"system": f"{MODE_NAMES.get(travel_mode, travel_mode)} 이동시간 확보"})

    buffer = policy["buffer_minutes"]["transit_leg"]
    base = frm["dwell"]["value"]

    def arrival_at(dwell_minutes: int) -> float:
        return to_min(frm["entry"]) + dwell_minutes + leg["minutes"]

    def ok_at(dwell_minutes: int) -> bool:
        return arrival_at(dwell_minutes) + buffer <= target

    arrival = arrival_at(base)
    evidence = {
        "depart_at": to_hhmm(to_min(frm["entry"]) + base),
        "travel_minutes": leg["minutes"], "buffer_minutes": buffer,
        "arrival": to_hhmm(arrival),
        "planned_arrival": to["start"], "arrive_by": to["entry"],
        "mode": leg_mode, "stated_mode": travel_mode,
        "method": leg.get("method"), "source": leg.get("source"),
        "snapshot_id": leg.get("snapshot_id"), "checked_at": leg.get("checked_at"),
        "is_lower_bound": leg.get("is_lower_bound", False),
        "excluded_from_travel_time": leg.get("excluded"),
    }

    # ⭐ 이동수단을 말하지 않았으면 fail 을 내지 않는다 (체류 하한과 같은 원칙).
    #    지하철로 늦는다는 것은 "지하철로는 못 간다" 이지 "못 간다" 가 아니다. 택시로는
    #    갈 수 있을 수도 있다. "대중교통 기본" 은 우리가 정한 기본값이므로, 사용자가
    #    그 수단으로 간다고 말했을 때만 fail 로 확정한다. 항공 최소 환승시간(MCT)도
    #    수단이 정해진 연결에만 "불가능" 을 적용하고, TripIt 은 수단을 모를 때 위험 경고만 한다.
    #
    #    pass 쪽은 수단을 몰라도 된다.
    #      fail  사용자가 고른 수단으로 못 간다   → 그 수단을 알아야 한다
    #      pass  갈 수 있는 수단이 하나는 있다    → 우리가 가진 수단으로 충분하다
    def late(detail: str) -> dict:
        if travel_mode is not None and travel_mode == leg_mode:
            if leg.get("is_lower_bound"):
                detail += " — 주행시간 하한선만으로도 늦으므로 확정된다"
            return check("INSUFFICIENT_TRAVEL_TIME", label, FAIL, evidence=evidence, detail=detail)
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="NO_USER_MODE",
                     detail=f"{mode_name}로 가면 {detail}. "
                            f"이동수단을 말하지 않아 다른 수단으로 갈 수 있는지는 확인하지 않았다",
                     how_to_resolve={"user": f"{to['name']}까지 어떤 수단으로 이동하시나요?"},
                     reference=evidence)

    # ⭐ 사용자가 말하지 않은 체류시간으로는 판정하지 않는다 (T08 과 같은 원칙).
    #    fail 은 "어떻게 머물든 늦는다" 의 증명이어야 한다. 체류시간은 음수가 될 수
    #    없으므로 0분이 보장된 하한이다. 들어가자마자 나와도 늦을 때만 fail 이다.
    #    얼마나 오래 머물지는 모르므로 pass 도 낼 수 없다.
    #
    #    전에는 기본값을 ±50% 흔들어 둘 다 늦으면 fail 을 냈다. 하지만 궁궐 90분의
    #    45~135분은 우리 추측을 중심으로 한 범위일 뿐이다. 20분만 둘러보는 사람도
    #    있다. 기본값은 사용자에게 물어볼 때 "늦을 수 있다" 는 안내로만 쓴다.
    if not stated_dwell:
        earliest = arrival_at(0)
        if earliest > target:
            evidence.update(depart_at=frm["entry"], arrival=to_hhmm(earliest))
            return late(f"{frm['name']}에서 바로 나와도 {to_hhmm(earliest)} 도착이라 {by}에 늦는다")

        detail = f"{frm['name']} 체류시간을 몰라 확정할 수 없다"
        if arrival > target:
            detail += f". 기본값 {base}분으로 보면 {to_hhmm(arrival)} 도착이라 늦을 수 있다"
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="NO_USER_DWELL",
                     detail=detail,
                     depends_on_assumptions=[frm["dwell"].get("assumption_id")],
                     how_to_resolve={"user": f"{frm['name']}에서 몇 시에 나오실 예정인가요?"},
                     reference=evidence)

    # ⭐ 하한선의 비대칭 — 한 방향으로만 확정할 수 있다.
    #    지금 이동시간은 지하철 주행시간뿐이고 도보·환승·대기가 빠져 있다.
    #    빠진 몫이 얼마인지는 모른다. 그래도 부호는 안다 — 언제나 0 보다 크다.
    #
    #      하한선으로도 늦는다  → fail 확정   빠진 값을 더하면 더 늦어질 뿐이다
    #      하한선으로는 도착한다 → unknown    나머지를 모르니 확정할 수 없다
    #
    #    이게 "확보한 만큼만 말한다" 를 산수로 옮긴 것이다. 단 이 하한선은 지하철로
    #    갈 때의 하한선이라, fail 확정은 사용자가 지하철로 간다고 말했을 때만 한다 (late).
    #
    # ⭐ 권장 여유는 fail 을 가르는 데 쓰지 않는다.
    #    "10분 여유를 두자" 는 우리가 정한 설정이지 사실이 아니다. 권장 여유까지 더해서
    #    늦는다고 fail 을 내면 설정값을 올리기만 해도 가능한 일정이 불가능이 된다.
    #    14:00 출발 · 14:15 시작 · 주행 6.5분이면 14:06 에 닿는다. 모자란 것은
    #    권장 여유이지 이동 자체가 아니다. 열차 조건과 같은 규칙이다.
    #
    #      이동만으로 늦는다              → fail
    #      닿지만 권장 여유를 못 채운다   → unknown (BUFFER_NOT_MET)  사용자가 판단한다
    #      권장 여유까지 채운다           → pass, 하한선이면 unknown (LOWER_BOUND_ONLY)
    if arrival > target:
        return late(f"{to_hhmm(arrival)} 도착 예상인데 {by}까지 가야 한다")

    if not ok_at(base):
        spare = round(target - arrival)
        detail = (f"{by}보다 {spare}분 먼저 도착하지만, "
                  f"설정한 권장 여유 {buffer}분보다 {round(arrival + buffer - target)}분 부족하다")
        if leg.get("is_lower_bound"):
            detail += ". 주행시간 하한선 기준이라 실제 도착은 이보다 늦다"
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="BUFFER_NOT_MET",
                     detail=detail,
                     how_to_resolve={"user": f"{to['name']} 시작 전 여유가 {spare}분뿐인데 괜찮으신가요?"},
                     reference=evidence)

    if leg.get("is_lower_bound"):
        missing = ", ".join(leg.get("excluded") or ["미확인 구성요소"])
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="LOWER_BOUND_ONLY",
                     detail=f"{mode_name} 주행시간({leg['minutes']}분)만으로는 "
                            f"도착하지만 {missing}이 빠져 있어 확정할 수 없다",
                     how_to_resolve={"system": "역 출입구 보행 경로·환승 이동시간·배차간격 확보"},
                     reference=evidence)
    return check("INSUFFICIENT_TRAVEL_TIME", label, PASS, evidence=evidence)


# ── 필수 조건 (KTX 등) ──────────────────────────────────────────────
def evaluate_hard_constraint(hc, stops, facts, policy, visit_date, travel_mode=None) -> dict:
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
    result = {"id": hc["id"], "event": event}

    last = stops[-1]
    leg = leg_between(facts, last["name"], hc["place"])
    buffer = policy["buffer_minutes"].get(hc.get("buffer_rule", "rail_boarding"))
    required = to_min(hc["time"]) - buffer

    policy_eval = {
        "buffer_minutes": buffer,
        "buffer_source": "system_default",
        "required_arrival": to_hhmm(required),
        # ⭐ status 와 severity 는 서로 다른 질문이다. 섞으면 위험한 문제가 숨는다.
        #      status   지금 정보로 확정할 수 있나   pass / fail / unknown / n.a.
        #      severity 실패하면 타격이 큰가        blocking / warning
        #    KTX 를 놓치는 건 정보가 부족해도 여전히 치명적이다. 그래서
        #    unknown 이라고 severity 를 낮추지 않는다. 낮추면 "중요도 낮음" 으로
        #    보여서 사용자가 확인을 건너뛴다.
        "severity": "blocking",
    }

    event_time = to_min(hc["time"])
    stated_dwell = last["dwell"]["source"] == "user_stated"

    # 필수 조건마다 시각의 뜻이 다르다. 열차·항공편은 출발 시각이라 그 시각에 도착하면
    # 이미 놓친 것이다. "20:30 까지 도착" 은 20:30 에 닿으면 제시간이다.
    departs = hc["type"] in ("TRAIN_DEPARTURE", "FLIGHT_DEPARTURE")
    ev = f"{EVENT_NAMES.get(hc['type'], '필수 조건')}({hc['time']})"
    ok_arrival = "실제 탑승 가능 여부" if departs else "실제로 제시간에 닿는지"

    def misses(arrival_min: float) -> bool:
        return arrival_min >= event_time if departs else arrival_min > event_time

    # check_travel 과 같다. 이동시간이 0분이어도 늦으면 수단과 상관없이 fail 이다.
    earliest_departure = to_min(last["entry"]) + (last["dwell"]["value"] if stated_dwell else 0)
    if misses(earliest_departure):
        policy_eval.update(
            status=FAIL,
            detail=f"{last['name']}에서 빨라도 {to_hhmm(earliest_departure)}에 나오는데 "
                   f"{ev}에 늦는다 — 이동시간이 0분이어도 늦는다",
        )
        result["policy"] = policy_eval
        return result

    if leg is None:
        policy_eval.update(status=UNKNOWN, unknown_reason="UNVERIFIED_TRAVEL_TIME",
                           detail=f"{last['name']} → {hc['place']} 이동시간을 확보하지 못했다")
        result["policy"] = policy_eval
        return result

    if not evidence_usable_on(leg, visit_date):
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="EVIDENCE_EXPIRED",
            detail=f"이동시간 근거의 적용 기간(~{leg.get('valid_until')})을 벗어난 방문일이다",
            how_to_resolve={"system": "해당 구간 이동시간을 다시 조회"},
        )
        result["policy"] = policy_eval
        return result

    leg_mode = leg.get("mode")
    mode_name = MODE_NAMES.get(leg_mode, leg_mode or "이동")
    if travel_mode and travel_mode != leg_mode:
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="UNVERIFIED_TRAVEL_TIME",
            detail=f"{MODE_NAMES.get(travel_mode, travel_mode)} 이동시간을 확보하지 못했다. "
                   f"확보한 것은 {mode_name} 주행시간뿐이다",
            how_to_resolve={"system": f"{MODE_NAMES.get(travel_mode, travel_mode)} 이동시간 확보"},
        )
        result["policy"] = policy_eval
        return result

    base = last["dwell"]["value"]

    def arrival_at(dwell_minutes: int) -> int:
        return to_min(last["entry"]) + dwell_minutes + leg["minutes"]

    est = arrival_at(base)
    policy_eval.update(
        estimated_arrival=to_hhmm(est),
        travel_minutes=leg["minutes"],
        travel_source=leg.get("source"), travel_snapshot=leg.get("snapshot_id"),
        margin_vs_event=round(event_time - est),
        margin_vs_required=round(required - est),
        is_lower_bound=leg.get("is_lower_bound", False),
        stated_mode=travel_mode,
    )

    # 이동수단을 말하지 않았으면 fail 을 내지 않는다 (check_travel 의 late 와 같은 규칙).
    # severity 는 blocking 그대로다. 열차를 놓칠 수 있다는 경고가 사라지면 안 된다.
    def late(detail: str) -> dict:
        if travel_mode is not None and travel_mode == leg_mode:
            if leg.get("is_lower_bound"):
                detail += " — 주행시간 하한선만으로도 늦으므로 확정된다"
            policy_eval.update(status=FAIL, detail=detail)
        else:
            policy_eval.update(
                status=UNKNOWN,
                unknown_reason="NO_USER_MODE",
                detail=f"{mode_name}로 가면 {detail}. "
                       f"이동수단을 말하지 않아 다른 수단으로 갈 수 있는지는 확인하지 않았다",
                how_to_resolve={"user": f"{hc['place']}까지 어떤 수단으로 이동하시나요?"},
                not_confirmed="다른 이동수단으로 갔을 때의 도착 시각",
            )
        result["policy"] = policy_eval
        return result

    # 체류시간을 사용자가 말하지 않았으면 check_travel 과 같은 규칙이다.
    # 0분으로도 늦을 때만 fail 이고, 그 밖에는 확정하지 않는다. 기본값은 안내에만 쓴다.
    if not stated_dwell:
        earliest = arrival_at(0)
        if misses(earliest):
            policy_eval.update(
                estimated_arrival=to_hhmm(earliest),
                margin_vs_event=round(event_time - earliest),
                margin_vs_required=round(required - earliest),
            )
            return late(f"{last['name']}에서 바로 나와도 {to_hhmm(earliest)} 도착이라 {ev}에 늦는다")

        detail = f"{last['name']} 체류시간을 몰라 확정할 수 없다"
        if misses(est):
            detail += f". 기본값 {base}분으로 보면 {to_hhmm(est)} 도착이라 {ev}에 늦을 수 있다"
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="NO_USER_DWELL",
            detail=detail,
            depends_on_assumptions=[last["dwell"].get("assumption_id")],
            how_to_resolve={"user": f"{last['name']}에서 몇 시에 나오실 예정인가요?"},
            confirmed=f"바로 나와도 {to_hhmm(earliest)} 이후에 도착한다",
            not_confirmed=f"{last['name']} 체류시간과 {ok_arrival}",
        )
        result["policy"] = policy_eval
        return result

    # 사용자가 반드시 지켜야 하는 것은 열차 출발 시각이다. 서비스가 정한
    # 15분 전 도착은 안전을 위한 권장 기준이지, 그 자체로 사용자 일정의
    # 확정 위반은 아니다.
    if misses(est):
        return late(f"예상 도착({to_hhmm(est)})이 {ev}보다 늦다")
    if est > required:
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="BUFFER_NOT_MET",
            detail=(
                f"{ev} 전 {round(event_time - est)}분 도착 예상이지만, "
                f"설정한 권장 여유 {buffer}분보다 {round(est - required)}분 부족하다"
            ),
            confirmed=f"현재 이동시간 기준으로는 {ev} 전 {round(event_time - est)}분 도착한다",
            not_confirmed=f"역 출입구 이동·탑승 절차를 포함한 {ok_arrival}",
            how_to_resolve={"system": "역 출입구 보행 경로·탑승 절차·대기시간 확보"},
        )
    elif leg.get("is_lower_bound"):
        # 중요한 열차 일정인데 정보가 부족하다면 중요도가 낮은 문제가 아니라
        # 추가 확인이 필요한 중요한 문제다. severity 는 blocking 으로 둔다.
        missing = ", ".join(leg.get("excluded") or ["미확인 구성요소"])
        policy_eval.update(
            status=UNKNOWN,
            unknown_reason="LOWER_BOUND_ONLY",
            detail=f"주행시간 하한선({leg['minutes']}분)으로는 권장 도착 시각에 맞지만 "
                   f"{missing}이 빠져 있다",
            confirmed=f"주행시간만으로는 {round(required - est)}분 여유가 있다",
            not_confirmed=ok_arrival,
            how_to_resolve={"system": "역 출입구 보행 경로·환승 이동시간·배차간격 확보"},
        )
    else:
        policy_eval["status"] = PASS
    result["policy"] = policy_eval
    return result


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
    # ⭐ 입력이 비어 있을 때 시간 계산 예외를 내지 않는다.
    #    날짜·시각을 모르는 것은 "검사할 필요가 없다"가 아니라
    #    필요한 검사를 끝내지 못한 상태이므로 unknown 으로 남긴다.
    missing_inputs = input_checks(itinerary)
    if missing_inputs:
        for n, c in enumerate(missing_inputs, 1):
            c["id"] = f"chk-{n:03d}"
        counts = {s: sum(c["status"] == s for c in missing_inputs)
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
            "checks": missing_inputs,
            "hard_constraints": [],
        }

    visit_date = itinerary["date"]
    assumptions: list[dict] = []

    # 자연어에서 날짜나 시작 시각을 추정한 경우, 사용자 입력과 구분해 기록한다.
    if itinerary.get("date_source") == "inferred":
        assumptions.append({
            "id": "A1",
            "field": "date",
            "value": visit_date,
            "source": "llm_inferred",
            "rule": "extract.inferred_date",
            "reason": "입력에 연도가 없어 추출 단계에서 가장 가까운 미래 날짜로 해석했다",
            "ask_user": "방문 날짜가 맞는지 확인해 주세요",
        })

    # 날짜와 요일이 맞지 않으면 사용자가 어느 날을 뜻했는지 모른다. "10월 8일 수요일" 은
    # 7일 수요일일 수도, 8일 목요일일 수도 있고, 어느 날이든 일정 자체는 문제없을 수 있다.
    # 불가능이 아니라 판정 보류다. 어느 쪽이 맞는지 묻는다.
    stated_weekday = itinerary.get("weekday_stated")
    if stated_weekday and stated_weekday != weekday_of(visit_date):
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

    # ⭐ LLM 이 추정한 입력에 기대는 판정은 pass 든 fail 이든 확정하지 않는다.
    #    "점심 먹고" → 12:00 은 LLM 의 짐작이다. 그 12:00 으로 "입장 불가" 를 내면
    #    추정치로 불가능을 단정한 것이고, "입장 가능" 을 내도 가능을 단정한 것이다.
    #    기본 체류시간과 같은 원칙이다. 결과는 참고로 남기고 추정한 값이 맞는지 묻는다.
    #
    #    연도는 다르다. "10월 8일 목요일" 처럼 요일을 말했고 그 요일이 맞으면(틀리면
    #    위에서 INPUT_CONFLICT) 날짜는 사용자 말로 확인된 것이다. 요일 없이 연도를
    #    채웠을 때만 추정으로 본다.
    date_dep = [] if stated_weekday else [a for a in assumptions if a["field"] == "date"]

    # 체류시간: 사용자가 말했으면 그대로, 아니면 기본값을 쓰되 반드시 기록한다.
    stops = []
    for i, s in enumerate(itinerary["stops"]):
        # ⭐ area 일정은 대표 좌표나 비슷한 이름의 시설로 바꿔 판정하지 않는다.
        #    "북촌에서 점심"을 임의의 식당 운영시간으로 검사하면 다른 장소를
        #    검증한 셈이 된다. 사용자가 장소를 특정하기 전까지는 unknown 이다.
        scope = s.get("scope", "place")
        place = None if scope == "area" else facts["places"].get(s["place"])

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
        stops.append({"name": s["place"], "place": place, "start": s["start"], "entry": s["start"],
                      "start_source": start_source, "start_dep": start_dep, "scope": scope,
                      "scope_note": s.get("scope_note"), "dwell": dwell})

    checks: list[dict] = []
    closed_statuses: dict[str, str] = {}

    # 추정 입력 처리는 검사 하나가 끝날 때마다 바로 한다. 휴무 결과를 입장 검사가,
    # 입장 결과를 체류 검사가 이어받기 때문에, 다 끝낸 뒤에 고치면 앞뒤가 어긋난다.
    for st in stops:
        c = unconfirmed(check_closed_day(st["name"], st["place"], visit_date, facts), date_dep)
        closed_statuses[st["name"]] = c["status"]
        a = unconfirmed(check_admission(st["name"], st["place"], visit_date, st["start"],
                                        c["status"], facts["holidays"]),
                        date_dep + st["start_dep"])
        # 개장 전에 도착하면 개장 시각에 들어간다. 체류와 다음 이동은 거기서부터 센다.
        st["entry"] = (a.get("evidence") or a.get("reference") or {}).get("entry_at", st["start"])
        d = unconfirmed(check_dwell(st["name"], st["place"], visit_date, st["entry"], st["dwell"],
                                    a["status"], policy, facts["holidays"]),
                        date_dep + st["start_dep"])
        checks += [c, a, d]

    for i, st in enumerate(stops):
        if i == len(stops) - 1:
            checks.append(check("INSUFFICIENT_TRAVEL_TIME", f"{st['name']} → (없음)", NA,
                                reason="마지막 스톱이라 다음 구간이 없다"))
        else:
            checks.append(unconfirmed(
                check_travel(st, stops[i + 1], facts, policy, closed_statuses,
                             visit_date, itinerary.get("travel_mode")),
                st["start_dep"] + stops[i + 1]["start_dep"]))

    for n, c in enumerate(checks, 1):
        c["id"] = f"chk-{n:03d}"

    hard = []
    for hc in itinerary.get("hard_constraints", []):
        h = evaluate_hard_constraint(hc, stops, facts, policy, visit_date,
                                     itinerary.get("travel_mode"))
        h["policy"] = unconfirmed(h["policy"], stops[-1]["start_dep"])
        hard.append(h)

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

    return {
        "date": visit_date,
        "date_source": itinerary.get("date_source", "explicit"),
        "weekday": weekday_of(visit_date),
        "weekday_stated": stated_weekday,
        "is_holiday": visit_date in facts["holidays"]["dates"],
        "summary": {"verdict": verdict, "message": message, "counts": counts},
        "assumptions": assumptions,
        "checks": checks,
        "hard_constraints": hard,
    }
