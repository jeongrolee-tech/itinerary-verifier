"""구조화 추출의 기준 날짜 · 형식 검증 · 채점을 가짜 Claude 로 본다."""

import copy
import re
from datetime import date

import pytest
from pydantic import ValidationError

from extract import Extraction, extract, to_itinerary
from helpers import NEW, SUITE, Claude
from labels import grade_extraction

EMPTY = {"date": None, "date_source": "missing", "weekday_stated": None,
         "stops": [], "hard_constraints": [], "avoid_taxi": False, "notes": []}


def test_reference_date_goes_into_the_message():
    """시스템 프롬프트가 그대로여야 캐시가 붙어 기준 날짜는 사용자 메시지에 넣는다."""
    claude = Claude(EMPTY)
    extract("10월 8일 목요일 오후 1시에 경복궁", claude, today="2026-09-22")
    assert claude.sent[0]["messages"][0]["content"].startswith("오늘은 2026-09-22 이다.")


def test_reference_date_is_required():
    """기준 날짜를 빼먹으면 실행한 날로 조용히 채우지 않고 바로 멈춘다."""
    with pytest.raises(TypeError):
        extract("10월 8일 목요일 오후 1시에 경복궁", Claude(EMPTY))


def test_expected_dates_resolve_from_the_suite_reference_date():
    """연도 없는 케이스의 기대 날짜는 기준 날짜 뒤 가장 가까운 그날이어야 한다."""
    ref = date.fromisoformat(SUITE["reference_date"])
    for case in SUITE["cases"]:
        if re.search(r"\d{4}년", case["raw"]):
            continue  # 연도를 말한 입력은 기준 날짜와 상관없다.
        want = date.fromisoformat(case["date"])
        assert ref < want <= ref.replace(year=ref.year + 1), case["id"]


# 형식 검증

VALID = {
    "date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [], "avoid_taxi": False,
    "stops": [{"raw": "오후 1시 경복궁 두 시간", "place": "경복궁", "start": "13:00", "start_source": "explicit",
               "dwell_minutes": 120, "scope": "place", "scope_note": None, "outside_only": False}],
    "hard_constraints": [{"raw": "20:30 서울역 KTX", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30",
                          "time_source": "explicit"}]}


def changed(path, value):
    d = copy.deepcopy(VALID)
    *head, last = path
    target = d
    for p in head:
        target = target[p]
    target[last] = value
    return d


def test_valid_extraction_reaches_the_judge_as_text():
    assert to_itinerary(Extraction.model_validate(VALID))["date"] == "2026-10-08"


@pytest.mark.parametrize("path, value", [
    (("date",), "2026-02-30"),  # 없는 날이다.
    (("date",), "10월 8일"),
    (("weekday_stated",), "목요일"),  # THU 처럼 옮겨야 한다.
    (("stops", 0, "start"), "1:00 PM"),
    (("stops", 0, "start"), "9:00"),  # 두 자리로 써야 한다.
    (("stops", 0, "start"), "24:00"),
    (("stops", 0, "dwell_minutes"), 0),
    (("hard_constraints", 0, "time"), "8:30pm"),
])
def test_malformed_values_are_rejected(path, value):
    """틀린 형식이 판정 코어로 가면 계산하다 멈추거나 엉뚱한 시각으로 판정한다."""
    with pytest.raises(ValidationError):
        Extraction.model_validate(changed(path, value))


@pytest.mark.parametrize("path, value", [
    (("date",), None),  # 날짜가 없는데 출처가 inferred 다.
    (("date_source",), "missing"),  # 날짜가 있는데 출처가 missing 이다.
    (("stops", 0, "start"), None),  # 시각이 없는데 출처가 explicit 이다.
    (("stops", 0, "start_source"), "missing"),  # 시각이 있는데 출처가 missing 이다.
    (("hard_constraints", 0, "time"), None),  # 필수 일정도 같다.
    (("hard_constraints", 0, "time_source"), "missing"),
])
def test_value_and_its_source_must_agree(path, value):
    """시각이 있는데 출처가 missing 이면 판정 코어가 그 시각을 사용자가 말한 값처럼 쓴다."""
    with pytest.raises(ValidationError):
        Extraction.model_validate(changed(path, value))


def test_flight_gets_no_buffer_rule_from_policy():
    """정책에 flight_boarding 기준이 없어 판정이 항공편에 pass 를 내지 않는다."""
    ex = Extraction.model_validate(changed(("hard_constraints", 0, "type"), "FLIGHT_DEPARTURE"))
    assert to_itinerary(ex)["hard_constraints"][0]["buffer_rule"] == "flight_boarding"


# 기대 추출값 채점

def as_extracted(case, **change):
    """정답을 추출 결과 모양으로 옮긴다. 라벨에 출처가 없으면 말한 시각으로 본다."""
    got = {"date": case["date"], "date_source": case["date_source"], "weekday_stated": case.get("weekday_stated"),
           "avoid_taxi": case.get("avoid_taxi", False),
           "stops": [{**s, "start_source": s.get("start_source", "explicit" if s.get("start") else "missing"),
                      "scope": s.get("scope", "place")} for s in case["stops"]],
           "hard_constraints": [{**h, "time_source": h.get("time_source", "explicit" if h.get("time") else "missing")}
                                for h in case.get("hard_constraints", [])]}
    return {**got, **change}


def wrong(case, got, facts):
    return {k: msgs for k, msgs in grade_extraction(case, got, facts).items() if msgs}


@pytest.mark.parametrize("case", SUITE["cases"] + NEW["cases"], ids=lambda c: c["id"])
def test_extraction_equal_to_the_label_has_nothing_wrong(case, facts):
    """새 평가 문제의 정답 모양도 채점기와 맞는지 본다."""
    assert wrong(case, as_extracted(case), facts) == {}


def by_id(prefix):
    return next(c for c in NEW["cases"] if c["id"].startswith(prefix))


def test_guessing_an_unknown_train_time_is_wrong(facts):
    """N03 은 열차 시각을 모른다. 시각을 지어 넣으면 필수 일정이 틀린다."""
    case = by_id("N03")
    hc = {**case["hard_constraints"][0], "time": "20:00", "time_source": "inferred"}
    assert set(wrong(case, as_extracted(case, hard_constraints=[hc]), facts)) == {"hard_constraints"}


def test_train_time_source_is_graded(facts):
    """N04 의 8시 반은 앞에 기댈 시각이 없어 추정이다. 뒤 문장을 보고 말한 시각으로 옮기면 틀린다."""
    case = by_id("N04")
    hc = {**case["hard_constraints"][0], "time_source": "explicit"}
    assert set(wrong(case, as_extracted(case, hard_constraints=[hc]), facts)) == {"hard_constraints"}


def test_missing_the_no_taxi_wish_is_wrong(facts):
    case = by_id("N05")
    assert set(wrong(case, as_extracted(case, avoid_taxi=False), facts)) == {"avoid_taxi"}


def test_missing_the_outside_only_wish_is_wrong(facts):
    """밖에서만 머문다는 말을 놓치면 휴무 위반으로 불가능이 나온다."""
    case = by_id("N01")
    want = {**case, "stops": [{**case["stops"][0], "outside_only": True}, *case["stops"][1:]]}
    assert set(wrong(want, as_extracted(case), facts)) == {"outside_only"}
