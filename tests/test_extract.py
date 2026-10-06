"""
추출 — 연도가 빠진 날짜를 푸는 기준 날짜 (2차 피드백 4번).

기준 날짜는 시스템 시계가 아니라 부르는 쪽이 넣는다. 평가에서는 tests.json 의 reference_date 를 쓴다.
Claude 는 가짜로 끼운다(test_verify.py 와 같은 방식). 진짜로 부르지 않으니 키도 요금도 들지 않는다.
"""

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest
from anthropic.types import Message
from pydantic import ValidationError

from extract import Extraction, extract, to_itinerary

SUITE = json.loads((Path(__file__).parent.parent / "core" / "tests.json").read_text(encoding="utf-8"))
EMPTY = {"date": None, "date_source": "missing", "weekday_stated": None,
         "stops": [], "hard_constraints": [], "notes": []}


class Claude:
    """messages.create 에 들어온 요청을 남기고, 빈 추출 결과를 SDK 의 Message 로 돌려준다."""

    def __init__(self):
        self.messages = self
        self.sent = []

    def create(self, **kw):
        self.sent.append(kw)
        return Message.model_validate({
            "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(EMPTY)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}})


def test_reference_date_goes_into_the_message():
    """기준 날짜는 사용자 메시지 맨 앞에 '오늘은 … 이다' 로 들어간다. 시스템 프롬프트는 그대로라 캐시가 붙는다."""
    claude = Claude()
    extract("10월 8일 목요일 오후 1시에 경복궁", claude, today="2026-09-22")
    assert claude.sent[0]["messages"][0]["content"].startswith("오늘은 2026-09-22 이다.")


def test_reference_date_is_required():
    """기준 날짜를 빼먹으면 실행한 날로 조용히 채우지 않고 바로 멈춘다."""
    with pytest.raises(TypeError):
        extract("10월 8일 목요일 오후 1시에 경복궁", Claude())


def test_expected_dates_resolve_from_the_suite_reference_date():
    """
    연도를 말하지 않은 케이스의 기대 날짜는 기준 날짜 다음에 오는 가장 가까운 그날이어야 한다.
    기준 날짜를 바꾸거나 새 케이스를 넣을 때 라벨과 어긋나면 여기서 잡힌다.
    """
    ref = date.fromisoformat(SUITE["reference_date"])
    for case in SUITE["cases"]:
        if re.search(r"\d{4}년", case["raw"]):
            continue  # 연도를 말한 입력은 기준 날짜와 상관없다
        want = date.fromisoformat(case["date"])
        assert ref < want <= ref.replace(year=ref.year + 1), case["id"]


# ── 받자마자 형식을 검증한다 (2차 피드백 4번) ─────────────────────────

VALID = {
    "date": "2026-10-08", "date_source": "inferred", "weekday_stated": "THU", "notes": [],
    "stops": [{"raw": "오후 1시 경복궁 두 시간", "place": "경복궁", "start": "13:00", "start_source": "explicit",
               "dwell_minutes": 120, "scope": "place", "scope_note": None}],
    "hard_constraints": [{"raw": "20:30 서울역 KTX", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30"}]}


def changed(path, value):
    d = copy.deepcopy(VALID)
    *head, last = path
    target = d
    for p in head:
        target = target[p]
    target[last] = value
    return d


def test_valid_extraction_reaches_the_judge_as_text():
    """검증을 통과한 날짜는 판정 코어가 읽는 문자열(YYYY-MM-DD)로 넘어간다."""
    assert to_itinerary(Extraction.model_validate(VALID))["date"] == "2026-10-08"


@pytest.mark.parametrize("path, value", [
    (("date",), "2026-02-30"),                  # 없는 날
    (("date",), "10월 8일"),
    (("weekday_stated",), "목요일"),             # THU 처럼 옮겨야 한다
    (("stops", 0, "start"), "1:00 PM"),
    (("stops", 0, "start"), "9:00"),            # 두 자리로
    (("stops", 0, "start"), "24:00"),
    (("stops", 0, "dwell_minutes"), 0),
    (("hard_constraints", 0, "time"), "8:30pm"),
])
def test_malformed_values_are_rejected(path, value):
    """형식이 틀린 값은 판정 코어로 넘기지 않는다. 넘기면 계산하다 멈추거나 엉뚱한 시각으로 판정한다."""
    with pytest.raises(ValidationError):
        Extraction.model_validate(changed(path, value))


@pytest.mark.parametrize("path, value", [
    (("date",), None),                          # 날짜가 없는데 출처가 inferred
    (("date_source",), "missing"),              # 날짜가 있는데 출처가 missing
    (("stops", 0, "start"), None),              # 시각이 없는데 출처가 explicit
    (("stops", 0, "start_source"), "missing"),  # 시각이 있는데 출처가 missing
])
def test_value_and_its_source_must_agree(path, value):
    """시각이 있는데 출처가 missing 이면 판정 코어가 그 시각을 사용자가 말한 값처럼 쓴다."""
    with pytest.raises(ValidationError):
        Extraction.model_validate(changed(path, value))


def test_flight_gets_no_buffer_rule_from_policy():
    """항공편은 flight_boarding 으로 옮긴다. 정책에 그 기준이 없어서 판정 코어가 pass 를 내지 않는다."""
    ex = Extraction.model_validate(changed(("hard_constraints", 0, "type"), "FLIGHT_DEPARTURE"))
    assert to_itinerary(ex)["hard_constraints"][0]["buffer_rule"] == "flight_boarding"
