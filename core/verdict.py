"""
판정 코어 — 순수 함수. 외부 API를 호출하지 않는다.

입력: 일정(itinerary) + 사실(facts) + 정책(policy)  — 전부 인자로 받는다
출력: 검사별 상태 + 필수 조건 평가 + 전체 판정

이 파일에 네트워크 호출이 들어가는 순간 고정 입력으로 테스트할 수 없게 된다.
"""

from __future__ import annotations

from datetime import date as Date, timedelta
from typing import Any

PASS, FAIL, UNKNOWN, NA = "pass", "fail", "unknown", "not_applicable"

# date.weekday() 는 월요일이 0
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


# ── 시간 유틸 (전부 '분' 단위 정수로 다룬다) ──────────────────────────
def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def weekday_of(iso: str) -> str:
    return WEEKDAYS[Date.fromisoformat(iso).weekday()]


def shift_date(iso: str, days: int) -> str:
    return (Date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def evidence_usable_on(ev: dict | None, visit_date: str) -> bool:
    """오늘 조회했다는 사실만으로 다음 달 방문일에 쓸 수 있는 정보가 되지는 않는다."""
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


# ── 휴무일 ──────────────────────────────────────────────────────────
def closed_date_for(visit_date: str, cd: dict, holidays: dict) -> str | None:
    """
    기관마다 규칙이 다르다. 코드에 박지 않고 facts 의 exception_rule 로 분기한다.
      shift_to_next_nonholiday : 정기휴일이 공휴일이면 개방하고, 다음 첫 비공휴일이 휴일이 된다 (궁궐)
      open_if_holiday_no_shift : 정기휴일이 공휴일이면 개방하고, 밀지 않는다 (미술관)
    """
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

    closed = closed_date_for(visit_date, cd, facts["holidays"])
    evidence = {
        "weekly": cd["weekly"],
        "exception_rule": cd.get("exception_rule"),
        "effective_closed_date": closed,
        "source": cd.get("source"), "url": cd.get("url"),
        "checked_at": cd.get("checked_at"), "valid_until": cd.get("valid_until"),
    }
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

    # 공휴일 전용 시간표가 있으면 요일보다 우선한다.
    # 공휴일이 평일이어도 주말·공휴일 시간표를 따르는 기관이 있다.
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
        "planned_time": start,
        "source": h.get("source"), "url": h.get("url"),
        "checked_at": h.get("checked_at"), "valid_until": h.get("valid_until"),
    }
    if to_min(start) < to_min(h["open"]):
        return check("ADMISSION_NOT_POSSIBLE", name, FAIL, evidence=evidence,
                     detail=f"개장({h['open']}) 전 도착")
    if to_min(start) > to_min(limit):
        return check("ADMISSION_NOT_POSSIBLE", name, FAIL, evidence=evidence,
                     detail=f"입장마감({limit}) 이후 도착")
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


def check_travel(frm, to, facts, policy, closed_statuses) -> dict:
    label = f"{frm['name']} → {to['name']}"
    if closed_statuses.get(frm["name"]) == FAIL or closed_statuses.get(to["name"]) == FAIL:
        return check("INSUFFICIENT_TRAVEL_TIME", label, NA,
                     reason="양 끝 중 한 곳이 휴무일이라 구간이 성립하지 않는다")

    leg = leg_between(facts, frm["name"], to["name"])
    if leg is None:
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="UNVERIFIED_TRAVEL_TIME",
                     detail="이 구간 이동시간을 확보하지 못했다",
                     how_to_resolve={"system": "도보 구간이면 TMAP, 대중교통이면 Routes API 호출"})

    # 근사값은 참고용이다. 근사값이 여유시간보다 짧다는 이유만으로 통과시키지 않는다.
    if leg.get("method") == "haversine_estimate":
        return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                     unknown_reason="ESTIMATE_ONLY",
                     detail="직선거리 기반 근사값뿐이다. 실제 보행 경로·출입구·우회가 반영되지 않아 판정을 보류한다",
                     reference={"travel_minutes": leg["minutes"]})

    buffer = policy["buffer_minutes"]["transit_leg"]
    target = to_min(to["start"])
    base = frm["dwell"]["value"]

    def ok_at(dwell_minutes: int) -> bool:
        return to_min(frm["start"]) + dwell_minutes + leg["minutes"] + buffer <= target

    evidence = {
        "depart_at": to_hhmm(to_min(frm["start"]) + base),
        "travel_minutes": leg["minutes"], "buffer_minutes": buffer,
        "arrival": to_hhmm(to_min(frm["start"]) + base + leg["minutes"] + buffer),
        "planned_arrival": to["start"],
        "mode": leg.get("mode"), "method": leg.get("method"), "source": leg.get("source"),
        "snapshot_id": leg.get("snapshot_id"), "checked_at": leg.get("checked_at"),
    }

    # 기본값으로 계산한 결과를 사용자 일정의 확정적인 오류처럼 보여주면 안 된다.
    # 가정을 흔들어서 판정이 뒤집히면 확정하지 않는다.
    if frm["dwell"]["source"] != "user_stated":
        r = policy["dwell_uncertainty_ratio"]
        if ok_at(round(base * (1 - r))) != ok_at(round(base * (1 + r))):
            return check("INSUFFICIENT_TRAVEL_TIME", label, UNKNOWN,
                         unknown_reason="DEPENDS_ON_DEFAULT_DWELL",
                         detail=f"출발 시각이 기본값 체류시간({base}분)에 의존한다. "
                                f"±{int(r * 100)}% 범위에서 판정이 뒤집혀 확정할 수 없다",
                         depends_on_assumptions=[frm["dwell"].get("assumption_id")],
                         how_to_resolve={"user": f"{frm['name']}에서 몇 시에 나오실 예정인가요?"},
                         reference=evidence)
        evidence["depends_on_assumptions"] = [frm["dwell"].get("assumption_id")]

    if ok_at(base):
        return check("INSUFFICIENT_TRAVEL_TIME", label, PASS, evidence=evidence)
    return check("INSUFFICIENT_TRAVEL_TIME", label, FAIL, evidence=evidence,
                 detail=f"{evidence['arrival']} 도착 예상인데 계획은 {to['start']}이다")


# ── 필수 조건 (KTX 등) ──────────────────────────────────────────────
def evaluate_hard_constraint(hc, stops, facts, policy) -> dict:
    """
    사용자가 말한 것과 서비스가 정한 권장 기준을 섞지 않는다.

      - 열차 출발 시각      : 사용자가 말한 사실. 그대로 보존한다
      - 권장 도착 여유      : 우리가 정한 정책
      - 예상 도착 시각      : 계산값

    "설정한 여유가 N분 부족하다"까지가 확인되는 것이다. 그것만으로 실제 탑승
    불가능을 확정할 수 없고, 출발 전에 도착한다는 이유만으로 탑승 가능을
    보장할 수도 없다.
    """
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
        # 실패 시 영향이 큰 문제인지와, 현재 정보로 확정할 수 있는지는 별개다.
        # 정보가 부족하다고 해서 severity 를 낮추지 않는다.
        "severity": "blocking",
    }

    if leg is None:
        policy_eval.update(status=UNKNOWN, unknown_reason="UNVERIFIED_TRAVEL_TIME",
                           detail=f"{last['name']} → {hc['place']} 이동시간을 확보하지 못했다")
        result["policy"] = policy_eval
        return result

    base = last["dwell"]["value"]

    def arrival_at(dwell_minutes: int) -> int:
        return to_min(last["start"]) + dwell_minutes + leg["minutes"]

    est = arrival_at(base)
    policy_eval.update(
        estimated_arrival=to_hhmm(est),
        travel_minutes=leg["minutes"],
        travel_source=leg.get("source"), travel_snapshot=leg.get("snapshot_id"),
        margin_vs_event=to_min(hc["time"]) - est,
        margin_vs_required=required - est,
    )

    if last["dwell"]["source"] != "user_stated":
        r = policy["dwell_uncertainty_ratio"]
        lo = arrival_at(round(base * (1 - r))) <= required
        hi = arrival_at(round(base * (1 + r))) <= required
        if lo != hi:
            policy_eval.update(
                status=UNKNOWN,
                unknown_reason="DEPENDS_ON_DEFAULT_DWELL",
                detail=f"예상 도착이 기본값 체류시간({base}분)에 의존한다. "
                       f"±{int(r * 100)}% 범위에서 판정이 뒤집혀 확정할 수 없다",
                depends_on_assumptions=[last["dwell"].get("assumption_id")],
                how_to_resolve={"user": f"{last['name']}에서 몇 시에 나오실 예정인가요?"},
                confirmed=f"설정한 {buffer}분 여유 기준으로 {abs(required - est)}분 "
                          f"{'부족' if est > required else '여유'}하다",
                not_confirmed="실제 탑승 가능 여부",
            )
            result["policy"] = policy_eval
            return result
        policy_eval["depends_on_assumptions"] = [last["dwell"].get("assumption_id")]

    policy_eval["status"] = PASS if est <= required else FAIL
    if est > required:
        policy_eval["detail"] = f"설정한 {buffer}분 여유가 {est - required}분 부족하다"
        policy_eval["not_confirmed"] = "실제 탑승 불가능 여부. 열차 출발 전에는 도착한다" \
            if est <= to_min(hc["time"]) else None
    result["policy"] = policy_eval
    return result


# ── 본체 ────────────────────────────────────────────────────────────
def judge(itinerary: dict, facts: dict, policy: dict) -> dict:
    visit_date = itinerary["date"]
    assumptions: list[dict] = []

    # 체류시간: 사용자가 말했으면 그대로, 아니면 기본값을 쓰되 반드시 기록한다.
    stops = []
    for i, s in enumerate(itinerary["stops"]):
        place = facts["places"].get(s["place"])
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
        stops.append({"name": s["place"], "place": place, "start": s["start"], "dwell": dwell})

    checks: list[dict] = []
    closed_statuses: dict[str, str] = {}

    for st in stops:
        c = check_closed_day(st["name"], st["place"], visit_date, facts)
        closed_statuses[st["name"]] = c["status"]
        a = check_admission(st["name"], st["place"], visit_date, st["start"], c["status"],
                            facts["holidays"])
        d = check_dwell(st["name"], st["place"], visit_date, st["start"], st["dwell"],
                        a["status"], policy, facts["holidays"])
        checks += [c, a, d]

    for i, st in enumerate(stops):
        if i == len(stops) - 1:
            checks.append(check("INSUFFICIENT_TRAVEL_TIME", f"{st['name']} → (없음)", NA,
                                reason="마지막 스톱이라 다음 구간이 없다"))
        else:
            checks.append(check_travel(st, stops[i + 1], facts, policy, closed_statuses))

    for n, c in enumerate(checks, 1):
        c["id"] = f"chk-{n:03d}"

    hard = [evaluate_hard_constraint(hc, stops, facts, policy)
            for hc in itinerary.get("hard_constraints", [])]

    # 전체 판정: 확인된 위반이 있으면 위반, 위반은 없지만 미확인이 있으면 보류,
    # 필요한 검사가 모두 통과한 경우에만 통과.
    statuses = [c["status"] for c in checks] + [h["policy"]["status"] for h in hard]
    counts = {s: statuses.count(s) for s in (PASS, FAIL, UNKNOWN, NA)}
    verdict = ("infeasible" if counts[FAIL] else
               "undetermined" if counts[UNKNOWN] else "feasible")

    message = {
        "feasible": "확인한 정보와 검사 범위 내에서 위반이 발견되지 않았습니다",
        "infeasible": "확인한 정보에서 실행할 수 없는 부분이 발견되었습니다",
        "undetermined": f"위반은 발견되지 않았지만 {counts[UNKNOWN]}개 항목을 확인하지 못했습니다",
    }[verdict]

    return {
        "date": visit_date,
        "weekday": weekday_of(visit_date),
        "is_holiday": visit_date in facts["holidays"]["dates"],
        "summary": {"verdict": verdict, "message": message, "counts": counts},
        "assumptions": assumptions,
        "checks": checks,
        "hard_constraints": hard,
    }
