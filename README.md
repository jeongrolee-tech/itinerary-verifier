# itinerary-verifier

여행 일정이 실제로 실행 가능한지 검증한다. **확인한 것 / 어긋난 것 / 확인하지 못한 것**을 근거와 함께 나눠서 낸다.

> Checks whether a travel itinerary is actually executable — validates opening hours,
> closures and transit times against official sources, and **reports what it could not verify**.

## 무엇이 다른가

오류를 못 찾은 것과 문제가 없는 것은 다르다. 이 검증기는 근거를 확보하지 못하면 `pass`를 내지 않는다.

```
verdict = infeasible      확인된 위반이 있다
          undetermined    위반은 없지만 필수 검사가 미확인이다
          feasible        필요한 검사가 모두 통과했다
```

사용자에게도 "실행 가능한 일정입니다"가 아니라 **"확인한 정보와 검사 범위 내에서 위반이 발견되지 않았습니다"**라고 말한다.

## 실행

```bash
pip install anthropic pydantic

python core/run.py                 # 판정만 — API 키 불필요
python core/run.py --case T05      # 한 케이스 상세
python core/run_extract.py         # 자연어 → 추출 → 판정 (Anthropic 키 필요)
python core/compare.py --arms code # 비교 실험, 코드 arm만 — 키 불필요
python core/compare.py             # 비교 실험 3종 (Anthropic 키 필요)
```

키는 실행 시 물어본다. 화면에 안 찍히고 셸 히스토리에도 안 남는다.

## 현재 상태 (2026-09-21)

| | 결과 |
| --- | --- |
| 판정 | 15/15 (dev 9 · holdout 6) |
| 추출 | 15/15 |
| 전체 파이프라인 | 15/15 |
| **미확인을 통과로 낸 건수** | **0** |
| 비용 | 건당 $0.0098 (claude-opus-5, effort low) |
| 지연 | 중앙값 4.3초 |

실행 기록: [`core/last-run.json`](core/last-run.json) · [`core/last-extract-run.json`](core/last-extract-run.json)

> 15/15는 테스트가 쉽다는 신호일 수 있다. 라벨과 코드를 같은 사람이 썼고, holdout도 결과를 이미 본 상태다. 결과를 보지 않은 새 케이스가 필요하다.

## 구조

```
[조회 계층]   외부 API 호출 · 실패 처리      fetch_holidays.py, fetch-travel-times.mjs
     ↓        facts.json / policy.json
[추출]        extract.py — 자연어를 구조로 옮긴다. 값을 채우지 않는다
     ↓
[판정 코어]   verdict.py — 순수 함수. 네트워크 호출 없음
     ↓
[채점·기록]   run.py / run_extract.py → last-*.json
```

판정 코어를 외부 API에서 분리했다. **API 없이 고정 입력으로 테스트할 수 있어야** 하기 때문이다.

| 파일 | 역할 |
| --- | --- |
| `core/verdict.py` | 판정 코어 |
| `core/extract.py` | 자연어 → 구조화 (Claude, structured outputs) |
| `core/facts.json` | 사실 — 운영정보·이동시간·공휴일. 근거 필드 포함 |
| `core/policy.json` | 정책 — 기본값·버퍼. **전부 우리가 정한 값** |
| `core/tests.json` | 골든 테스트 15개 + 라벨 |
| `core/compare.py` | 비교 실험 — 판정 방식 3종을 같은 입력으로 돌린다 |
| `core/build_transit_legs.py` | 공공누리 지하철 데이터로 구간 주행시간 하한선 계산 |
| `core/transit-seoul-metro.csv` | 서울교통공사 역간거리·소요시간 (공공누리 1유형) |
| `docs/data-policy.md` | 제공사별 저장·캐싱·공개 범위 (약관 인용) |

## 판정 규칙

| 규칙 |
| --- |
| 근거가 없으면 `unknown` |
| 근거의 적용 기간(`valid_until`)이 지나면 못 쓴다 |
| 입장 가능과 체류 완료는 다른 검사 |
| 체류시간을 사용자가 안 말했으면 체류 검사를 안 한다 |
| 기본값을 ±50% 흔들어 판정이 뒤집히면 `unknown` |
| 근사값으로는 통과시키지 않는다 |
| 정보가 부족해도 심각도(`severity`)를 낮추지 않는다 |
| 공휴일 시간표가 요일 시간표보다 우선한다 |

## 골든 테스트

라벨은 판정 코드를 보지 않고 공식 페이지 문구에서 만들었다.

| | 케이스 | 잡아내는 것 |
| --- | --- | --- |
| T05 | 대체공휴일 연쇄 | 요일 lookup만 하는 구현 |
| T06 | 기관별 규칙 차이 | 휴무 규칙을 코드에 박는 구현 |
| T09 | 계절 경계 | 연중 같은 시간으로 처리 |
| T12 | 기본값이 판정을 흔듦 | 설정값으로 확정 오류 생성 |
| T14 | 근거 만료 | 조회 시각만 보고 적용 |
| T15 | 공휴일 시간표 | 요일만 보고 시간표 선택 |

**T05와 T06은 같은 날(2026-10-06)인데 정답이 반대다.** 궁궐은 휴궁, 미술관은 개관. 휴무 규칙을 코드에 박으면 하나는 반드시 틀린다.

## 확인된 근거

| 값 | 출처 | 확인 |
| --- | --- | --- |
| 공휴일 2026년 22건 | 공공데이터포털 특일 정보 | API 호출 + 원본 스냅샷 |
| 대중교통 이동시간 5구간 | Google Routes API | API 호출 + 원본 스냅샷 |
| 궁궐 정기휴일·예외 규칙 | 국가유산청 궁능유적본부 | 공식 페이지 화면 확인 |
| 미술관 운영시간·휴관 규칙 | 서울시립미술관 관람안내 | 공식 페이지 화면 확인 |

## 아직 안 한 것

- 도보 이동시간 (TMAP 미착수 — 도보 구간은 전부 `unknown`)
- 비교 실험 LLM arm 2종 실행 — 하니스는 있고 키를 넣어 돌리면 된다
- 라벨 재검토 8건 (`docs/label-review-260922.md`)
- 도보·환승·대기 이동시간 — 실제 이동시간의 73~85%가 여기다
- 종묘 회차 입장 검사 구현
- 수정안 생성 + 재검사
- 결과를 보지 않은 새 holdout 케이스

## 범위

서울 · 당일 · 스톱 3~6개 · 대중교통 기본.
제외 — 해외 도시 / 1박 이상 / 예약 / 예산 / 날씨 / 동선 최적화 / 다국어 / 자동차
