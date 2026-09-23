#!/usr/bin/env node
/**
 * §6-4 이동시간 표를 채우기 위한 실제 호출 스크립트
 *
 *   node fetch-travel-times.mjs
 *   → 실행하면 API 키를 물어본다 (입력값은 화면에 안 찍힘)
 *
 * 한국에서 WALK 모드가 막혀 있으므로 전부 TRANSIT으로 호출하고,
 * 짧은 구간은 하버사인 도보 근사와 비교해서 도보 분기 기준의 재료로 쓴다.
 *
 * 결과는 travel-times.json 으로 저장된다. 원본 응답을 그대로 담으므로
 * 벤치마크 스냅샷으로 쓸 수 있다.
 */

import { writeFileSync } from "node:fs";
import readline from "node:readline";

// ── API 키를 터미널에서 입력받는다 ───────────────────────────────
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
    // 입력값이 화면에 남지 않도록 가린다 (셸 히스토리에도 안 남는다)
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

// ── 좌표 ────────────────────────────────────────────────────────
const P = {
  "경복궁":                   { latitude: 37.5796, longitude: 126.9770 },
  "북촌한옥마을":             { latitude: 37.5826, longitude: 126.9850 },
  "광장시장":                 { latitude: 37.5701, longitude: 126.9997 },
  "서울역":                   { latitude: 37.5547, longitude: 126.9707 },
  "서울시립미술관 서소문본관": { latitude: 37.5640, longitude: 126.9739 },
  "덕수궁":                   { latitude: 37.5658, longitude: 126.9751 },
  "명동":                     { latitude: 37.5636, longitude: 126.9850 },
};

// ── 채울 구간 ───────────────────────────────────────────────────
const LEGS = [
  { from: "서울시립미술관 서소문본관", to: "경복궁",       for: "예시 5-1 정상" },
  { from: "경복궁",                   to: "서울시립미술관 서소문본관", for: "예시 5-2 위반" },
  { from: "경복궁",                   to: "광장시장",     for: "예시 5-3 미확인" },
  { from: "경복궁",                   to: "북촌한옥마을", for: "§6-4" },
  { from: "광장시장",                 to: "서울역",       for: "§6-4 · KTX 케이스" },
  { from: "서울시립미술관 서소문본관", to: "덕수궁",      for: "§6-4 · 부록 A-2 재현" },
  { from: "덕수궁",                   to: "명동",         for: "§6-4" },
];

// 출발 시각을 고정해야 스냅샷이 결정론적이 된다.
// 내일 14:00 KST = 05:00 UTC
const DEPARTURE = (() => {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + 1);
  d.setUTCHours(5, 0, 0, 0);
  return d.toISOString().replace(/\.\d{3}/, "");
})();

const ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes";
const REQUEST_TIMEOUT_MS = 20000;
const FIELDS = [
  "routes.duration",
  "routes.distanceMeters",
  "routes.legs.steps.travelMode",
  "routes.legs.steps.staticDuration",
  "routes.legs.steps.distanceMeters",
].join(",");

// ── 하버사인 도보 근사 (우회계수 1.3, 보행속도 4km/h) ───────────
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
        travelMode: "TRANSIT",
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
    duration_min: min(total),          // 대기 포함. 판정에 쓰는 값
    static_min: min(staticTotal),      // 순수 이동
    wait_min: min(total - staticTotal),// 배차 대기 추정
    walk_step_min: min(walkOnly),      // 경로 안의 도보만
    transit_steps: steps.filter(s => s.travelMode === "TRANSIT").length,
    meters: route.distanceMeters,
    raw: json,                         // 스냅샷용 원본
  };
}

// ── 실행 ────────────────────────────────────────────────────────
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

  // 도보/대중교통 분기는 TMAP 실제 도보시간이 있어야 판단할 수 있다.
  // 하버사인 근사는 우회계수 1.3과 보행속도 4km/h가 전부 system_default라
  // 판정 근거로 쓸 수 없다. (실측 대조 결과 경복궁→북촌에서 53% 과소평가)
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
    raw_response: r.raw,   // 원본 응답. 근거로 쓰려면 이게 남아 있어야 한다
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

// ── §6-4에 붙일 표 ──────────────────────────────────────────────
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
