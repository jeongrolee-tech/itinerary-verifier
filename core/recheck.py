"""고친 일정 전체를 다시 판정한다. 한 곳을 고치면 뒤 일정도 함께 움직인다."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from verdict import EVENT_NAMES, judge  # noqa: E402


def _diff_paths(before: Any, after: Any, path: str = "") -> list[str]:
    """바뀐 칸의 경로를 stops[1].start 꼴로 모은다."""
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
    """필수 일정을 화면에 쓰는 말로 바꾼다. 예: 열차 출발(18:00 서울역)"""
    return (f"{EVENT_NAMES.get(hc.get('type'), '필수 일정')}"
            f"({hc.get('time') or '시각 없음'} {hc.get('place') or '장소 없음'})")


def _needs_confirmation(original: dict, revised: dict) -> list[dict]:
    """꼭 지킨다던 것을 바꿨는지 본다. 시각과 체류, 방문 순서는 고쳐도 된다."""
    # 판정이 나아진 것만으로 수정을 받아들이지 않는다. 장소를 빼기만 해도 판정은 나아진다.
    items: list[dict] = []
    constraints = original.get("hard_constraints") or []

    # 날짜를 처음 채우거나 지운 것은 옮긴 것으로 보지 않는다.
    if constraints and original.get("date") and revised.get("date") and original["date"] != revised["date"]:
        events = ", ".join(_event(h) for h in constraints)
        items.append({
            "kind": "DATE_MOVED", "target": "일정 날짜",
            "detail": f"날짜가 바뀌었다({original.get('date')} → {revised.get('date')}). "
                      f"필수 일정 {events}은(는) 원래 날짜로 예약했을 수 있다",
            "ask_user": f"{events} 예약도 새 날짜({revised.get('date')})에 맞게 옮기셨나요?",
        })

    after = {h.get("id"): h for h in revised.get("hard_constraints") or []}
    for h in constraints:
        new = after.get(h.get("id"))
        if new is None:
            detail = f"필수 일정이 고친 일정에 없다({_event(h)})"
        elif any(new.get(k) != h.get(k) for k in ("type", "place", "time")):
            detail = f"필수 일정이 바뀌었다({_event(h)} → {_event(new)})"
        else:
            continue
        items.append({"kind": "HARD_CONSTRAINT_CHANGED", "target": h.get("id"), "detail": detail,
                      "ask_user": f"{_event(h)} 일정을 바꿔도 되나요?"})

    # 장소는 이름으로 짝을 맞춘다. 순서로 맞추면 순서만 바꿔도 장소가 바뀐 것으로 보인다.
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


def recheck_revision(original: dict, revised: dict, facts: dict, policy: dict,
                     revised_facts: dict | None = None) -> dict:
    """revised_facts 에는 고친 출발 시각으로 새로 받은 이동시간이 들어 있다."""
    before = judge(original, facts, policy)
    after = judge(revised, facts if revised_facts is None else revised_facts, policy)
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

