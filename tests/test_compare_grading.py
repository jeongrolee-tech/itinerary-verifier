"""비교 실험의 채점이 받아 둔 응답에서 틀린 답을 실제로 걸러내는지 본다. API 는 부르지 않는다."""

import copy
import json

from helpers import CORE, SUITE
from labels import grade_answer

STORED = {r["id"]: r for r in json.loads((CORE / "last-compare.json").read_text(encoding="utf-8"))["rows"]}
CASES = {c["id"]: c for c in SUITE["cases"]}


def stored(prefix, arm):
    cid = next(k for k in STORED if k.startswith(prefix))
    return CASES[cid], copy.deepcopy(STORED[cid]["arms"][arm])


def same(target):
    return target.replace(" ", "")


def test_stored_answer_with_the_same_rules_is_clean():
    """대조군: 같은 규칙을 받은 LLM 의 T04 답은 판정 · 검사 · 필수 일정이 모두 맞다."""
    case, ans = stored("T04", "llm_with_facts")
    g = grade_answer(case, ans)
    assert g["verdict_ok"] and g["checks_wrong"] == [] and g["hard_constraints_wrong"] == []


def test_dropping_an_expected_check_is_caught():
    case, ans = stored("T04", "llm_with_facts")
    want = case["expect_checks"][0]
    ans["checks"] = [c for c in ans["checks"] if not (c["type"] == want["type"] and same(c["target"]) == same(want["target"]))]
    assert [w["got"] for w in grade_answer(case, ans)["checks_wrong"]] == [None]


def test_changing_a_status_is_caught():
    case, ans = stored("T04", "llm_with_facts")
    want = case["expect_checks"][0]
    (c,) = [c for c in ans["checks"] if c["type"] == want["type"] and same(c["target"]) == same(want["target"])]
    c["status"] = "fail" if want["status"] != "fail" else "pass"
    assert [w["got"] for w in grade_answer(case, ans)["checks_wrong"]] == [c["status"]]


def test_dropping_the_train_leg_is_caught():
    """LLM 은 KTX 를 '광장시장 → 서울역' 이동 검사로 적는다. 그 검사를 지우면 필수 일정이 틀린다."""
    case, ans = stored("T04", "llm_with_facts")
    ans["checks"] = [c for c in ans["checks"] if same(c["target"]) != "광장시장→서울역"]
    assert grade_answer(case, ans)["hard_constraints_wrong"] == [{**case["expect_hard_constraints"][0], "got": None}]


def test_code_train_is_graded_by_the_same_rule():
    case, ans = stored("T04", "code")
    ans["hard_constraints"][0]["status"] = "pass"
    assert grade_answer(case, ans)["hard_constraints_wrong"][0]["got"] == "pass"


def test_naive_llm_passing_the_train_is_caught():
    """받아 둔 'LLM에 그냥 물음' T04 답은 전체 판정은 맞지만 미확인이어야 할 KTX 를 통과시켰다."""
    case, ans = stored("T04", "llm_naive")
    g = grade_answer(case, ans)
    assert g["verdict_ok"] and g["hard_constraints_wrong"][0]["got"] == "pass"


def test_reason_text_is_not_graded():
    """한계: 검사의 이유 글은 정답에 없어 채점하지 않는다. 이유만 바꾼 답은 걸러내지 못한다."""
    case, ans = stored("T04", "llm_with_facts")
    for c in ans["checks"]:
        c["reason"] = "근거 없음"
    g = grade_answer(case, ans)
    assert g["checks_wrong"] == [] and g["hard_constraints_wrong"] == []
