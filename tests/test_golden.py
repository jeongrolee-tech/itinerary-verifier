"""
골든 테스트를 회귀 테스트로 — 2차 피드백 4번 "판정 함수를 작은 단위로 분리한 뒤 pytest로 위 반례와 3~6개 장소
시나리오를 회귀 테스트".

tests.json 의 케이스 전부를 판정 코어에 넣고 라벨과 대조한다. run.py 와 같은 채점(labels.grade · grade_applied)이다.
반례는 test_confirmed_fail.py 에, 3~6곳 일정은 T16 · T17 같은 케이스에 있다. 판정 코드를 고치다 라벨과 어긋나면
여기서 바로 잡힌다. 라벨을 고쳐서 맞추지 않는다 — 라벨은 판정 코드를 보고 고치지 않는다(CLAUDE.md).
"""

import json
from pathlib import Path

import pytest

from labels import case_facts, grade, grade_applied, stale_reason
from verdict import judge

SUITE = json.loads((Path(__file__).parent.parent / "core" / "tests.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", SUITE["cases"], ids=[c["id"] for c in SUITE["cases"]])
def test_case_matches_its_label(case, facts, policy):
    stale = stale_reason(case, SUITE, facts)
    if stale:
        pytest.skip(f"라벨 미검토 — {stale}")  # 데이터가 바뀐 라벨은 정답으로 믿을 수 없다(run.py 와 같은 규칙)
    out = judge(case, case_facts(case, facts), policy)
    assert grade(case, out) + grade_applied(case, out) == []
