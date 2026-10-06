"""
실행 조건 기록 — 2차 피드백 3번.

같은 15/15 라도 코드 · 라벨 · 사실 데이터 · 정책 · 프롬프트 · 기준 날짜가 다르면
다른 숫자다. run.py · run_extract.py · compare.py 가 실행 기록에 이것을 함께 남긴다.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
INPUT_FILES = ("tests.json", "facts.json", "policy.json")


def fingerprint(data: bytes | str) -> str:
    """내용 지문 — 한 글자만 바뀌어도 달라진다. sha256 앞 12자리."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    # Windows 작업 폴더는 CRLF, 다른 OS 는 LF 로 같은 커밋을 꺼낸다. 줄바꿈을 맞추지
    # 않으면 같은 파일인데 지문이 달라 "조건이 바뀌었다" 로 읽힌다.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:12]


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=HERE,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def uncommitted(porcelain: str) -> list[str]:
    """git status --porcelain 에서 결과에 영향을 주는 변경만 고른다. 실행 기록(last-*.json)은 출력이라 뺀다."""
    paths = [line[3:].split(" -> ")[-1] for line in porcelain.splitlines() if line.strip()]
    return sorted(p for p in paths if not Path(p).name.startswith("last-"))


def run_meta(**extra) -> dict:
    """실행 조건. extra 에는 스크립트마다 다른 조건(기준 날짜 · 프롬프트 지문)을 넣는다."""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", ".")
    changed = None if status is None else uncommitted(status)
    return {
        "code_commit": commit.strip() if commit else None,
        "uncommitted": changed,
        # ⭐ 커밋하지 않은 변경이 있어도 실행은 막지 않는다. 개발 중에도 돌려 봐야 한다.
        #    대신 그 실행은 커밋만으로 다시 만들 수 없다고 적어 둔다. 숫자를 인용할 때는
        #    reproducible 이 true 인 기록만 쓴다.
        "reproducible": bool(commit) and changed == [],
        "fingerprints": {name: fingerprint((HERE / name).read_bytes()) for name in INPUT_FILES},
        **extra,
    }


def reference_date(suite: dict, override: str | None = None) -> str:
    """연도가 빠진 날짜("10월 8일")를 푸는 기준 날짜. tests.json 의 값을 쓰고, --reference-date 로 바꿀 수 있다.

    실행한 날을 쓰지 않는다. 기대 추출값이 이 날을 '오늘' 로 보고 만들어졌으므로, 실행한 날을 쓰면
    날짜가 지날수록 같은 입력이 다른 연도로 옮겨져 같은 기록을 다시 만들 수 없다 (2차 피드백 4번).
    날짜가 아닌 값은 여기서 바로 멈춘다.
    """
    return date.fromisoformat(override or suite["reference_date"]).isoformat()


def describe(meta: dict) -> str:
    """화면용 한두 줄."""
    commit = (meta["code_commit"] or "알 수 없음")[:7]
    parts = [f"코드 {commit}"] + [f"{name.split('.')[0]} {fp}" for name, fp in meta["fingerprints"].items()]
    if meta.get("reference_date"):
        parts.append(f"기준 날짜 {meta['reference_date']}")
    line = "  조건: " + " · ".join(parts)
    if meta["uncommitted"] is None:
        line += "\n  ⚠ git 을 쓸 수 없어 코드 버전을 남기지 못했다 — 재현할 수 없는 실행이다"
    elif meta["uncommitted"]:
        line += "\n  ⚠ 커밋하지 않은 변경이 있다 — 재현할 수 없는 실행이다: " + ", ".join(meta["uncommitted"])
    return line
