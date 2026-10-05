"""
판정 결과를 사용자에게 보여줄 묶음으로 나눈다 — 2차 피드백 2번.

    어긋난 것 · 확인하지 못한 것 · 확인한 것 · 해당 없음 · 검사하지 않는 것

판정은 verdict.py 가 하고, 여기서는 그 결과를 읽기만 한다. judge() 결과를
바꾸지 않으므로 골든 라벨과 채점에는 영향이 없다.
"""

from __future__ import annotations

from verdict import EVENT_NAMES, FAIL, NA, PASS, UNKNOWN

CHECK_NAMES = {
    "CLOSED_DAY": "휴무일",
    "ADMISSION_NOT_POSSIBLE": "입장",
    "DWELL_NOT_COMPLETABLE": "체류 완료",
    "INSUFFICIENT_TRAVEL_TIME": "이동",
    "INPUT_REQUIRED": "입력",
    "INPUT_CONFLICT": "입력",
}

# 고칠 것이 먼저 보이도록 어긋난 것부터 적는다.
# ⭐ 해당 없음도 보여준다. "입장 여부가 확정되지 않아 체류 검사가 성립하지 않는다" 같은
#    이유를 숨기면, 검사가 불필요하다는 주장을 근거 없이 하는 셈이 된다.
GROUPS = [
    (FAIL, "어긋난 것", "✕"),
    (UNKNOWN, "확인하지 못한 것", "?"),
    (PASS, "확인한 것", "○"),
    (NA, "해당 없음", "–"),
]

# ⭐ 검사하지 않는 것을 결과마다 붙인다. feasible 은 "이 목록을 뺀 범위에서 위반을
#    찾지 못했다" 는 뜻이다. 목록이 없으면 통과한 결과만 보고 식당 대기나 예약까지
#    확인한 것으로 읽는다. README 지원 범위의 '검사하지 않는 것' 칸과 글자까지 같아야
#    한다 — tests/test_report.py 가 대조한다.
NOT_CHECKED = [
    "식당 대기시간 · 혼잡도 — 공식 출처가 없다",
    "당일 임시 휴무 · 행사 · 현장 상황 — 근거는 확인한 날의 공식 안내 페이지다",
    "예약 필요 여부 · 매진 · 가격 · 날씨",
    "동선 최적화 · 수정안 자동 생성 — 사용자가 고친 일정을 다시 검사하는 것까지만 한다",
]


def _item(id_: str | None, title: str, result: dict) -> dict:
    return {
        "id": id_,
        "title": title,
        "reason": result.get("detail") or result.get("reason"),
        "notice": result.get("notice"),
        "ask_user": (result.get("how_to_resolve") or {}).get("user"),
    }


def _rows(result: dict) -> list[tuple[str, dict]]:
    rows = [(c["status"], _item(c.get("id"), f"{c['target']} {CHECK_NAMES.get(c['type'], c['type'])}", c))
            for c in result["checks"]]
    for h in result["hard_constraints"]:
        e, p = h["event"], h["policy"]
        kind = EVENT_NAMES.get(e["type"], "필수 조건")
        name = "필수 조건" if kind == "필수 조건" else f"필수 조건 {kind}"
        rows.append((p["status"], _item(h.get("id"), f"{name} {e['time']} {e['place']}", p)))
    return rows


def report(result: dict) -> dict:
    """judge() 결과를 상태별 묶음으로 나눈다. result 는 바꾸지 않는다."""
    rows = _rows(result)
    return {
        "verdict": result["summary"]["verdict"],
        "message": result["summary"]["message"],
        "groups": [{"status": status, "title": title,
                    "items": [item for s, item in rows if s == status]}
                   for status, title, _ in GROUPS],
        "not_checked": list(NOT_CHECKED),
    }


def render(rep: dict) -> str:
    """report() 결과를 화면용 글로 만든다. 빈 묶음은 적지 않는다."""
    marks = {status: mark for status, _, mark in GROUPS}
    lines = [f"[결과]  {rep['verdict']} — {rep['message']}"]
    for group in rep["groups"]:
        if not group["items"]:
            continue
        lines += ["", f"  {group['title']} ({len(group['items'])})"]
        for it in group["items"]:
            lines.append(f"    {marks[group['status']]} {it['title']}"
                         + (f" — {it['reason']}" if it["reason"] else ""))
            if it["notice"]:
                lines.append(f"        안내: {it['notice']}")
            if it["ask_user"]:
                lines.append(f"        물어볼 것: {it['ask_user']}")
    lines += ["", "  검사하지 않는 것", *(f"    {x}" for x in rep["not_checked"])]
    return "\n".join(lines)
