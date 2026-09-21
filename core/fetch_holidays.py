#!/usr/bin/env python3
"""
공휴일 캘린더 확보 — 한국천문연구원 특일 정보 (공공데이터포털)

    python core/fetch_holidays.py
    → 실행하면 서비스키를 물어본다 (입력값은 화면에 안 찍힌다)

getRestDeInfo 로 연 단위 공휴일을 받아서
  - core/holidays-2026.json  원본 응답 스냅샷
  - core/facts.json 의 holidays 갱신 (verified=true)
까지 한다.

대체공휴일이 여기 들어 있어야 궁궐 휴무일 예외 규칙을 판정할 수 있다.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
YEAR = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 2026
ENDPOINT = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"


def ask_key() -> str:
    for a in sys.argv:
        if a.startswith("--key="):
            return a[6:].strip()
    try:
        return getpass("공공데이터포털 서비스키 입력: ").strip()
    except Exception:
        return input("공공데이터포털 서비스키 입력: ").strip()


def build_url(key: str, month: int) -> str:
    # 포털이 인증키를 두 형태로 준다.
    #   Encoding 키 : 이미 URL 인코딩됨 (% 포함) → 그대로 붙인다
    #   Decoding 키 : 원문 → 우리가 인코딩한다
    # 잘못 고르면 SERVICE_KEY_IS_NOT_REGISTERED_ERROR 가 난다.
    service_key = key if "%" in key else urllib.parse.quote(key, safe="")
    params = urllib.parse.urlencode(
        {"solYear": YEAR, "solMonth": f"{month:02d}", "numOfRows": 100, "_type": "json"}
    )
    return f"{ENDPOINT}?serviceKey={service_key}&{params}"


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as res:
        raw = res.read().decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 키가 안 풀리면 JSON 요청이어도 XML 에러가 돌아온다
        raise SystemExit(
            "JSON 이 아닌 응답이 왔다. 서비스키 형태(Encoding/Decoding)를 바꿔서 다시 시도해봐라.\n"
            "발급 직후에는 몇 분 걸리기도 한다.\n\n" + raw[:600]
        )


def items_of(payload: dict) -> list[dict]:
    body = payload.get("response", {}).get("body", {})
    items = body.get("items")
    if not items:
        return []
    item = items.get("item") if isinstance(items, dict) else items
    if item is None or item == "":
        return []
    return item if isinstance(item, list) else [item]


key = ask_key()
if not key:
    raise SystemExit("키가 비어 있다.")

print(f"\n{YEAR}년 공휴일 조회 중...\n")

raw_by_month: dict[str, dict] = {}
records: list[dict] = []

for month in range(1, 13):
    payload = fetch(build_url(key, month))
    header = payload.get("response", {}).get("header", {})
    if header.get("resultCode") not in ("00", "0"):
        raise SystemExit(f"{month}월 조회 실패: {header.get('resultCode')} {header.get('resultMsg')}")
    raw_by_month[f"{month:02d}"] = payload
    for it in items_of(payload):
        locdate = str(it["locdate"])
        records.append({
            "date": f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:]}",
            "name": it.get("dateName"),
            "is_holiday": it.get("isHoliday") == "Y",
        })

records.sort(key=lambda r: r["date"])
holiday_dates = sorted({r["date"] for r in records if r["is_holiday"]})

for r in records:
    flag = "휴일" if r["is_holiday"] else "    "
    print(f"  {r['date']}  {flag}  {r['name']}")

# 대체공휴일은 이름에 '대체'가 들어간다. 이게 없으면 케이스 4를 풀 수 없다.
substitutes = [r for r in records if r["is_holiday"] and "대체" in (r["name"] or "")]
print(f"\n공휴일 {len(holiday_dates)}건 / 그중 대체공휴일 {len(substitutes)}건")
for s in substitutes:
    print(f"  ↳ {s['date']}  {s['name']}")

checked_at = datetime.now(timezone.utc).isoformat()
snapshot = {
    "snapshot_id": f"holidays-{YEAR}-{datetime.now().strftime('%Y%m%d')}",
    "year": YEAR,
    "checked_at": checked_at,
    "source": "공공데이터포털 한국천문연구원 특일 정보 getRestDeInfo",
    "url": "https://www.data.go.kr/data/15012690/openapi.do",
    "records": records,
    "raw_responses": raw_by_month,
}
snap_path = HERE / f"holidays-{YEAR}.json"
snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

facts_path = HERE / "facts.json"
facts = json.loads(facts_path.read_text(encoding="utf-8"))
before = facts["holidays"]
facts["holidays"] = {
    "verified": True,
    "source": "공공데이터포털 한국천문연구원 특일 정보 (getRestDeInfo)",
    "url": "https://www.data.go.kr/data/15012690/openapi.do",
    "checked_at": checked_at,
    "valid_from": f"{YEAR}-01-01",
    "valid_until": f"{YEAR}-12-31",
    "snapshot_id": snapshot["snapshot_id"],
    "dates": holiday_dates,
}
facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"\n스냅샷 저장: {snap_path.name}")
print(f"facts.json 갱신: holidays.verified {before['verified']} → True")
print(f"  손으로 넣었던 값 {before['dates']}")
print(f"  실제 조회한 값  {holiday_dates}")
print("\n이제 python core/run.py 를 다시 돌려라.")
