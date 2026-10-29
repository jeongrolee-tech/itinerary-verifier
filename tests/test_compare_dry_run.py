"""compare.py --dry-run 이 키 없이 LLM 방식의 코드 경로를 끝까지 도는지 본다."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dry_run_goes_through_every_llm_arm():
    # 요청 본문 모양이 바뀌면 가짜 클라이언트가 못 읽는다. 기록은 .gitignore 된 dry-run 파일에만 쓴다.
    r = subprocess.run([sys.executable, "core/compare.py", "--dry-run", "--case", "T05"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", timeout=120,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    assert "가짜 응답으로 돌렸다" in r.stdout
