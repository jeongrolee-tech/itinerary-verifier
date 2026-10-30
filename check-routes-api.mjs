#!/usr/bin/env node
/** Google Routes API 가 한국에서 수단별 경로를 주는지 확인한다. */

import { postJson, printGoogleSpend } from "./api-call.mjs";

const KEY = process.env.GKEY;
if (!KEY) {
  console.error("GKEY 환경변수가 없다.  export GKEY=\"...\"");
  process.exit(1);
}

const ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes";

// 대중교통은 출발 시각이 필요하다. 내일 오전 10시 KST 로 잡는다.
const departureTime = (() => {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + 1);
  d.setUTCHours(1, 0, 0, 0); // UTC 01:00 이 KST 10:00 이다. setHours 는 컴퓨터 시간대로 맞춘다
  return d.toISOString().replace(/\.\d{3}/, "");
})();

const PLACES = {
  경복궁:       { latitude: 37.5796, longitude: 126.9770 },
  북촌한옥마을: { latitude: 37.5826, longitude: 126.9850 },
  광장시장:     { latitude: 37.5701, longitude: 126.9997 },
  서울역:       { latitude: 37.5547, longitude: 126.9707 },
};

const CASES = [
  { mode: "WALK",    from: "경복궁",   to: "북촌한옥마을", note: "도보 약 1km" },
  { mode: "TRANSIT", from: "광장시장", to: "서울역",       note: "대중교통" },
  { mode: "DRIVE",   from: "광장시장", to: "서울역",       note: "자동차" },
];

async function probe({ mode, from, to }) {
  const body = {
    origin:      { location: { latLng: PLACES[from] } },
    destination: { location: { latLng: PLACES[to] } },
    travelMode: mode,
    languageCode: "ko",
    units: "METRIC",
  };
  if (mode === "TRANSIT") body.departureTime = departureTime;
  if (mode === "DRIVE") body.routingPreference = "TRAFFIC_UNAWARE";

  const r = await postJson(ENDPOINT, { "X-Goog-Api-Key": KEY, "X-Goog-FieldMask": "routes.duration,routes.distanceMeters" },
                           body);
  if (!r.ok) return { ok: false, verdict: r.failure_type, detail: r.detail, http: r.http };
  const route = r.json?.routes?.[0];
  if (!route) return { ok: false, verdict: "NO_ROUTE", detail: "경로 없음 (미지원 가능성)", http: r.http };
  const sec = parseInt(String(route.duration).replace("s", ""), 10);
  return { ok: true, verdict: "OK", minutes: Math.round(sec / 60), meters: route.distanceMeters, http: r.http };
}

const results = [];
for (const c of CASES) {
  const r = await probe(c);
  results.push({ ...c, ...r });
  const label = `${c.mode.padEnd(8)} ${c.from} → ${c.to}`;
  if (r.ok) {
    console.log(`✅ ${label}  ${r.minutes}분 / ${r.meters}m`);
  } else {
    console.log(`❌ ${label}  [${r.verdict}] ${r.detail}`);
  }
}

console.log("\n--- 판정 ---");
const okModes = results.filter(r => r.ok).map(r => r.mode);
if (okModes.length === 3) {
  console.log("세 모드 전부 지원. Google 단일 벤더로 통합 가능.");
} else if (okModes.includes("TRANSIT") && !okModes.includes("WALK")) {
  console.log("대중교통만 지원. 도보는 TMAP으로 분리 필요.");
} else if (okModes.length === 0) {
  console.log("전부 실패. 키 설정 문제인지 먼저 확인하고, 아니면 ODsay + TMAP으로 간다.");
} else {
  console.log(`지원: ${okModes.join(", ")} / 미지원: ${results.filter(r => !r.ok).map(r => r.mode).join(", ")}`);
}

// Compute Routes Essentials 는 1,000건에 $5, 매달 1만 건 무료다. TRAFFIC_AWARE 같은 기능은 Pro 라 쓰지 않는다.
// 출처 https://developers.google.com/maps/billing-and-pricing/pricing (2026-10-08 갱신본, 2026-10-14 확인)
printGoogleSpend("Google Routes", 10000, 5 / 1000);
