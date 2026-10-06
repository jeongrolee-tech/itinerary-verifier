"""
추출 — 연도가 빠진 날짜를 푸는 기준 날짜 (2차 피드백 4번).

기준 날짜는 시스템 시계가 아니라 부르는 쪽이 넣는다. 평가에서는 tests.json 의 reference_date 를 쓴다.
Claude 는 가짜로 끼운다(test_verify.py 와 같은 방식). 진짜로 부르지 않으니 키도 요금도 들지 않는다.
"""

import json
import re
from datetime import date
from pathlib import Path

import pytest
from anthropic.types import Message

from extract import extract

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
