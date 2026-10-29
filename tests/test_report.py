"""결과 묶음과 일정 줄이 검사를 한 번씩 담고 판정을 바꾸지 않는지 본다."""

import copy
import json
from pathlib import Path

import pytest

from labels import case_facts
from report import NOT_CHECKED, questions, render, report, timeline
from verdict import judge

ROOT = Path(__file__).parent.parent
CASES = json.loads((ROOT / "core" / "tests.json").read_text(encoding="utf-8"))["cases"]


def case(prefix):
    return next(c for c in CASES if c["id"].startswith(prefix))


@pytest.mark.parametrize("c", CASES, ids=[c["id"] for c in CASES])
def test_every_check_lands_in_its_status_group_once(c, facts, policy):
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
    questions(result)
    assert result == before


def test_not_checked_is_attached_even_when_feasible(facts, policy):
    """feasible 을 반드시 가능하다로 읽지 않게 검사하지 않는 것도 붙인다."""
    rep = report(judge(case("T10"), facts, policy))
    assert rep["verdict"] == "feasible"
    assert rep["not_checked"] == NOT_CHECKED
    assert "검사하지 않는 것" in render(rep)


def test_unknown_keeps_question_for_user(facts, policy):
    """T08 은 체류시간을 말하지 않아 그 질문이 붙는다."""
    rep = report(judge(case("T08"), facts, policy))
    unknown = next(g for g in rep["groups"] if g["status"] == "unknown")
    assert [it["ask_user"] for it in unknown["items"]] == ["경복궁에서 얼마나 머무르실 예정인가요?"]


@pytest.mark.parametrize("prefix, expected", [
    # 체류 검사와 다음 구간이 같은 체류시간을 묻고, 점포도 두 검사가 묻는다.
    ("T12", ["경복궁에서 얼마나 머무르실 예정인가요?", "광장시장 — 방문할 점포를 알려주세요"]),
    # 열차 조건도 같은 체류시간을 물어 한 번만 묻는다.
    ("T04", ["광장시장 — 방문할 점포를 알려주세요", "광장시장에서 얼마나 머무르실 예정인가요?"]),
    # 구역은 휴무일 · 입장 · 이동이 같은 말로 묻는다.
    ("T28", ["삼청동에서 정확히 어디로 가시나요?"]),
])
def test_questions_ask_each_thing_once(prefix, expected, facts, policy):
    """장소 이름이 없는 질문에는 장소를 붙인다."""
    assert questions(judge(case(prefix), facts, policy)) == expected


@pytest.mark.parametrize("c", CASES, ids=[c["id"] for c in CASES])
def test_questions_keep_something_to_ask(c, facts, policy):
    """묻는 항목이 있으면 모은 질문도 있고 같은 질문은 두 번 나오지 않는다."""
    result = judge(c, facts, policy)
    asked = [it["ask_user"] for g in report(result)["groups"] for it in g["items"] if it["ask_user"]]
    qs = questions(result)
    assert len(qs) == len(set(qs)) and bool(qs) == bool(asked)


def test_render_shows_violations_first(facts, policy):
    """고칠 것이 먼저 보이도록 어긋난 것부터 적는다. T04 는 네 묶음이 모두 나온다."""
    lines = render(report(judge(case("T04"), facts, policy))).split("\n")
    order = ["어긋난 것", "확인하지 못한 것", "확인한 것", "해당 없음", "검사하지 않는 것"]
    positions = [next(i for i, line in enumerate(lines) if line.startswith(f"  {title}")) for title in order]
    assert positions == sorted(positions)


def test_not_checked_matches_readme():
    """README 와 결과 화면이 다른 약속을 하지 않게 글자까지 대조한다."""
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


# 일정 순서로 보기
# 검사 하나가 빠지거나 두 줄에 들어가면 화면이 판정과 달라 보인다.

def lines_of(rows):
    return [x for r in rows for x in r["lines"]]


@pytest.mark.parametrize("c", CASES, ids=[c["id"] for c in CASES])
def test_every_check_lands_in_one_timeline_row(c, facts, policy):
    """마지막 장소 뒤 (없음) 구간은 일정에 없는 줄이라 뺀다."""
    result = judge(c, case_facts(c, facts), policy)
    rows = timeline(c, result)
    expected = sorted([x["id"] for x in result["checks"] if not x["target"].endswith("→ (없음)")]
                      + [h["id"] for h in result["hard_constraints"]])
    assert sorted(x["id"] for x in lines_of(rows)) == expected


def statuses(prefix, facts, policy):
    c = case(prefix)
    return [(r["kind"], r["title"], r["status"]) for r in timeline(c, judge(c, case_facts(c, facts), policy))]


def test_one_closed_place_shows_where_in_the_day(facts, policy):
    """T31: 경복궁만 화요일 휴궁이고, 휴궁이 낀 구간도 여유를 더해 시작 전에 닿는다."""
    assert statuses("T31", facts, policy) == [
        ("stop", "창덕궁", "pass"), ("travel", "창덕궁 → 경복궁", "pass"),
        ("stop", "경복궁", "fail"), ("travel", "경복궁 → 덕수궁", "pass"),
        ("stop", "덕수궁", "pass"), ("travel", "덕수궁 → 서울시립미술관 서소문본관", "pass"),
        ("stop", "서울시립미술관 서소문본관", "pass")]


def test_unknown_is_its_own_mark(facts, policy):
    """T32: 광장시장 줄만 미확인이고 앞뒤 장소와 구간은 통과다."""
    rows = statuses("T32", facts, policy)
    assert ("stop", "광장시장", "unknown") in rows
    assert all(s == "pass" for kind, title, s in rows if title != "광장시장")


def test_hard_constraint_comes_last(facts, policy):
    assert statuses("T33", facts, policy)[-1] == ("hard", "필수 일정 · 열차 출발 서울역", "pass")


def test_missing_input_rows_wait(facts, policy):
    """날짜가 없으면 입력부터 묻고, 검사하지 않은 줄은 상태가 없다."""
    it = {"date": None, "date_source": "missing", "weekday_stated": None, "hard_constraints": [],
          "stops": [{"place": "경복궁", "start": "14:00", "start_source": "explicit", "scope": "place"},
                    {"place": "덕수궁", "start_source": "missing", "scope": "place"}]}
    rows = timeline(it, judge(it, facts, policy))
    assert (rows[0]["kind"], rows[0]["status"]) == ("input", "unknown")
    assert [(r["title"], r["status"]) for r in rows[1:]] == [
        ("경복궁", None), ("경복궁 → 덕수궁", None), ("덕수궁", "unknown")]


def test_same_place_twice_keeps_each_visit(facts, policy):
    """검사 대상 이름이 겹쳐도 줄마다 자기 방문의 검사 세 개를 갖는다."""
    it = {"date": "2026-10-21", "date_source": "explicit", "weekday_stated": None, "hard_constraints": [],
          "stops": [{"place": "경복궁", "start": "10:00", "start_source": "explicit", "dwell_minutes": 60, "scope": "place"},
                    {"place": "경복궁", "start": "15:00", "start_source": "explicit", "dwell_minutes": 60, "scope": "place"}]}
    result = judge(it, facts, policy)
    stops = [r for r in timeline(it, result) if r["kind"] == "stop"]
    assert [len(r["lines"]) for r in stops] == [3, 3]
    assert stops[0]["lines"][0]["id"] != stops[1]["lines"][0]["id"]


def test_timeline_does_not_change_result(facts, policy):
    c = case("T31")
    result = judge(c, case_facts(c, facts), policy)
    before = copy.deepcopy(result)
    timeline(c, result)
    assert result == before
