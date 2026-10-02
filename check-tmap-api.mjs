#!/usr/bin/env node
/**
 * TMAP 길찾기 API 가 서울 구간에서 수단별로 경로를 주는지 확인한다
 *
 *   node check-tmap-api.mjs
 *   → 앱 키를 물어본다 (입력값은 화면에 안 찍히고 셸 기록에도 안 남는다).
 *     TMAP_KEY 환경변수가 있으면 그걸 쓴다.
 *
 * 확인하는 것
 *   대중교통 — 경로가 오나, 출발 시각(searchDttm)을 몇 달 뒤로 넣어도 되나, 아주 가까운 구간은 어떻게 오나
 *   걷기     — 보행자 경로가 오나
 *   자동차   — 경로와 택시 요금이 오나
 *   자전거   — TMAP 공개 API 목록에 없어서 부르지 않는다
 *
 * 값은 화면에만 보여 주고 파일로 남기지 않는다. TMAP 약관상 받은 데이터는
 * 저장 후 24시간 넘게 쓸 수 없다 — docs/data-policy.md 참조.
 */

import { readFileSync } from "node:fs";
import readline from "node:readline";

// ── 앱 키를 터미널에서 입력받는다 (fetch-travel-times.mjs 와 같은 방식) ──
function askKey() {
  if (process.env.TMAP_KEY) return Promise.resolve(process.env.TMAP_KEY.trim());
  return new Promise(resolve => {
    const prompt = "TMAP 앱 키 입력: ";
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    // 입력값이 화면에 남지 않도록 가린다 (셸 기록에도 안 남는다)
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

const REQUEST_TIMEOUT_MS = 20000;
const TRANSIT = "https://apis.openapi.sk.com/transit/routes";
const PEDESTRIAN = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1";
const CAR = "https://apis.openapi.sk.com/tmap/routes?version=1";

// 장소 좌표는 core/facts.json 의 입구 좌표를 그대로 쓴다. 서울역은 스톱이 아니라 필수 조건의
// 목적지라 facts.json 장소에 없어서 check-routes-api.mjs 와 같은 값을 쓴다.
const facts = JSON.parse(readFileSync(new URL("./core/facts.json", import.meta.url), "utf-8"));
const PLACES = Object.fromEntries(
  Object.entries(facts.places)
    .filter(([, p]) => p.entrance)
    .map(([name, p]) => [name, { lat: p.entrance.lat, lng: p.entrance.lng }]),
);
PLACES["서울역"] = { lat: 37.5547, lng: 126.9707 };

const MUSEUM = "서울시립미술관 서소문본관";
const CASES = [
  { mode: "대중교통", from: MUSEUM, to: "경복궁", at: "202610081400", note: "T01 구간, 10월 8일 14:00 출발" },
  { mode: "대중교통", from: "광장시장", to: "서울역", at: "202610081930", note: "T04 구간, 10월 8일 19:30 출발" },
  { mode: "대중교통", from: "경복궁", to: "광장시장", at: "202703051500", note: "5개월 뒤(T14 날짜) 출발" },
  { mode: "대중교통", from: "덕수궁", to: MUSEUM, at: "202610081400", note: "아주 가까운 구간" },
  { mode: "걷기", from: "덕수궁", to: MUSEUM, note: "아주 가까운 구간" },
  { mode: "걷기", from: "경복궁", to: "창덕궁", note: "1km 남짓" },
  { mode: "자동차", from: "광장시장", to: "서울역", note: "택시 요금도 오는지" },
];

function requestFor({ mode, from, to, at }) {
  const s = PLACES[from], e = PLACES[to];
  const xy = { startX: String(s.lng), startY: String(s.lat), endX: String(e.lng), endY: String(e.lat) };
  if (mode === "대중교통") {
    return { url: TRANSIT, body: { ...xy, count: 1, lang: 0, format: "json", searchDttm: at } };
  }
  const coord = { reqCoordType: "WGS84GEO", resCoordType: "WGS84GEO" };
  if (mode === "걷기") {
    return { url: PEDESTRIAN, body: { ...xy, ...coord, startName: encodeURIComponent(from), endName: encodeURIComponent(to) } };
  }
  return { url: CAR, body: { ...xy, ...coord } };
}

// 응답에서 시간을 읽는다. 대중교통은 metaData.plan.itineraries[0], 걷기·자동차는 features[0].properties 에 있다.
function readRoute(mode, json) {
  if (mode === "대중교통") {
    const it = json?.metaData?.plan?.itineraries?.[0];
    if (!it) return null;
    return { seconds: it.totalTime, extra: `환승 ${it.transferCount}회 · 걷기 ${Math.round(it.totalWalkTime / 60)}분` };
  }
  const p = json?.features?.[0]?.properties;
  if (p?.totalTime === undefined) return null;
  return { seconds: p.totalTime, extra: mode === "자동차" ? `택시 요금 ${p.taxiFare ?? "없음"}원` : "" };
}

async function probe(c) {
  const { url, body } = requestFor(c);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { appKey: KEY, Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    const timedOut = error?.name === "AbortError";
    return { ok: false, verdict: timedOut ? "TIMEOUT" : "NETWORK_ERROR",
             detail: timedOut ? `${REQUEST_TIMEOUT_MS}ms 안에 응답하지 않음` : String(error?.message ?? error) };
  } finally {
    clearTimeout(timer);
  }

  const raw = await res.text();
  let json;
  try {
    json = JSON.parse(raw);
  } catch {
    return { ok: false, verdict: "UNPARSEABLE_RESPONSE", detail: `HTTP ${res.status} ${raw.slice(0, 200)}` };
  }
  const route = res.ok ? readRoute(c.mode, json) : null;
  if (route) return { ok: true, ...route };
  // 경로가 없으면 TMAP 이 준 이유를 그대로 보여 준다. 대중교통은 HTTP 200 에 result.status 로 온다(11 = 가까움)
  const reason = json?.result ?? json?.error ?? json;
  return { ok: false, verdict: res.ok ? "NO_ROUTE" : "HTTP_ERROR", detail: `HTTP ${res.status} ${JSON.stringify(reason).slice(0, 200)}` };
}

console.log("값은 화면에만 보여 주고 저장하지 않는다 (TMAP 약관: 저장 후 24시간 넘게 사용 불가)\n");
const results = [];
for (const c of CASES) {
  const r = await probe(c);
  results.push({ ...c, ...r });
  const label = `${c.mode.padEnd(5)} ${c.from} → ${c.to}  (${c.note})`;
  console.log(r.ok ? `✅ ${label}\n     ${Math.round(r.seconds / 60)}분  ${r.extra}`
                   : `❌ ${label}\n     [${r.verdict}] ${r.detail}`);
}
console.log("➖ 자전거  TMAP 공개 API 목록에 자전거 경로가 없어 부르지 않았다");

console.log("\n--- 수단별 ---");
for (const mode of ["대중교통", "걷기", "자동차"]) {
  const rs = results.filter(r => r.mode === mode);
  console.log(`${mode}: ${rs.filter(r => r.ok).length}/${rs.length} 경로 받음`);
}
