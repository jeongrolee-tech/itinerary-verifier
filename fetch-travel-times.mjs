#!/usr/bin/env node
/** Google Routes 로 구간별 대중교통 이동시간을 받아 스냅샷으로 남긴다. */

import { writeFileSync } from "node:fs";
import readline from "node:readline";

function askKey() {
  const fromArg = process.argv.find(a => a.startsWith("--key="));
  if (fromArg) return Promise.resolve(fromArg.slice(6).trim());

  return new Promise(resolve => {
    const prompt = "Google API 키 입력: ";
    const rl = readline.createInterface({
      input: process.stdin,
      output: process.stdout,
      terminal: true,
    });
    // 입력한 키가 화면에 보이지 않게 가린다.
    let asked = false;
    rl._writeToOutput = s => {
      if (!asked) { process.stdout.write(prompt); asked = true; return; }
      if (s.includes("\n")) process.stdout.write("\n");
    };
    rl.question(prompt, ans => { rl.close(); resolve(ans.trim()); });
  });
}

const KEY = await askKey();
if (!KEY) {
  console.error("키가 비어 있다.");
  process.exit(1);
}

// 좌표
const P = {
  "경복궁":                   { latitude: 37.5796, longitude: 126.9770 },
  "북촌한옥마을":             { latitude: 37.5826, longitude: 126.9850 },
  "광장시장":                 { latitude: 37.5701, longitude: 126.9997 },
  "서울역":                   { latitude: 37.5547, longitude: 126.9707 },
  "서울시립미술관 서소문본관": { latitude: 37.5640, longitude: 126.9739 },
  "덕수궁":                   { latitude: 37.5658, longitude: 126.9751 },
  "명동":                     { latitude: 37.5636, longitude: 126.9850 },
};

// 채울 구간
const LEGS = [
  { from: "서울시립미술관 서소문본관", to: "경복궁",       for: "예시 5-1 정상" },
  { from: "경복궁",                   to: "서울시립미술관 서소문본관", for: "예시 5-2 위반" },
  { from: "경복궁",                   to: "광장시장",     for: "예시 5-3 미확인" },
  { from: "경복궁",                   to: "북촌한옥마을", for: "§6-4" },
  { from: "광장시장",                 to: "서울역",       for: "§6-4 · KTX 케이스" },
  { from: "서울시립미술관 서소문본관", to: "덕수궁",      for: "§6-4 · 부록 A-2 재현" },
  { from: "덕수궁",                   to: "명동",         for: "§6-4" },
];

// 스냅샷 조건을 맞추려고 출발 시각을 내일 14:00 KST(05:00 UTC)로 고정한다.
const DEPARTURE = (() => {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + 1);
  d.setUTCHours(5, 0, 0, 0);
  return d.toISOString().replace(/\.\d{3}/, "");
})();

const ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes";
let CALLS = 0;  // 응답을 받은 호출만 세어 끝에 나간 돈을 찍는다
const REQUEST_TIMEOUT_MS = 20000;
const FIELDS = [
  "routes.duration",
  "routes.distanceMeters",
  "routes.legs.steps.travelMode",
  "routes.legs.steps.staticDuration",
  "routes.legs.steps.distanceMeters",
].join(",");

// 하버사인 도보 근사. 우회계수 1.3, 보행속도 4km/h 를 쓴다.
function straightMeters(a, b) {
  const R = 6371000, rad = x => (x * Math.PI) / 180;
  const dLat = rad(b.latitude - a.latitude);
  const dLon = rad(b.longitude - a.longitude);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(rad(a.latitude)) * Math.cos(rad(b.latitude)) * Math.sin(dLon / 2) ** 2;
  return Math.round(2 * R * Math.asin(Math.sqrt(h)));
}
const walkEstimateMin = m => Math.round((m * 1.3) / (4000 / 60));

const sec = v => parseInt(String(v ?? "0").replace("s", ""), 10) || 0;
const min = s => Math.round(s / 60);

async function call(leg) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": FIELDS,
      },
      body: JSON.stringify({
        origin:      { location: { latLng: P[leg.from] } },
        destination: { location: { latLng: P[leg.to] } },
        travelMode: "TRANSIT",  // 한국은 WALK 를 주지 않는다
        departureTime: DEPARTURE,
        languageCode: "ko",
        units: "METRIC",
      }),
      signal: controller.signal,
    });
  } catch (error) {
    const timedOut = error?.name === "AbortError";
    return {
      ok: false,
      failure_type: timedOut ? "TIMEOUT" : "NETWORK_ERROR",
      reason: timedOut ? `${REQUEST_TIMEOUT_MS}ms 안에 응답하지 않음` : String(error?.message ?? error),
      http: null,
    };
  } finally {
    clearTimeout(timer);
  }
  if (res.ok) CALLS += 1;

  const raw = await res.text();
  let json;
  try {
    json = JSON.parse(raw);
  } catch {
    return {
      ok: false,
      failure_type: "UNPARSEABLE_RESPONSE",
      reason: "응답이 JSON 형식이 아님",
      http: res.status,
      raw,
    };
  }

  if (!res.ok) {
    return {
      ok: false,
      failure_type: "HTTP_ERROR",
      reason: json?.error?.message ?? raw.slice(0, 200),
      http: res.status,
      raw: json,
    };
  }
  const route = json?.routes?.[0];
  if (!route) {
    return { ok: false, failure_type: "NO_RESULT", reason: "경로 없음", http: res.status, raw: json };
  }

  const steps = route.legs?.[0]?.steps ?? [];
  const staticTotal = steps.reduce((a, s) => a + sec(s.staticDuration), 0);
  const walkOnly = steps.filter(s => s.travelMode === "WALK")
                        .reduce((a, s) => a + sec(s.staticDuration), 0);
  const total = sec(route.duration);

  return {
    ok: true,
    http: res.status,
    duration_min: min(total),  // 대기를 포함한 값이라 판정에 쓴다
    static_min: min(staticTotal),
    wait_min: min(total - staticTotal),  // 배차 대기 추정이다
    walk_step_min: min(walkOnly),
    transit_steps: steps.filter(s => s.travelMode === "TRANSIT").length,
    meters: route.distanceMeters,
    raw: json,
  };
}

// 실행
console.log(`\n출발 시각 (고정): ${DEPARTURE}\n`);

const rows = [];
for (const leg of LEGS) {
  const straight = straightMeters(P[leg.from], P[leg.to]);
  const walkEst = walkEstimateMin(straight);
  const r = await call(leg);

  const label = `${leg.from} → ${leg.to}`;
  if (!r.ok) {
    console.log(`❌ ${label}\n   [${r.failure_type}, ${r.http ?? "no HTTP"}] ${r.reason}\n`);
    rows.push({ ...leg, straight_m: straight, walk_estimate_min: walkEst, ok: false,
      failure_type: r.failure_type, reason: r.reason, http: r.http });
    continue;
  }

  // 하버사인 근사는 계수가 전부 system_default 라 도보 분기 근거로 쓰지 않는다.
  // 실측과 대조하니 경복궁에서 북촌까지 53% 과소평가했다.
  const flag = r.transit_steps === 0
    ? "경로에 대중교통 구간 없음 → 사실상 도보 구간"
    : "도보 분기 여부: 판단 불가 (TMAP 도보시간 필요)";

  console.log(
    `✅ ${label}\n` +
    `   판정에 쓸 값 ${r.duration_min}분  (순수이동 ${r.static_min}분 + 대기 ${r.wait_min}분)\n` +
    `   직선 ${straight}m / 경로거리 ${r.meters}m\n` +
    `   [참고] 하버사인 도보 근사 ${walkEst}분 — 근거 아님, 오차 큼\n` +
    `   ${flag}\n`
  );

  rows.push({
    ...leg,
    straight_m: straight,
    walk_estimate_min: walkEst,
    ok: true,
    duration_min: r.duration_min,
    static_min: r.static_min,
    wait_min: r.wait_min,
    walk_step_min: r.walk_step_min,
    transit_steps: r.transit_steps,
    route_meters: r.meters,
    note: flag,
    raw_response: r.raw,  // 근거로 쓰려면 원본 응답이 남아 있어야 한다
  });
}

const snapshot = {
  snapshot_id: `tt-${new Date().toISOString().slice(0, 10)}`,
  checked_at: new Date().toISOString(),
  departure_time: DEPARTURE,
  mode: "TRANSIT",
  note: "한국은 Routes API WALK 미제공. 전부 TRANSIT으로 호출하고 하버사인 도보 근사와 비교했다.",
  legs: rows,
};
writeFileSync("travel-times.json", JSON.stringify(snapshot, null, 2));

console.log("--- §6-4 표에 붙일 내용 ---\n");
console.log("| 출발 → 도착 | 직선 | ② 구글 TRANSIT | ③ 대조값 | 도보시간 | 쓸 값 |");
console.log("| --- | --- | --- | --- | --- | --- |");
for (const r of rows) {
  const api = r.ok ? `${r.duration_min}분 (대기 ${r.wait_min}분)` : `실패: ${r.reason}`;
  const use = r.ok ? `${r.duration_min}분` : "없음 → `unknown`";
  console.log(`| ${r.from} → ${r.to} | ${r.straight_m}m | ${api} | — | 미확보 | ${use} |`);
}
console.log(
  "\n※ 하버사인 근사는 표에 넣지 않는다. system_default라 근거가 못 된다.\n" +
  "   도보시간 열은 TMAP 호출 또는 타 지도 서비스 수동 대조로 채운다."
);
console.log(`\n스냅샷 ID: ${snapshot.snapshot_id}`);
console.log("저장: travel-times.json");

// Compute Routes Essentials 는 1,000건에 $5, 매달 1만 건 무료다. TRAFFIC_AWARE 같은 기능은 Pro 라 쓰지 않는다.
// 출처 https://developers.google.com/maps/billing-and-pricing/pricing (2026-10-08 갱신본, 2026-10-14 확인)
const PER_CALL_USD = 5 / 1000;
console.log("\n--- 이번 실행에서 나간 돈 ---");
// 무료 사용량은 달 단위인데 이번 달에 쓴 양은 Google Cloud 콘솔에만 있어 범위로 찍는다.
console.log(`Google Routes ${CALLS}건 — 매달 10,000건까지 무료, 넘은 건은 건당 $${PER_CALL_USD}`
            + ` ($0 ~ $${(CALLS * PER_CALL_USD).toFixed(3)})`);
