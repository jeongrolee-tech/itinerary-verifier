#!/usr/bin/env node
/**
 * Google Places API (New) — 영업시간 채움률 및 휴무일 정확도 검증
 *
 *   $env:GKEY = "발급받은키"
 *   node check-places-api.mjs
 *
 * 확인하는 것
 *   1. regularOpeningHours 필드가 채워져 있는가 (채움률)
 *   2. 채워져 있다면 그 값이 공식 출처와 일치하는가 (정확도)
 *
 * 2번이 더 중요하다. 필드가 있어도 틀리면 쓸 수 없다.
 *
 * 결과는 places-api-check.json 으로 저장된다.
 */

import { writeFileSync } from "node:fs";

const KEY = process.env.GKEY;
if (!KEY) {
  console.error('GKEY 환경변수가 없다.  $env:GKEY = "..."');
  process.exit(1);
}

const ENDPOINT = "https://places.googleapis.com/v1/places:searchText";
const REQUEST_TIMEOUT_MS = 20000;

// Places API의 day: 0=일, 1=월, 2=화, 3=수, 4=목, 5=금, 6=토
const DAY_KO = ["일", "월", "화", "수", "목", "금", "토"];

/**
 * expectedClosedDay: 공식 출처로 확인된 정기휴일 (숫자 = 위 day 코드)
 *   null 이면 정답을 모르는 상태 — 채움률만 보고 정확도는 판정하지 않는다.
 */
const TARGETS = [
  { query: "경복궁",                  expectedClosedDay: 2,    source: "국가유산청 궁능유적본부", note: "9~10월 09:00-18:00, 입장마감 17:00" },
  { query: "창덕궁",                  expectedClosedDay: 1,    source: "국가유산청 궁능유적본부" },
  { query: "덕수궁",                  expectedClosedDay: 1,    source: "국가유산청 궁능유적본부" },
  { query: "창경궁",                  expectedClosedDay: 1,    source: "국가유산청 궁능유적본부" },
  { query: "종묘",                    expectedClosedDay: 2,    source: "국가유산청 궁능유적본부" },
  { query: "서울시립미술관 서소문본관", expectedClosedDay: 1,    source: "sema.seoul.go.kr" },
  // 정답 미확보 — 채움률만 본다
  { query: "국립중앙박물관",           expectedClosedDay: null },
  { query: "국립민속박물관",           expectedClosedDay: null },
  { query: "남산서울타워",             expectedClosedDay: null },
  { query: "광장시장",                 expectedClosedDay: null },
  { query: "통인시장",                 expectedClosedDay: null, note: "소규모 — 채움률 하한 테스트" },
  { query: "북촌한옥마을",             expectedClosedDay: null, note: "상시개방 — 영업시간 개념 없음" },
];

const FIELD_MASK = [
  "places.id",
  "places.displayName",
  "places.formattedAddress",
  "places.location",
  "places.businessStatus",
  "places.regularOpeningHours",
].join(",");

async function search(textQuery) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": FIELD_MASK,
      },
      body: JSON.stringify({
        textQuery,
        languageCode: "ko",
        regionCode: "KR",
        maxResultCount: 1,
      }),
      signal: controller.signal,
    });
  } catch (error) {
    const timedOut = error?.name === "AbortError";
    return {
      error: timedOut ? `${REQUEST_TIMEOUT_MS}ms 안에 응답하지 않음` : String(error?.message ?? error),
      failure_type: timedOut ? "TIMEOUT" : "NETWORK_ERROR",
      http: null,
    };
  } finally {
    clearTimeout(timer);
  }

  const raw = await res.text();
  let json;
  try {
    json = JSON.parse(raw);
  } catch {
    return {
      error: "응답이 JSON 형식이 아님",
      failure_type: "UNPARSEABLE_RESPONSE",
      http: res.status,
      raw,
    };
  }

  if (!res.ok) {
    return {
      error: json?.error?.message ?? raw.slice(0, 200),
      failure_type: "HTTP_ERROR",
      http: res.status,
      raw: json,
    };
  }
  return { place: json?.places?.[0] ?? null, failure_type: json?.places?.length ? null : "NO_RESULT", http: res.status, raw: json };
}

/** periods 에서 실제로 여는 요일 집합을 뽑는다 */
function openDaysFrom(hours) {
  if (!hours?.periods?.length) return null;
  const days = new Set();
  for (const p of hours.periods) {
    if (typeof p?.open?.day === "number") days.add(p.open.day);
  }
  // 24시간 영업은 periods 가 1개(day 0, close 없음)로 오는 경우가 있다
  if (hours.periods.length === 1 && !hours.periods[0].close) return "ALWAYS_OPEN";
  return days;
}

const results = [];

for (const t of TARGETS) {
  const { place, error, failure_type, http } = await search(t.query);

  if (error) {
    console.log(`⚠️  ${t.query.padEnd(20)} [${failure_type}, ${http ?? "no HTTP"}] ${error}`);
    results.push({ ...t, status: "ERROR", failure_type, error, http });
    continue;
  }
  if (!place) {
    console.log(`❌ ${t.query.padEnd(20)} 검색 결과 없음`);
    results.push({ ...t, status: "NOT_FOUND", failure_type: "NO_RESULT", http });
    continue;
  }

  const name = place.displayName?.text ?? "(이름없음)";
  const hours = place.regularOpeningHours;

  if (!hours) {
    console.log(`⬜ ${t.query.padEnd(20)} → ${name}  |  regularOpeningHours 없음`);
    results.push({
      ...t, status: "NO_HOURS", place_id: place.id, matched_name: name,
      business_status: place.businessStatus ?? null,
    });
    continue;
  }

  const open = openDaysFrom(hours);
  const record = {
    ...t,
    status: "HAS_HOURS",
    place_id: place.id,
    matched_name: name,
    address: place.formattedAddress,
    business_status: place.businessStatus ?? null,
    weekday_descriptions: hours.weekdayDescriptions ?? null,
    periods_count: hours.periods?.length ?? 0,
  };

  // 정확도 판정
  if (t.expectedClosedDay === null) {
    console.log(`✅ ${t.query.padEnd(20)} → ${name}  |  영업시간 있음 (정답 미확보)`);
    record.accuracy = "UNVERIFIED";
  } else if (open === "ALWAYS_OPEN") {
    console.log(`❗ ${t.query.padEnd(20)} → ${name}  |  24시간으로 표기됨 / 실제 ${DAY_KO[t.expectedClosedDay]}요일 휴무`);
    record.accuracy = "MISMATCH";
    record.mismatch_reason = "구글은 상시영업, 공식은 정기휴일 있음";
  } else {
    const googleClosed = [...Array(7).keys()].filter(d => !open.has(d));
    const ok = googleClosed.length === 1 && googleClosed[0] === t.expectedClosedDay;
    record.google_closed_days = googleClosed.map(d => DAY_KO[d]);
    record.expected_closed_day = DAY_KO[t.expectedClosedDay];
    record.accuracy = ok ? "MATCH" : "MISMATCH";
    const mark = ok ? "✅" : "❗";
    const detail = ok
      ? `${DAY_KO[t.expectedClosedDay]}요일 휴무 일치`
      : `구글 휴무=[${record.google_closed_days.join(",") || "없음"}] / 공식=${DAY_KO[t.expectedClosedDay]}`;
    console.log(`${mark} ${t.query.padEnd(20)} → ${name}  |  ${detail}`);
  }

  results.push(record);
}

// ---- 요약 ----
const total = results.length;
const hasHours = results.filter(r => r.status === "HAS_HOURS").length;
const verified = results.filter(r => r.accuracy === "MATCH" || r.accuracy === "MISMATCH");
const matched = verified.filter(r => r.accuracy === "MATCH").length;

console.log("\n--- 요약 ---");
console.log(`채움률   : ${hasHours}/${total}  (${Math.round((hasHours / total) * 100)}%)`);
if (verified.length) {
  console.log(`정확도   : ${matched}/${verified.length}  (정답 확보된 항목 기준)`);
}

console.log("\n--- 판정 ---");
if (verified.length && matched < verified.length) {
  console.log("휴무일 불일치 발견. 구글 영업시간을 단독 근거로 쓸 수 없다.");
  console.log("→ 주요 관광지는 공식 홈페이지 수동 큐레이션, 구글은 폴백으로만 사용.");
} else if (hasHours / total < 0.6) {
  console.log("채움률이 낮다. 롱테일 장소는 구글로 못 덮는다.");
  console.log("→ 수동 큐레이션 범위를 넓히거나, 대상 장소를 주요 관광지로 한정한다.");
} else if (verified.length && matched === verified.length) {
  console.log("정답 확보 항목은 전부 일치. 다만 표본이 작으니 계절별 운영시간과");
  console.log("공휴일 예외 규칙이 반영되는지는 별도로 확인해야 한다.");
} else {
  console.log("정답을 확보한 항목이 없어 정확도 판정 불가. 공식 출처부터 채울 것.");
}

console.log("\n※ 구글 regularOpeningHours 는 단일 스케줄이다.");
console.log("   경복궁처럼 계절별로 운영시간이 바뀌는 곳, 그리고 대체공휴일에 따라");
console.log("   휴무일이 이동하는 규칙은 구조적으로 표현되지 않는다.");
console.log("   weekday_descriptions 를 직접 눈으로 확인할 것.");

writeFileSync(
  "places-api-check.json",
  JSON.stringify({ checked_at: new Date().toISOString(), summary: { total, hasHours, verified: verified.length, matched }, results }, null, 2)
);
console.log("\n결과 저장: places-api-check.json");
