"""
수정된 일정 전체를 다시 검사한다.

판정 코어(verdict.py)는 한 일정의 결과만 계산한다. 여기서는 사용자가
그 결과를 보고 입력을 고친 뒤, 수정 전과 후를 함께 기록한다.

바뀐 스톱만 따로 보지 않고 judge 를 수정된 전체 일정에 다시 부른다.
한 곳을 고치면 뒤 일정의 이동시간과 필수 조건이 함께 움직이기 때문이다.

고친 일정이 사용자가 꼭 지킨다고 한 것(필수 조건 · 날짜에 묶인 예약 · 가려던 장소)을
바꿨으면 needs_confirmation 에 따로 적는다 — 2차 피드백 2번.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from verdict import EVENT_NAMES, judge  # noqa: E402


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


def _event(hc: dict) -> str:
    return f"{EVENT_NAMES.get(hc.get('type'), '필수 조건')}({hc.get('time')} {hc.get('place')})"


def _needs_confirmation(original: dict, revised: dict) -> list[dict]:
    """
    고친 일정이 사용자가 꼭 지킨다고 한 것을 바꿨는지 본다.

    시각 · 체류시간 · 방문 순서는 고쳐도 되는 것이라 적지 않는다.
    """
    # ⭐ 판정이 나아졌다는 것만으로 수정을 받아들이지 않는다.
    #    장소를 지우면 확인할 것이 줄어 판정이 나아지고, 날짜를 옮기면 예약한 열차도
    #    새 날짜에 탈 수 있는 것처럼 판정된다. 둘 다 사용자가 원한 일정이 아닐 수 있다.
    items: list[dict] = []
    constraints = original.get("hard_constraints") or []

    if constraints and original.get("date") != revised.get("date"):
        events = ", ".join(_event(h) for h in constraints)
        items.append({
            "kind": "DATE_MOVED", "target": "일정 날짜",
            "detail": f"날짜가 바뀌었다({original.get('date')} → {revised.get('date')}). "
                      f"필수 조건 {events}은(는) 원래 날짜로 예약했을 수 있다",
            "ask_user": f"{events} 예약도 새 날짜({revised.get('date')})에 맞게 옮기셨나요?",
        })

    after = {h.get("id"): h for h in revised.get("hard_constraints") or []}
    for h in constraints:
        new = after.get(h.get("id"))
        if new is None:
            detail = f"꼭 지킨다고 한 조건이 고친 일정에 없다({_event(h)})"
        elif any(new.get(k) != h.get(k) for k in ("type", "place", "time")):
            detail = f"꼭 지킨다고 한 조건이 바뀌었다({_event(h)} → {_event(new)})"
        else:
            continue
        items.append({"kind": "HARD_CONSTRAINT_CHANGED", "target": h.get("id"), "detail": detail,
                      "ask_user": f"{_event(h)} 조건을 바꿔도 되나요?"})

    # ⭐ 장소는 순서가 아니라 이름으로 짝을 맞춘다. 순서로 비교하면 방문 순서만
    #    바꿔도 모든 칸의 장소가 바뀐 것처럼 보인다.
    left = Counter(s.get("place") for s in revised.get("stops") or [])
    for s in original.get("stops") or []:
        place = s.get("place")
        if left[place]:
            left[place] -= 1
            continue
        items.append({"kind": "STOP_DROPPED", "target": place,
                      "detail": "고친 일정에 없다 — 빠졌거나 다른 장소로 바뀌었다",
                      "ask_user": f"{place} 방문을 빼도 되나요?"})
    return items


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
        "needs_confirmation": _needs_confirmation(original, revised),
        "resolution_hints_before": _resolution_hints(before),
        "resolution_hints_after": _resolution_hints(after),
    }

