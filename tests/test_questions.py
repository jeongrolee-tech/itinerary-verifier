"""사용자가 풀 보류에는 물을 말이 있고, 답할 수 없는 것은 묻지 않는지 본다."""

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
    """20:00 + 20분 = 20:20 이라 여유가 15분에 못 미쳐 괜찮은지 묻는다."""
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
    """광장시장은 점포마다 시간이 달라 갈 점포를 알아야 확인할 수 있다."""
    checks = place_checks(judge({"date": THURSDAY, "stops": [{"place": "광장시장", "start": "18:00"}]}, facts, policy),
                          "광장시장")
    for t in ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE"):
        assert checks[t]["how_to_resolve"]["user"] == "방문할 점포를 알려주세요"


def test_street_without_data_is_not_asked_for_a_shop(facts, policy):
    """청계천은 점포로 답할 수 없어 우리가 공식 출처를 확보해야 풀린다."""
    c = place_checks(judge({"date": THURSDAY, "stops": [{"place": "청계천", "start": "18:00", "dwell_minutes": 60}]},
                           facts, policy), "청계천")["CLOSED_DAY"]
    assert c["status"] == UNKNOWN and set(c["how_to_resolve"]) == {"system"}


@pytest.mark.parametrize("case", SUITE["cases"], ids=[c["id"] for c in SUITE["cases"]])
def test_every_unknown_the_user_can_solve_comes_with_a_question(case, facts, policy):
    out = judge(case, case_facts(case, facts), policy)
    assert [u.get("unknown_reason") for u in unknowns(out) if no_question(u)] == []
