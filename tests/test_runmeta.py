"""인용한 숫자의 실행 조건을 기록만 보고 알 수 있는지 본다."""

import pytest

import runmeta
from runmeta import fingerprint, library_versions, record_name, reference_date, run_meta, uncommitted


def test_fingerprint_ignores_line_endings():
    """Windows 작업 폴더(CRLF)와 다른 OS(LF)에서 같은 커밋은 같은 지문을 내야 한다."""
    assert fingerprint(b"a\r\nb\r\n") == fingerprint(b"a\nb\n")


def test_fingerprint_changes_with_one_character():
    assert fingerprint("15/15") != fingerprint("15/14")


def test_run_outputs_do_not_count_as_uncommitted():
    """출력인 실행 기록 · 응답 캐시를 변경으로 세면 모든 실행이 재현 불가가 된다."""
    status = (" M core/last-run.json\n M core/response-cache.json\n M core/verdict.py\n"
              "?? core/new_rule.py\nR  core/a.py -> core/b.py\n")
    assert uncommitted(status) == ["core/b.py", "core/new_rule.py", "core/verdict.py"]


def test_conditions_name_code_inputs_and_reference_date():
    meta = run_meta(reference_date="2026-09-22")
    assert meta["code_commit"] and len(meta["code_commit"]) == 40
    assert set(meta["fingerprints"]) == {"tests.json", "facts.json", "policy.json"}
    assert meta["reference_date"] == "2026-09-22"
    assert meta["reproducible"] == (meta["uncommitted"] == [])


def test_without_git_the_run_is_not_reproducible(monkeypatch):
    """압축 파일로 받아 git 이 없으면 코드 버전을 몰라 재현 가능이 아니다."""
    monkeypatch.setattr(runmeta, "_git", lambda *args: None)
    meta = run_meta()
    assert meta["code_commit"] is None
    assert meta["uncommitted"] is None
    assert meta["reproducible"] is False


def test_reference_date_comes_from_the_suite_not_the_clock():
    """실행한 날을 쓰면 날짜가 지날수록 같은 입력이 다른 연도로 옮겨진다."""
    assert reference_date({"reference_date": "2026-09-22"}) == "2026-09-22"


def test_reference_date_can_be_set_when_running():
    assert reference_date({"reference_date": "2026-09-22"}, "2026-10-01") == "2026-10-01"


def test_reference_date_must_be_a_real_date():
    """이상한 날짜로 돌린 기록이 남지 않게 13월 같은 값은 시작 전에 멈춘다."""
    with pytest.raises(ValueError):
        reference_date({"reference_date": "2026-09-22"}, "2026-13-01")


def test_library_versions_are_recorded():
    """모델이 받는 스키마가 SDK 버전에 따라 달라질 수 있어 버전도 남긴다."""
    versions = library_versions()
    assert set(versions) == {"anthropic", "pydantic"}
    assert all(versions.values())  # 테스트 환경에는 둘 다 설치돼 있다.


def test_new_evaluation_records_do_not_overwrite_the_regression_records():
    """README 가 인용하는 회귀셋 기록을 새 평가 실행이 덮어쓰지 않는다."""
    assert record_name("last-run.json", "tests.json") == "last-run.json"
    assert record_name("last-run.json", "eval-new.json") == "last-run-eval-new.json"
    assert record_name("last-compare-subset.json", "eval-new.json") == "last-compare-subset-eval-new.json"


def test_conditions_fingerprint_the_suite_that_ran():
    assert "eval-new.json" in run_meta("eval-new.json")["fingerprints"]
