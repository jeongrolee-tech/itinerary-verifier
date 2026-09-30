"""
결과를 묶어서 보여주기 — 2차 피드백 2번.

"결과 화면에서도 검사한 것과 확인하지 못한 것을 각각 보여주세요."
묶음은 보여주는 방법일 뿐이다. 검사 하나가 빠지거나 두 번 들어가면 안 되고,
판정 결과를 바꿔서도 안 된다.
"""

import copy
import json
from pathlib import Path

import pytest

from report import NOT_CHECKED, render, report
from verdict import judge

ROOT = Path(__file__).parent.parent
CASES = json.loads((ROOT / "core" / "tests.json").read_text(encoding="utf-8"))["cases"]


def case(prefix):
    return next(c for c in CASES if c["id"].startswith(prefix))


@pytest.mark.parametrize("c", CASES, ids=[c["id"] for c in CASES])
def test_every_check_lands_in_its_status_group_once(c, facts, policy):
    """골든 15건 전부 — 검사와 필수 조건이 빠짐없이, 한 번씩, 자기 상태의 묶음에 들어간다."""
    result = judge(c, facts, policy)
    rep = report(result)
    expected = sorted([(x["id"], x["status"]) for x in result["checks"]]
                      + [(h["id"], h["policy"]["status"]) for h in result["hard_constraints"]])
    got = sorted((it["id"], g["status"]) for g in rep["groups"] for it in g["items"])
    assert got == expected


def test_report_does_not_change_result(facts, policy):
    """보여주기가 판정을 바꾸면 채점하는 결과와 사용자가 보는 결과가 달라진다."""
    result = judge(case("T04"), facts, policy)
    before = copy.deepcopy(result)
    report(result)
    assert result == before


def test_not_checked_is_attached_even_when_feasible(facts, policy):
    """feasible 에도 검사하지 않는 것이 같이 나와야 '여행이 반드시 가능하다' 로 읽히지 않는다."""
    rep = report(judge(case("T10"), facts, policy))
    assert rep["verdict"] == "feasible"
    assert rep["not_checked"] == NOT_CHECKED
    assert "검사하지 않는 것" in render(rep)


def test_unknown_keeps_question_for_user(facts, policy):
    """확인하지 못한 것에는 무엇을 물어야 하는지가 같이 붙는다 (T08: 체류시간 미입력)."""
    rep = report(judge(case("T08"), facts, policy))
    unknown = next(g for g in rep["groups"] if g["status"] == "unknown")
    assert [it["ask_user"] for it in unknown["items"]] == ["경복궁에서 얼마나 머무르실 예정인가요?"]


def test_render_shows_violations_first(facts, policy):
    """고칠 것이 먼저 보이도록 어긋난 것부터 적는다. T04 는 네 묶음이 모두 나온다."""
    lines = render(report(judge(case("T04"), facts, policy))).split("\n")
    order = ["어긋난 것", "확인하지 못한 것", "확인한 것", "해당 없음", "검사하지 않는 것"]
    positions = [next(i for i, line in enumerate(lines) if line.startswith(f"  {title}")) for title in order]
    assert positions == sorted(positions)


def test_not_checked_matches_readme():
    """README 지원 범위와 결과 화면이 서로 다른 약속을 하지 않게, '검사하지 않는 것' 칸과 글자까지 대조한다."""
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("| 검사하는 것 | 검사하지 않는 것 |")
    cells = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cell = line.strip("|").split("|")[1].strip()
        if cell:
            cells.append(cell)
    assert cells == NOT_CHECKED
