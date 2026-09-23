"""
수정된 일정 전체를 다시 검사한다.

판정 코어(verdict.py)는 한 일정의 결과만 계산한다. 여기서는 사용자가
그 결과를 보고 입력을 고친 뒤, 수정 전과 후를 함께 기록한다.

바뀐 스톱만 따로 보지 않고 judge 를 수정된 전체 일정에 다시 부른다.
한 곳을 고치면 뒤 일정의 이동시간과 필수 조건이 함께 움직이기 때문이다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from verdict import judge  # noqa: E402


def _diff_paths(before: Any, after: Any, path: str = "") -> list[str]:
    """두 JSON 형태 값의 변경 경로를 사람이 읽기 쉽게 반환한다."""
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[key], after[key], child))
        return paths

    if isinstance(before, list) and isinstance(after, list):
        paths = []
        for i in range(max(len(before), len(after))):
            child = f"{path}[{i}]"
            if i >= len(before) or i >= len(after):
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[i], after[i], child))
        return paths

    return [] if before == after else [path or "(입력 전체)"]


def _resolution_hints(result: dict) -> list[dict]:
    """현재 결과에서 사용자가 보완할 항목만 모아 반환한다."""
    hints: list[dict] = []
    for check in result.get("checks", []):
        if check.get("status") not in {"fail", "unknown"}:
            continue
        hints.append({
            "id": check.get("id"),
            "type": check.get("type"),
            "target": check.get("target"),
            "status": check.get("status"),
            "reason": check.get("unknown_reason") or check.get("reason"),
            "how_to_resolve": check.get("how_to_resolve", {}),
        })

    for hard in result.get("hard_constraints", []):
        policy = hard.get("policy", {})
        if policy.get("status") not in {"fail", "unknown"}:
            continue
        hints.append({
            "id": hard.get("id"),
            "type": hard.get("event", {}).get("type"),
            "target": hard.get("event", {}).get("place"),
            "status": policy.get("status"),
            "reason": policy.get("unknown_reason"),
            "how_to_resolve": policy.get("how_to_resolve", {}),
        })
    return hints


def recheck_revision(original: dict, revised: dict, facts: dict, policy: dict) -> dict:
    """
    수정 전·후 일정을 모두 검사하고 비교 결과를 반환한다.

    revised 전체를 judge 에 넘기므로 바뀐 장소뿐 아니라 날짜·체류시간·
    이동시간·필수 조건이 전부 다시 검사된다.
    """
    before = judge(original, facts, policy)
    after = judge(revised, facts, policy)
    before_verdict = before["summary"]["verdict"]
    after_verdict = after["summary"]["verdict"]

    return {
        "changed_fields": _diff_paths(original, revised),
        "rechecked_all": True,
        "before": before,
        "after": after,
        "verdict_changed": before_verdict != after_verdict,
        "resolution_hints_before": _resolution_hints(before),
        "resolution_hints_after": _resolution_hints(after),
    }

