"""
판정 결과를 사용자에게 보여줄 묶음으로 나눈다 — 2차 피드백 2번.

    어긋난 것 · 확인하지 못한 것 · 확인한 것 · 해당 없음 · 검사하지 않는 것

판정은 verdict.py 가 하고, 여기서는 그 결과를 읽기만 한다. judge() 결과를
바꾸지 않으므로 골든 라벨과 채점에는 영향이 없다.
"""

from __future__ import annotations

import unicodedata

from verdict import EVENT_NAMES, FAIL, NA, PASS, UNKNOWN

CHECK_NAMES = {
    "CLOSED_DAY": "휴무일",
    "ADMISSION_NOT_POSSIBLE": "입장",
    "DWELL_NOT_COMPLETABLE": "체류 완료",
    "INSUFFICIENT_TRAVEL_TIME": "이동",
    "INPUT_REQUIRED": "입력",
    "INPUT_CONFLICT": "입력",
}

# 읽는 말 — 코드와 데이터에는 영어 값이 남고, 화면과 문서에는 이 말을 쓴다(docs/용어.md '읽는 말과 코드 값').
# 화면에 찍는 run.py · run_extract.py · compare.py · eval_report.py · labels.py 가 여기서 가져다 쓴다
VERDICT_NAMES = {"feasible": "가능", "infeasible": "불가능", "undetermined": "보류"}
STATUS_NAMES = {PASS: "통과", FAIL: "위반", UNKNOWN: "미확인", NA: "해당 없음"}
SEVERITY_NAMES = {"blocking": "치명", "warning": "주의"}
SET_NAMES = {"dev": "연습용", "holdout": "시험용"}
ARM_NAMES = {"llm_naive": "LLM에 그냥 물음", "llm_only": "LLM에 '모르면 모른다' 지시",
             "llm_with_facts": "LLM에 사실 데이터 줌", "code": "판정 코드"}
SOURCE_NAMES = {"curated": "공식 페이지에서 옮김", "public_data": "공공데이터", "api_runtime": "TMAP 실시간 조회",
                "synthetic_fixture": "테스트용 가상 값", "field_measured": "직접 측정",
                "user_stated": "사용자", "system_default": "우리 기준"}
INPUT_SOURCE_NAMES = {"explicit": "말함", "inferred": "추정", "missing": "없음"}
RESOLVER_NAMES = {"user": "사용자", "system": "시스템"}


def width(text) -> int:
    """터미널에서 차지하는 칸 수. 한글처럼 넓은 글자는 두 칸이다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def pad(text, cols: int) -> str:
    """칸 수 기준으로 오른쪽을 채운다. len() 으로 채우면 한글이 섞인 표의 열이 어긋난다."""
    return f"{text}" + " " * max(cols - width(text), 1)

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
    "사용자가 고른 이동수단으로 되는지 — 대중교통 · 도보 · 택시 중 하나로라도 갈 수 있으면 된다고 본다",
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
        kind = EVENT_NAMES.get(e["type"], "필수 일정")
        name = "필수 일정" if kind == "필수 일정" else f"필수 일정 {kind}"
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


# 장소 검사 — 질문에 장소 이름이 없으면("방문할 점포를 알려주세요") 앞에 붙인다
PLACE_CHECKS = {"CLOSED_DAY", "ADMISSION_NOT_POSSIBLE", "DWELL_NOT_COMPLETABLE"}


def questions(result: dict) -> list[str]:
    """사용자에게 물을 말을 한 번씩만 모은다. result 는 바꾸지 않는다 (데모 화면 core/app.py 가 쓴다).

    여러 검사가 같은 것을 모를 때가 많다. T12 는 경복궁 체류시간을 몰라 체류 검사가 "얼마나 머무르실",
    다음 구간이 "몇 시에 나오실" 로 묻고, 광장시장 휴무일과 입장이 똑같이 점포를 묻는다 — 질문 넷, 답할 것 둘.
    판정이 기댄 가정(체류시간 기본값 · 추정한 날짜와 시각)이 있으면 그 가정의 질문으로 묻고, 같은 말은 한 번만 적는다.
    """
    asks = {a["id"]: a["ask_user"] for a in result.get("assumptions", [])}
    rows = [(c, c["target"] if c["type"] in PLACE_CHECKS else None) for c in result["checks"]]
    rows += [(h["policy"], None) for h in result["hard_constraints"]]
    out: list[str] = []
    for r, place in rows:
        ask = (r.get("how_to_resolve") or {}).get("user")
        if not ask:
            continue
        found = [asks[a] for a in r.get("depends_on_assumptions") or [] if a in asks]
        for q in found or [f"{place} — {ask}" if place and place not in ask else ask]:
            if q not in out:
                out.append(q)
    return out


def render(rep: dict) -> str:
    """report() 결과를 화면용 글로 만든다. 빈 묶음은 적지 않는다."""
    marks = {status: mark for status, _, mark in GROUPS}
    lines = [f"[결과]  {VERDICT_NAMES.get(rep['verdict'], rep['verdict'])} — {rep['message']}"]
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
