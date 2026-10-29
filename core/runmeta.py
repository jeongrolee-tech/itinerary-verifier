"""실행 조건을 기록한다. 같은 점수라도 코드나 입력이 다르면 다른 숫자다."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
INPUT_FILES = ("tests.json", "facts.json", "policy.json")


def fingerprint(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    # Windows 는 CRLF 로 꺼내므로 줄바꿈을 맞춰야 같은 파일이 같은 지문을 낸다.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:12]


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=HERE,
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def uncommitted(porcelain: str) -> list[str]:
    """실행 기록과 응답 캐시는 실행이 쓰는 출력이라 뺀다."""
    paths = [line[3:].split(" -> ")[-1] for line in porcelain.splitlines() if line.strip()]
    return sorted(p for p in paths
                  if not Path(p).name.startswith("last-") and Path(p).name != "response-cache.json")


def record_name(name: str, suite: str) -> str:
    """last-run.json → last-run-eval-new.json. 회귀셋 기록을 덮어쓰지 않는다."""
    return name if suite == "tests.json" else name.replace(".json", f"-{Path(suite).stem}.json")


def run_meta(suite: str = "tests.json", **extra) -> dict:
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--", ".")
    changed = None if status is None else uncommitted(status)
    return {
        "code_commit": commit.strip() if commit else None,
        "uncommitted": changed,
        # 개발 중에도 돌려 봐야 해서 막지 않는다. 대신 재현할 수 없다고 적는다.
        "reproducible": bool(commit) and changed == [],
        "fingerprints": {name: fingerprint((HERE / name).read_bytes())
                         for name in (suite, *INPUT_FILES[1:])},
        **extra,
    }


def reference_date(suite: dict, override: str | None = None) -> str:
    """연도 없는 날짜는 실행한 날이 아니라 이 날짜로 푼다. 그래야 기록이 재현된다."""
    return date.fromisoformat(override or suite["reference_date"]).isoformat()


def library_versions() -> dict:
    """requirements.txt 가 하한만 적어 같은 커밋에서도 모델이 받는 스키마가 달라질 수 있다."""
    from importlib.metadata import PackageNotFoundError, version
    found = {}
    for name in ("anthropic", "pydantic"):
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    return found


def describe(meta: dict) -> str:
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
