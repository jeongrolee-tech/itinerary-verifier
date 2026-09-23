#!/usr/bin/env node
/**
 * Google Routes API 한국 지원 여부 확인
 *
 *   export GKEY="발급받은키"
 *   node check-routes-api.mjs
 *
 * 결과 JSON은 routes-api-check.json 으로 저장된다.
 * 그대로 노션 4-4 섹션 근거로 붙이면 된다.
 */

import { writeFileSync } from "node:fs";

const KEY = process.env.GKEY;
if (!KEY) {
  console.error("GKEY 환경변수가 없다.  export GKEY=\"...\"");
  process.exit(1);
}

const ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes";
const REQUEST_TIMEOUT_MS = 20000;

// 출발 시각: 내일 오전 10시 (KST). 대중교통 모드에 필요하다.
const departureTime = (() => {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(1, 0, 0, 0); // UTC 01:00 == KST 10:00
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

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    const timedOut = error?.name === "AbortError";
    return {
      ok: false,
      verdict: timedOut ? "TIMEOUT" : "NETWORK_ERROR",
      detail: timedOut ? `${REQUEST_TIMEOUT_MS}ms 안에 응답하지 않음` : String(error?.message ?? error),
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
    return { ok: false, verdict: "UNPARSEABLE_RESPONSE", detail: "응답이 JSON 형식이 아님", http: res.status, raw };
  }

  if (!res.ok) {
    return { ok: false, verdict: "HTTP_ERROR", detail: json?.error?.message ?? raw.slice(0, 200), http: res.status, raw: json };
  }
  const route = json?.routes?.[0];
  if (!route) {
    return { ok: false, verdict: "NO_ROUTE", detail: "경로 없음 (미지원 가능성)", http: res.status };
  }
  const sec = parseInt(String(route.duration).replace("s", ""), 10);
  return {
    ok: true,
    verdict: "OK",
    minutes: Math.round(sec / 60),
    meters: route.distanceMeters,
    http: res.status,
  };
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

const out = { checked_at: new Date().toISOString(), departureTime, results };
writeFileSync("routes-api-check.json", JSON.stringify(out, null, 2));

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
console.log("결과 저장: routes-api-check.json");
