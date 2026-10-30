#!/usr/bin/env node
/** Google Places 영업시간이 채워져 있는지, 휴무일이 공식 출처와 맞는지 확인한다. */

import { postJson, printGoogleSpend } from "./api-call.mjs";

const KEY = process.env.GKEY;
if (!KEY) {
  console.error('GKEY 환경변수가 없다.  $env:GKEY = "..."');
  process.exit(1);
}

const ENDPOINT = "https://places.googleapis.com/v1/places:searchText";

// Places API 의 day 는 일요일이 0 이다.
const DAY_KO = ["일", "월", "화", "수", "목", "금", "토"];

/** expectedClosedDay 는 공식 출처로 확인한 정기휴일의 day 값이다. */
const TARGETS = [
  { query: "경복궁",                  expectedClosedDay: 2,    source: "국가유산청 궁능유적본부", note: "9~10월 09:00-18:00, 입장마감 17:00" },
  { query: "창덕궁",                  expectedClosedDay: 1,    source: "국가유산청 궁능유적본부" },
  { query: "덕수궁",                  expectedClosedDay: 1,    source: "국가유산청 궁능유적본부" },
  { query: "창경궁",                  expectedClosedDay: 1,    source: "국가유산청 궁능유적본부" },
  { query: "종묘",                    expectedClosedDay: 2,    source: "국가유산청 궁능유적본부" },
  { query: "서울시립미술관 서소문본관", expectedClosedDay: 1,    source: "sema.seoul.go.kr" },
  // 정답을 아직 모르는 곳은 채움률만 본다.
  { query: "국립중앙박물관",           expectedClosedDay: null },
  { query: "국립민속박물관",           expectedClosedDay: null },
  { query: "남산서울타워",             expectedClosedDay: null },
  { query: "광장시장",                 expectedClosedDay: null },
  { query: "통인시장",                 expectedClosedDay: null, note: "소규모 — 채움률 하한 테스트" },
  { query: "북촌한옥마을",             expectedClosedDay: null, note: "상시개방 — 영업시간 개념 없음" },
];

const FIELD_MASK = "places.displayName,places.regularOpeningHours";

async function search(textQuery) {
  const r = await postJson(ENDPOINT, { "X-Goog-Api-Key": KEY, "X-Goog-FieldMask": FIELD_MASK },
                           { textQuery, languageCode: "ko", regionCode: "KR", maxResultCount: 1 });
  if (!r.ok) return { error: r.detail, failure_type: r.failure_type, http: r.http };
  return { place: r.json?.places?.[0] ?? null };
}

function openDaysFrom(hours) {
  if (!hours?.periods?.length) return null;
  const days = new Set();
  for (const p of hours.periods) {
    if (typeof p?.open?.day === "number") days.add(p.open.day);
  }
  // 24시간 영업은 close 가 없는 day 0 period 하나로 오기도 한다.
  if (hours.periods.length === 1 && !hours.periods[0].close) return "ALWAYS_OPEN";
  return days;
}

const results = [];

for (const t of TARGETS) {
  const { place, error, failure_type, http } = await search(t.query);

  if (error) {
    console.log(`⚠️  ${t.query.padEnd(20)} [${failure_type}, ${http ?? "no HTTP"}] ${error}`);
    results.push({ status: "ERROR" });
    continue;
  }
  if (!place) {
    console.log(`❌ ${t.query.padEnd(20)} 검색 결과 없음`);
    results.push({ status: "NOT_FOUND" });
    continue;
  }

  const name = place.displayName?.text ?? "(이름없음)";
  const hours = place.regularOpeningHours;

  if (!hours) {
    console.log(`⬜ ${t.query.padEnd(20)} → ${name}  |  regularOpeningHours 없음`);
    results.push({ status: "NO_HOURS" });
    continue;
  }

  const open = openDaysFrom(hours);
  let accuracy;
  if (t.expectedClosedDay === null) {
    console.log(`✅ ${t.query.padEnd(20)} → ${name}  |  영업시간 있음 (정답 미확보)`);
    accuracy = "UNVERIFIED";
  } else if (open === "ALWAYS_OPEN") {
    console.log(`❗ ${t.query.padEnd(20)} → ${name}  |  24시간으로 표기됨 / 실제 ${DAY_KO[t.expectedClosedDay]}요일 휴무`);
    accuracy = "MISMATCH";
  } else {
    const googleClosed = [...Array(7).keys()].filter(d => !open.has(d));
    const ok = googleClosed.length === 1 && googleClosed[0] === t.expectedClosedDay;
    accuracy = ok ? "MATCH" : "MISMATCH";
    const mark = ok ? "✅" : "❗";
    const detail = ok
      ? `${DAY_KO[t.expectedClosedDay]}요일 휴무 일치`
      : `구글 휴무=[${googleClosed.map(d => DAY_KO[d]).join(",") || "없음"}] / 공식=${DAY_KO[t.expectedClosedDay]}`;
    console.log(`${mark} ${t.query.padEnd(20)} → ${name}  |  ${detail}`);
  }
  // 약관상 파일로 남길 수 없어 요일별 영업시간 글은 화면에서 본다.
  for (const line of hours.weekdayDescriptions ?? []) console.log(`     ${line}`);
  results.push({ status: "HAS_HOURS", accuracy });
}

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
console.log("   위에 찍은 요일별 영업시간을 직접 눈으로 확인할 것.");

// regularOpeningHours 를 요청하면 Text Search Enterprise 요금이다. 1,000건에 $35, 매달 1천 건 무료다.
// 출처 https://developers.google.com/maps/billing-and-pricing/pricing (2026-10-08 갱신본, 2026-10-14 확인)
printGoogleSpend("Google Places", 1000, 35 / 1000);
