"""
실행 조건 기록 — 2차 피드백 3번.

"한 번의 실행마다 입력/정답 라벨/사실 데이터 버전/코드 커밋/모델/프롬프트/기준 날짜를
함께 고정해 주세요." 숫자를 인용할 때 그 숫자가 어떤 조건에서 나왔는지 기록만 보고
알 수 있어야 한다.
"""

import pytest

import runmeta
from runmeta import fingerprint, library_versions, reference_date, run_meta, uncommitted


def test_fingerprint_ignores_line_endings():
    """Windows 작업 폴더(CRLF)와 다른 OS(LF)에서 같은 커밋은 같은 지문을 내야 한다."""
    assert fingerprint(b"a\r\nb\r\n") == fingerprint(b"a\nb\n")


def test_fingerprint_changes_with_one_character():
    assert fingerprint("15/15") != fingerprint("15/14")


def test_run_outputs_do_not_count_as_uncommitted():
    """실행 기록은 출력이다. 변경으로 세면 한 번 돌린 뒤의 모든 실행이 재현 불가가 된다."""
    status = " M core/last-run.json\n M core/verdict.py\n?? core/new_rule.py\nR  core/a.py -> core/b.py\n"
    assert uncommitted(status) == ["core/b.py", "core/new_rule.py", "core/verdict.py"]


def test_conditions_name_code_inputs_and_reference_date():
    meta = run_meta(reference_date="2026-09-22")
    assert meta["code_commit"] and len(meta["code_commit"]) == 40
    assert set(meta["fingerprints"]) == {"tests.json", "facts.json", "policy.json"}
    assert meta["reference_date"] == "2026-09-22"
    assert meta["reproducible"] == (meta["uncommitted"] == [])


def test_without_git_the_run_is_not_reproducible(monkeypatch):
    """압축 파일로 받은 경우처럼 git 을 못 쓰면 코드 버전을 모른다. 재현 가능하다고 적으면 안 된다."""
    monkeypatch.setattr(runmeta, "_git", lambda *args: None)
    meta = run_meta()
    assert meta["code_commit"] is None
    assert meta["uncommitted"] is None
    assert meta["reproducible"] is False


def test_reference_date_comes_from_the_suite_not_the_clock():
    """연도가 빠진 날짜를 푸는 기준은 tests.json 의 값이다. 실행한 날을 쓰면 날짜가 지날수록
    같은 입력이 다른 연도로 옮겨진다 (2차 피드백 4번)."""
    assert reference_date({"reference_date": "2026-09-22"}) == "2026-09-22"


def test_reference_date_can_be_set_when_running():
    assert reference_date({"reference_date": "2026-09-22"}, "2026-10-01") == "2026-10-01"


def test_reference_date_must_be_a_real_date():
    """13월 같은 값은 실행을 시작하기 전에 멈춘다. 이상한 날짜로 돌린 기록이 남지 않게."""
    with pytest.raises(ValueError):
        reference_date({"reference_date": "2026-09-22"}, "2026-13-01")


def test_library_versions_are_recorded():
    """모델이 받는 스키마는 SDK 버전에 따라 달라질 수 있다. 같은 커밋이라도 버전을 함께 남긴다."""
    versions = library_versions()
    assert set(versions) == {"anthropic", "pydantic"}
    assert all(versions.values())  # 테스트 환경에는 둘 다 설치돼 있다
