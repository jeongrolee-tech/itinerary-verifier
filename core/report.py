"""판정 결과를 화면에 보여 줄 묶음과 줄로 나눈다. 판정 결과는 읽기만 한다."""

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

# 화면에 쓰는 말이다. 코드와 데이터에는 영어 값이 그대로 남는다.
VERDICT_NAMES = {"feasible": "가능", "infeasible": "불가능", "undetermined": "보류"}
STATUS_NAMES = {PASS: "통과", FAIL: "위반", UNKNOWN: "미확인", NA: "해당 없음"}
SEVERITY_NAMES = {"blocking": "치명", "warning": "주의"}
SET_NAMES = {"dev": "연습용", "holdout": "시험용", "new": "새 평가"}
ARM_NAMES = {"llm_naive": "LLM에 그냥 물음", "llm_only": "LLM에 '모르면 모른다' 지시",
             "llm_with_facts": "LLM에 사실 데이터 줌", "code": "판정 코드"}
SOURCE_NAMES = {"curated": "공식 페이지에서 옮김", "public_data": "공공데이터", "api_runtime": "TMAP 실시간 조회",
                "synthetic_fixture": "테스트용 가상 값", "field_measured": "직접 측정",
                "user_stated": "사용자", "system_default": "우리 기준"}
INPUT_SOURCE_NAMES = {"explicit": "말함", "inferred": "추정", "missing": "없음"}
RESOLVER_NAMES = {"user": "사용자", "system": "시스템"}


def width(text) -> int:
    """터미널에서 차지하는 칸 수를 센다. 한글은 두 칸이다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def pad(text, cols: int) -> str:
    """칸 수로 채운다. len() 으로 채우면 한글이 섞인 열이 어긋난다."""
    return f"{text}" + " " * max(cols - width(text), 1)

# 고칠 것이 먼저 보이게 어긋난 것부터 적는다.
# 해당 없음도 이유와 함께 보여 준다. 숨기면 검사를 건너뛴 근거가 사라진다.
GROUPS = [
    (FAIL, "어긋난 것", "✕"),
    (UNKNOWN, "확인하지 못한 것", "?"),
    (PASS, "확인한 것", "○"),
    (NA, "해당 없음", "–"),
]

# 검사하지 않는 것을 결과마다 붙인다. 가능은 이 목록 밖에서 위반을 못 찾았다는 뜻이다.
NOT_CHECKED = [
    "식당 대기시간 · 혼잡도 — 공식 출처가 없다",
    "당일 임시 휴무 · 행사 · 현장 상황 — 근거는 확인한 날의 공식 안내 페이지다",
    "예약 필요 여부 · 매진 · 가격 · 날씨",
    "사용자가 고른 이동수단으로 되는지 — 대중교통 · 도보 · 택시 중 하나로라도 갈 수 있으면 된다고 본다. 택시를 안 탄다고 했으면 택시만 뺀다",
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
        rows.append((p["status"], _item(h.get("id"), f"{name} {e['time'] or '시각 모름'} {e['place'] or '장소 모름'}", p)))
    return rows


def report(result: dict) -> dict:
    """judge() 결과를 상태별 묶음으로 나눈다."""
    rows = _rows(result)
    return {
        "verdict": result["summary"]["verdict"],
        "message": result["summary"]["message"],
        "groups": [{"status": status, "title": title,
                    "items": [item for s, item in rows if s == status]}
                   for status, title, _ in GROUPS],
        "not_checked": list(NOT_CHECKED),
    }


# 줄의 상태는 그 줄 검사 중 가장 나쁜 것이다.
WORST_FIRST = [FAIL, UNKNOWN, PASS, NA]
INPUT_CHECKS = ("INPUT_REQUIRED", "INPUT_CONFLICT")
PLACE_CHECKS_IN_ORDER = ("CLOSED_DAY", "ADMISSION_NOT_POSSIBLE", "DWELL_NOT_COMPLETABLE")


def _line(c: dict, name: str) -> dict:
    return {"id": c.get("id"), "name": name, "status": c["status"],
            "reason": c.get("detail") or c.get("reason"), "notice": c.get("notice")}


def _row(kind: str, time: str | None, title: str, lines: list[dict]) -> dict:
    status = next((s for s in WORST_FIRST if any(x["status"] == s for x in lines)), None)
    return {"kind": kind, "time": time, "title": title, "status": status, "lines": lines}


def _window_time(window: dict | None) -> str | None:
    """시각을 말하지 않은 장소의 시각 칸이다. 판정 코드가 계산한 시간대를 쓴다."""
    if not window:
        return None
    return f"{window['from']}~{window['to']}" + (" 전" if window["before"] else "")


def timeline(itinerary: dict, result: dict) -> list[dict]:
    """장소와 이동을 일정 순서대로 한 줄씩 적는다. 문제가 난 장소를 바로 찾게 한다."""
    pending = list(result["checks"])

    def take(type_: str, target: str) -> dict | None:
        # 같은 장소를 두 번 가면 대상 이름이 겹쳐 앞에서부터 하나씩 가져간다.
        c = next((c for c in pending if c["type"] == type_ and c["target"] == target), None)
        if c is not None:
            pending.remove(c)
        return c

    rows, stops = [], itinerary.get("stops") or []
    for i, s in enumerate(stops):
        taken = [(t, c) for t in INPUT_CHECKS + PLACE_CHECKS_IN_ORDER if (c := take(t, s["place"]))]
        lines = [_line(c, CHECK_NAMES[t]) for t, c in taken]
        window = next((c["window"] for _, c in taken if c.get("window")), None)
        rows.append(_row("stop", s.get("start") or _window_time(window), s["place"], lines))
        if i + 1 < len(stops):
            target = f"{s['place']} → {stops[i + 1]['place']}"
            c = take("INSUFFICIENT_TRAVEL_TIME", target)
            rows.append(_row("travel", None, target, [_line(c, "이동")] if c else []))
    # 남은 것은 날짜 같은 입력 검사다. 마지막 장소 뒤 '(없음)' 구간은 일정에 없어 뺀다.
    rest = [c for c in pending if not c["target"].endswith("→ (없음)")]
    if rest:
        rows.insert(0, _row("input", None, "일정 전체",
                            [_line(c, f"{c['target']} {CHECK_NAMES.get(c['type'], c['type'])}") for c in rest]))
    for h in result["hard_constraints"]:
        e, p = h["event"], h["policy"]
        kind = EVENT_NAMES.get(e["type"], "필수 일정")
        rows.append(_row("hard", e["time"], f"필수 일정 · {kind} {e['place'] or '장소 모름'}",
                         [_line({**p, "id": h.get("id")}, kind)]))
    return rows


# 장소 검사의 질문에 장소 이름이 없으면 앞에 붙인다.
PLACE_CHECKS = {"CLOSED_DAY", "ADMISSION_NOT_POSSIBLE", "DWELL_NOT_COMPLETABLE"}


def questions(result: dict) -> list[str]:
    """물을 말을 한 번씩만 모은다. 여러 검사가 같은 것을 모를 때가 많다."""
    asks = {a["id"]: a["ask_user"] for a in result.get("assumptions", [])}
    rows = [(c, c["target"] if c["type"] in PLACE_CHECKS else None) for c in result["checks"]]
    rows += [(h["policy"], None) for h in result["hard_constraints"]]
    out: list[str] = []
    for r, place in rows:
        ask = (r.get("how_to_resolve") or {}).get("user")
        if not ask:
            continue
        # 판정이 기댄 가정이 있으면 그 가정의 질문으로 묻는다.
        found = [asks[a] for a in r.get("depends_on_assumptions") or [] if a in asks]
        for q in found or [f"{place} — {ask}" if place and place not in ask else ask]:
            if q not in out:
                out.append(q)
    return out


def render(rep: dict) -> str:
    """report() 결과를 화면용 글로 만든다."""
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
