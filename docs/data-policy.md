# 제공사별 데이터 저장·캐싱·공개 정책

확인일 **2026-09-22** · 전제 **레포를 GitHub public 으로 공개한다**

> 이 문서는 법률 자문이 아니다. 각 제공사 약관 원문을 읽고 해당 조항을 그대로 옮긴 것이다.
> 회색지대는 회색지대라고 적었다. 확정 판단이 필요한 항목은 제공사에 직접 문의해야 한다.

판정 코어는 **고정 스냅샷**을 입력으로 받는다 (`facts.json`). 스냅샷은 레포에 커밋되고 public 으로 공개된다.
따라서 어떤 값이 `facts.json` 에 들어갈 수 있는지는 **품질 문제가 아니라 저장 권한 문제**가 먼저다.

---

## 요약

| 제공사 | 저장 허용 범위 | 공개(재배포) | 스냅샷에 넣을 수 있나 |
| --- | --- | --- | --- |
| Google Places API | `place_id` **영구** · lat/lng **30일** | ❌ | `place_id` 만 |
| Google Routes API | lat/lng **30일** | ❌ | ❌ |
| TMAP Open API | **24시간** | ❌ | ❌ |
| 공공데이터포털 특일 정보 (한국천문연구원) | **제한 없음** | ✅ | ✅ |
| 공식 페이지 사람이 읽고 옮긴 사실 (curated) | 제약 없음 | ✅ (출처 표기) | ✅ |

**결론 — 판정에 쓰는 사실은 `curated` 와 `공공데이터` 에서만 와야 한다. 상업 API 는 런타임 조회 전용이다.**

## 제약은 "사용"이 아니라 "저장"에 걸린다

혼동하기 쉬운 부분이다. **Google 대중교통 이동시간을 실서비스 판정에 쓰는 것은 문제없다.**
금지되는 것은 그 값을 남기는 것이다.

| 용도 | Google Places · Routes | TMAP | 공공데이터 · curated |
| --- | --- | --- | --- |
| 런타임에 호출해서 판정에 사용 | ✅ | ✅ | ✅ |
| 사용자에게 결과로 표시 | ✅ (`Google Maps` 표기) | ✅ | ✅ |
| 지도 없이 사용 | ✅ §14.1 · §19.1 | ✅ | ✅ |
| `facts.json` 스냅샷에 저장 | ❌ (`place_id` 만) | ❌ | ✅ |
| 골든 테스트 데이터로 사용 | ❌ §3.2.3(c)(vii) | ❌ | ✅ |
| public 레포에 커밋 | ❌ (`place_id` 만) | ❌ | ✅ |

판정 코어가 `legs` 를 **인자로** 받는 구조라 이 구분이 코드 변경 없이 성립한다.

```
[조회 계층]   런타임 Google Routes 호출 → 넘기고 버린다        ← Google 사용 가능
     ↓        legs 를 판정 코어에 인자로 넘긴다
[판정 코어]   verdict.py — 순수 함수. 출처를 가리지 않는다      ← 변경 불필요
     ↓
[테스트]      고정 입력으로 같은 코어를 돌린다                  ← 여기에만 제약이 걸린다
```

### 테스트 입력은 합성 픽스처로 만든다

판정 로직을 검증하는 데 실제 Google 값이 필요한 것이 아니다. **"이 구간 24분"이라는 입력**이
필요할 뿐이고, 그 숫자는 우리가 정하면 된다 (`source: synthetic_fixture`).

경계값을 직접 고르는 것이 실측 1건보다 나은 테스트다 — 실측은 그 값 하나만 검증하지만
경계값은 로직의 경계를 검증한다.

---

## 1. Google Maps Platform (Places API · Routes API)

### 기본 금지

[Google Maps Platform Terms of Service](https://cloud.google.com/maps-platform/terms/) §3.2.3 *Restrictions Against Misusing the Services*

> **(a) No Scraping.** Customer will not export, extract, or otherwise scrape Google Maps Content for use
> outside the Services. For example, Customer will not: (i) pre-fetch, index, **store**, reshare, or rehost
> Google Maps Content outside the services; (ii) bulk download Google Maps tiles, Street View images,
> geocodes, **directions, distance matrix results**, roads information, **places information**, elevation
> values, and time zone details; (iii) **copy and save business names, addresses**, or user reviews (…)

> **(b) No Caching.** Customer will not cache Google Maps Content **except as expressly permitted under the
> Maps Service Specific Terms.**

> **(c) No Creating Content From Google Maps Content.** (…) (vii) use Google Maps Content to improve machine
> learning and artificial intelligence models, including to **train, test, validate** or fine-tune the models.

### 명시적으로 허용된 예외 — 이게 전부다

[Google Maps Platform Service Specific Terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)

| 조항 | 허용 내용 |
| --- | --- |
| §A.3 *Google ID Caching* | "Customer may cache (a) `place_id` from Places API, Directions API, Geolocation API and Routes API (…)" |
| §14.3 *Places API — Caching* | "Customer may temporarily cache **latitude and longitude values** from the Places API for up to **30 consecutive calendar days**, after which Customer must delete the cached latitude and longitude values." |
| §19.3 *Routes API — Caching* | "Customer may temporarily cache **latitude (lat) and longitude (lng) values** from the Routes API for up to **30 consecutive calendar days**, after which Customer must delete (…)" |

관련 참고 — [Places API Policies](https://developers.google.com/maps/documentation/places/web-service/policies):
"The place ID (…) is exempt from the caching restrictions. You can therefore store place ID values indefinitely."

### 우리 프로젝트에 대한 판단

허용 목록에 **운영시간·이름·주소·소요시간·거리가 없다.** §3.2.3(b) 는 "명시적으로 허용된 것 외에는 캐싱 금지"이므로,
목록에 없는 값은 기본 금지에 걸리는 것으로 읽는 것이 안전하다.

| 값 | 판단 | 근거 |
| --- | --- | --- |
| `place_id` | ✅ 영구 저장 | §A.3 |
| 시설 lat/lng | ⚠️ 30일 | §14.3 · 스냅샷은 30일 넘게 산다 → 사실상 불가 |
| `regularOpeningHours` / `weekday_descriptions` | ❌ | 허용 목록에 없음 |
| 시설명 · 주소 | ❌ | §3.2.3(a)(iii) 에 직접 예시로 적혀 있음 |
| Routes `duration` / `distanceMeters` / steps | ❌ | 허용 목록에 없음. (a)(ii) "directions, distance matrix results" |
| 위 값에서 파생한 `minutes: 24` | ❌ 로 본다 | 반올림해도 응답의 실질. 확정 판단 필요하면 Google 에 문의 |
| **평가 데이터셋 용도 자체** | ❌ | §3.2.3(c)(vii) "test, validate" — 골든 테스트가 정확히 이 용도 |

`No use with a non-Google map` (§14.2 · §19.2) 도 걸려 있다. 다만 §14.1 · §19.1 이
"지도 없이 쓰는 것"은 허용하므로, 지도를 아예 띄우지 않는 현재 설계는 이 조항엔 안 걸린다.

### 허용되는 사용법

- 런타임에 호출해서 사용자에게 보여주고 **저장하지 않는다**
- `place_id` 로 장소를 식별하고 **그 id만** 남긴다
- 공식 정보와 대조할 때 호출하고, **일치/불일치 여부만** 문장으로 남긴다. 원문 값은 남기지 않는다
- 표기: `Google Maps` 명시 (Places API Policies 의 attribution 요구)

---

## 2. TMAP Open API

[TMAP API 약관](https://tmapapi.tmapmobility.com/terms.html) — 준수사항/제약사항

> TMAP Open API를 이용하여 얻어진 데이터는 **저장 후 24시간 이상 사용할 수 없습니다.**

> 동일한 서비스를 위하여 다수의 프로젝트를 생성하여 사용하는 경우에는 불법 사용으로 간주되어
> 사용이 중지될 수 있습니다.

**24시간.** Google(30일)보다 엄격하다. 고정 스냅샷 전제와 정면으로 충돌한다.

→ **도보 이동시간을 TMAP 으로 `facts.json` 에 채우는 계획은 성립하지 않는다.** 런타임 조회 전용으로만 가능하다.

---

## 3. 공공데이터포털 — 한국천문연구원 특일 정보

[공공데이터포털 15012690](https://www.data.go.kr/tcs/dss/selectApiDataDetailView.do?publicDataPk=15012690)

| 항목 | 값 |
| --- | --- |
| 제공기관 | 한국천문연구원 |
| 이용허락범위 | **제한 없음** |
| 비용 | 무료 |

→ `holidays-2026.json` 은 **저장·커밋·공개 전부 문제없다.** 출처 표기만 유지한다.

---

## 4. 공식 페이지 curated (국가유산청 · 서울시립미술관)

사람이 공식 페이지를 읽고 사실을 옮긴 것이다. API 약관의 대상이 아니다.

- `rule_text` 는 공식 안내 문구의 인용이다. 출처 URL·확인일·확인 방법을 반드시 함께 남긴다
- 각 기관의 공공누리 유형은 아직 확인하지 않았다 → **미확인 항목** (아래)

→ `facts.json` 의 `places` 는 전부 이쪽이다. **저장·공개 문제없다.**

---

## 5. 스냅샷 작성 규칙

`facts.json` 에 값을 넣기 전에 확인한다.

```
1. 이 값의 출처가 무엇인가          → source 필드에 반드시 적는다
2. 그 출처가 30일 넘는 저장을 허용하나  → 아니면 넣지 않는다
3. public 재배포를 허용하나          → 아니면 넣지 않는다
4. 허용하면                        → 출처·URL·확인일·적용기간(valid_until) 을 함께 넣는다
```

| `source` 값 | 스냅샷 허용 | 뜻 |
| --- | --- | --- |
| `curated` | ✅ | 사람이 공식 페이지에서 확인해 옮김 |
| `public_data` | ✅ | 공공데이터포털 (이용허락범위 제한 없음) |
| `field_measured` | ✅ | 사람이 실제로 이동해서 측정 |
| `google_place_id` | ✅ | `place_id` 값 자체만 |
| `api_runtime` | ❌ | 런타임 조회 전용. 스냅샷에 남기지 않는다 |

---

## 6. 아직 확인하지 않은 것

- 국가유산청 궁능유적본부 · 서울시립미술관 페이지의 **공공누리 유형** (제1~4유형 중 무엇인지)
- 이동시간 대체 출처의 약관 — 국토교통부 TAGO 대중교통정보, 서울시 열린데이터광장, ODsay
- Google 파생값(`minutes: 24`)이 캐싱에 해당하는지에 대한 Google 측 확답
