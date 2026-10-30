"""compare.py --dry-run 이 키 없이 LLM 방식의 코드 경로를 끝까지 도는지 본다."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dry_run(*args):
    return subprocess.run([sys.executable, "core/compare.py", "--dry-run", *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def test_dry_run_goes_through_every_llm_arm():
    # 요청 본문 모양이 바뀌면 가짜 클라이언트가 못 읽는다. 기록은 .gitignore 된 dry-run 파일에만 쓴다.
    r = dry_run("--case", "T05")
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    assert "가짜 응답으로 돌렸다" in r.stdout


def test_case_judged_differently_from_run_py_stops_first(tmp_path):
    """비교 실험은 말한 요일을 넘기지 않는다. 요일 없는 T10 은 판정이 갈려 부르기 전에 멈춘다."""
    suite = json.loads((ROOT / "core" / "tests.json").read_text(encoding="utf-8"))
    case = next(c for c in suite["cases"] if c["id"].startswith("T10"))
    del case["weekday_stated"]
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({**suite, "cases": [case]}, ensure_ascii=False), encoding="utf-8")
    r = dry_run("--suite", str(path))
    assert r.returncode == 1 and "T10" in r.stderr and "돈이 든다" in r.stderr
    assert "가짜 응답으로 돌렸다" not in r.stdout
