"""
보류마다 누가 무엇을 하면 풀리는지 — 2차 피드백 4번 "빠진 정보 질문 → 근거별 판정 → 사용자 수정 → 재검사".

unknown 은 끝이 아니라 다음에 할 일이다. 사용자가 풀 수 있는 보류에는 물을 말이 있어야 하고, 사용자가 답할 수
없는 것을 물어서는 안 된다. 평가 보고(core/eval_report.py)가 10-07 기록에서 찾은 빈틈이 시작이다.
"""

import json
from pathlib import Path

import pytest

from eval_report import no_question, unknowns
from labels import case_facts
from verdict import UNKNOWN, judge

SUITE = json.loads((Path(__file__).parent.parent / "core" / "tests.json").read_text(encoding="utf-8"))
THURSDAY = "2026-10-15"


def leg(minutes):
    return {"minutes": minutes, "mode": "transit", "is_lower_bound": False,
            "source": "synthetic_fixture", "method": "synthetic_fixture"}


def test_train_buffer_shortfall_asks_the_user(facts, policy):
    """열차 15분 전에 못 닿아도 열차보다 먼저 닿으면 사용자가 판단한다 — 그러려면 물어야 한다 (T27)."""
    facts["legs"]["덕수궁|서울역"] = leg(20)
    out = judge({"date": THURSDAY, "stops": [{"place": "덕수궁", "start": "18:00", "dwell_minutes": 120}],
                 "hard_constraints": [{"id": "HC1", "type": "TRAIN_DEPARTURE", "place": "서울역", "time": "20:30",
                                       "buffer_rule": "rail_boarding"}]}, facts, policy)
    p = out["hard_constraints"][0]["policy"]
    assert p["unknown_reason"] == "BUFFER_NOT_MET"
    assert p["how_to_resolve"]["user"] == "열차 출발(20:30) 전 여유가 10분뿐인데 괜찮으신가요?"


def place_checks(out, name):
    return {c["type"]: c for c in out["checks"] if c["target"] == name}


def test_market_without_hours_asks_which_shop(facts, policy):
    """광장시장은 점포마다 시간이 달라 시장 단위 정보가 없다. 갈 점포를 알면 그 점포를 확인할 수 있다."""
    checks = place_checks(judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00"}]}, facts, policy),
                          "광장시장")
    for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE"):
        assert checks[t]["how_to_resolve"]["user"] == "방문할 점포를 알려주세요"


def test_street_without_data_is_not_asked_for_a_shop(facts, policy):
    """청계천에 '방문할 점포' 를 물으면 답할 수 없다. 우리가 공식 출처를 확보해야 풀린다."""
    c = place_checks(judge({"date": THURSDAY, "stops": [{"place": "청계천", "start": "18:00", "dwell_minutes": 60}]},
                           facts, policy), "청계천")["CLOSED_DAY"]
    assert c["status"] == UNKNOWN and set(c["how_to_resolve"]) == {"system"}


@pytest.mark.parametrize("case", SUITE["cases"], ids=[c["id"] for c in SUITE["cases"]])
def test_every_unknown_the_user_can_solve_comes_with_a_question(case, facts, policy):
    """골든 케이스 전부 — 사용자가 풀어야 하는 보류(eval_report.SOLVED_BY)에 물을 말이 빠지지 않는다."""
    out = judge(case, case_facts(case, facts), policy)
    assert [u.get("unknown_reason") for u in unknowns(out) if no_question(u)] == []
