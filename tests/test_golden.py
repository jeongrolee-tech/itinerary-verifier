"""tests.json 케이스를 판정해 라벨과 대조한다. 라벨은 판정 코드를 보고 고치지 않는다."""

import pytest

from helpers import SUITE
from labels import case_facts, grade, grade_applied, stale_reason
from verdict import judge


@pytest.mark.parametrize("case", SUITE["cases"], ids=[c["id"] for c in SUITE["cases"]])
def test_case_matches_its_label(case, facts, policy):
    stale = stale_reason(case, SUITE, facts)
    if stale:
        pytest.skip(f"라벨 미검토 — {stale}")  # 데이터가 바뀐 라벨은 정답으로 믿을 수 없다.
    out = judge(case, case_facts(case, facts), policy)
    assert grade(case, out) + grade_applied(case, out) == []
