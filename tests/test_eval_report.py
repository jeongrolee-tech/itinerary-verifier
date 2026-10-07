"""
평가 보고 — 2차 피드백 3번 "총점 하나보다 오류 유형별 결과, 잘못된 정상 판정, unknown 비율과 원인, 비용/지연".

보고서는 기록만 읽는다. 여기서는 갈래를 나누는 규칙이 맞는지와, 판정 코드가 낼 수 있는 보류 이유를 보고서가
빠짐없이 아는지를 본다.
"""

import re
from pathlib import Path

import pytest

from eval_report import (ERROR_TYPES, NO_ANSWER, SOLVER, attribution, error_type, mismatched_conditions,
                         no_question)

CORE = Path(__file__).parent.parent / "core"


@pytest.mark.parametrize("expect, got, kind", [
    ("infeasible", "feasible", "잘못된 정상 판정"),
    ("undetermined", "feasible", "누출"),
    ("feasible", "infeasible", "오탐"),
    ("undetermined", "infeasible", "근거 없는 불가"),
    ("infeasible", "undetermined", "놓친 위반"),
    ("feasible", "undetermined", "과잉 보류"),
])
def test_wrong_verdicts_fall_into_six_kinds(expect, got, kind):
    assert error_type(expect, got).startswith(kind)


def test_right_verdict_is_not_an_error_and_refusal_is_no_answer():
    assert error_type("feasible", "feasible") is None
    assert error_type("infeasible", "refused") == NO_ANSWER
    assert len(set(ERROR_TYPES.values())) == 6


def test_every_unknown_reason_the_judge_can_give_has_a_solver():
    """판정 코드에 새 보류 이유가 생겼는데 보고서가 모르면 '누가 푸나' 칸이 비어 버린다."""
    reasons = set(re.findall(r'unknown_reason="([A-Z_]+)"', (CORE / "verdict.py").read_text(encoding="utf-8")))
    assert reasons and reasons <= set(SOLVER), reasons - set(SOLVER)


def test_question_missing_only_when_the_user_must_answer():
    """사용자가 풀어야 하는 보류에 물을 말이 없으면 잡는다. 데이터 쪽 보류는 물을 말이 없어도 된다."""
    assert no_question({"unknown_reason": "BUFFER_NOT_MET", "how_to_resolve": {"system": "…"}})
    assert not no_question({"unknown_reason": "BUFFER_NOT_MET", "how_to_resolve": {"user": "괜찮으신가요?"}})
    assert not no_question({"unknown_reason": "UNSUPPORTED_PLACE", "how_to_resolve": {"system": "…"}})


@pytest.mark.parametrize("row, blame", [
    ({"end_to_end_ok": True, "extraction_ok": True, "verdict_ok": True, "verdict": "feasible"}, "맞음"),
    ({"end_to_end_ok": False, "extraction_ok": True, "verdict_ok": False, "verdict": "feasible"}, "판정 탓"),
    ({"end_to_end_ok": False, "extraction_ok": False, "verdict_ok": False, "verdict": "feasible"}, "추출 탓"),
    ({"end_to_end_ok": False, "extraction_ok": False, "verdict_ok": True, "verdict": "feasible"}, "추출만 틀림"),
    ({"error": "refusal: …", "extraction_ok": False}, "추출 실패"),
])
def test_end_to_end_errors_are_blamed_on_a_stage(row, blame):
    assert attribution(row).startswith(blame)


def test_records_from_different_code_are_flagged():
    """조건이 다른 숫자를 한 보고서에 섞지 않는다."""
    same = {"code_commit": "abc", "fingerprints": {"tests.json": "1"}}
    assert mismatched_conditions({"판정": {"conditions": same}, "추출": {"conditions": same}}) == []
    other = {"code_commit": "def", "fingerprints": {"tests.json": "1"}}
    assert mismatched_conditions({"판정": {"conditions": same}, "추출": {"conditions": other}})
