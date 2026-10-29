#!/usr/bin/env python3
"""지하철 운행시간으로 구간 이동시간의 하한선을 만든다. 도보와 환승, 대기는 빠져 있다."""

from __future__ import annotations

import csv
import heapq
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent

SOURCE = {
    "name": "서울교통공사 역간거리 및 소요시간",
    "provider": "서울교통공사",
    "portal": "서울 열린데이터광장 OA-12034",
    "url": "https://data.seoul.go.kr/dataList/OA-12034/F/1/datasetView.do",
    "license": "공공누리 1유형 (출처표시, 상업적 이용 및 변경 가능)",
    "data_updated": "2024-09-02",
    "checked_at": "2026-09-22",
    "caveat": "열차가 출발하여 다음역에 도착하는 표준 운행시간. 열차사정에 따라 변동될 수 있다",
}

# 사람이 고른 가장 가까운 역이다. 출입구 기준으로는 아직 확인하지 않았다.
NEAREST_STATION = {
    "경복궁": ("3", "경복궁"),
    "서울시립미술관 서소문본관": ("1", "시청"),
    "덕수궁": ("1", "시청"),
    "창덕궁": ("3", "안국"),
    "광장시장": ("1", "종로5가"),
    "서울역": ("1", "서울역"),
}


def load_lines(path: Path) -> dict[str, list[tuple[str, int]]]:
    """호선별로 (역명, 앞 역에서 오는 초) 를 모은다. CSV 순서가 노선 순서다."""
    lines: dict[str, list[tuple[str, int]]] = defaultdict(list)
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mm, ss = row["소요시간"].strip().split(":")
            lines[row["호선"].strip()].append(
                (row["역명"].strip(), int(mm) * 60 + int(ss))
            )
    return dict(lines)


def build_graph(lines: dict[str, list[tuple[str, int]]]):
    graph: dict[tuple[str, str], list[tuple[tuple[str, str], int, str]]] = defaultdict(list)
    by_station: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for line, seq in lines.items():
        for i, (name, _) in enumerate(seq):
            node = (line, name)
            by_station[name].append(node)
            if i + 1 < len(seq):
                nxt_name, ride = seq[i + 1]
                nxt = (line, nxt_name)
                graph[node].append((nxt, ride, "ride"))
                graph[nxt].append((node, ride, "ride"))

    # 환승 비용 0 은 공짜가 아니라 미확인이다. 그래서 결과가 하한선이 된다.
    for nodes in by_station.values():
        for a in nodes:
            for b in nodes:
                if a != b:
                    graph[a].append((b, 0, "transfer"))
    return graph


def shortest(graph, start, goal):
    pq = [(0, 0, start, [start])]
    best: dict[tuple[str, str], int] = {}
    while pq:
        cost, transfers, node, path = heapq.heappop(pq)
        if node == goal:
            return cost, transfers, path
        if node in best and best[node] <= cost:
            continue
        best[node] = cost
        for nxt, w, kind in graph[node]:
            heapq.heappush(pq, (cost + w, transfers + (kind == "transfer"),
                                nxt, path + [nxt]))
    return None, None, None


def compute(pairs: list[tuple[str, str]]) -> dict:
    lines = load_lines(HERE / "transit-seoul-metro.csv")
    graph = build_graph(lines)
    out = {}
    for frm, to in pairs:
        if frm not in NEAREST_STATION or to not in NEAREST_STATION:
            continue
        a, b = NEAREST_STATION[frm], NEAREST_STATION[to]
        secs, transfers, path = shortest(graph, a, b)
        if secs is None:
            continue
        ride_min = round(secs / 60, 1)
        out[f"{frm}|{to}"] = {
            "minutes": ride_min,
            "is_lower_bound": True,
            "mode": "metro",
            "method": "metro_ride_only",
            "source": "public_data",
            "from_station": f"{a[1]}({a[0]})",
            "to_station": f"{b[1]}({b[0]})",
            "transfers": transfers,
            "excluded": ["장소→역 도보", "환승 이동시간", "열차 대기시간"],
            "note": f"지하철 주행시간만 {ride_min}분. 도보·환승·대기가 빠져 있어 "
                    f"실제 이동시간은 이보다 크다",
            "license": SOURCE["license"],
            "url": SOURCE["url"],
            "checked_at": SOURCE["checked_at"],
            "valid_until": "2026-12-31",
        }
    return out


PAIRS = [
    ("서울시립미술관 서소문본관", "경복궁"),
    ("경복궁", "서울시립미술관 서소문본관"),
    ("경복궁", "광장시장"),
    ("광장시장", "서울역"),
]

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    legs = compute(PAIRS)
    for key, leg in legs.items():
        print(f"{key}")
        print(f"   {leg['from_station']} → {leg['to_station']}  "
              f"환승 {leg['transfers']}회")
        print(f"   주행시간 하한선 {leg['minutes']}분  (빠진 것: "
              f"{', '.join(leg['excluded'])})")

    if "--write" in sys.argv:
        fp = HERE / "facts.json"
        facts = json.loads(fp.read_text(encoding="utf-8"))
        facts["legs"] = legs
        facts["transit_source"] = SOURCE
        fp.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nfacts.json 의 legs 를 교체했다 ({len(legs)}구간)")
