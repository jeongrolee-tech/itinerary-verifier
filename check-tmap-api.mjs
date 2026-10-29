#!/usr/bin/env node
/** TMAP 길찾기 API 가 서울 구간에서 수단별 경로를 주는지 확인한다. */

import readline from "node:readline";

function askKey() {
  if (process.env.TMAP_KEY) return Promise.resolve(process.env.TMAP_KEY.trim());
  return new Promise(resolve => {
    const prompt = "TMAP 앱 키 입력: ";
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
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

const REQUEST_TIMEOUT_MS = 20000;
const POI = "https://apis.openapi.sk.com/tmap/pois?version=1&count=10&searchKeyword=";
const TRANSIT = "https://apis.openapi.sk.com/transit/routes";
const PEDESTRIAN = "https://apis.openapi.sk.com/tmap/routes/pedestrian?version=1";
const CAR = "https://apis.openapi.sk.com/tmap/routes?version=1";
const PREDICTION = "https://apis.openapi.sk.com/tmap/routes/prediction?version=1";

const API_NAMES = { [POI.split("?")[0]]: "장소 검색", [TRANSIT]: "대중교통", [PEDESTRIAN.split("?")[0]]: "보행자 길찾기",
                    [CAR.split("?")[0]]: "자동차 길찾기", [PREDICTION.split("?")[0]]: "택시 예측(타임머신)" };
// core/spend.py 의 TMAP_PRICES 와 같은 표다. 확인하지 못한 단가는 지어내지 않고 null 로 둔다.
// 대중교통 0.88원은 docs/data-policy.md(2026-10-02 확인), 무료 제공량은 tmapapi.tmapmobility.com(2026-10-14 확인)에서 왔다.
const TMAP_PRICES = {
  "장소 검색": { freePerDay: 20000, won: null },
  "대중교통": { freePerDay: 10, won: 0.88 },
  "보행자 길찾기": { freePerDay: 1000, won: null },
  "자동차 길찾기": { freePerDay: 1000, won: null },
  "택시 예측(타임머신)": { freePerDay: null, won: null },
};
const SPENT = {};  // API 별로 응답을 받은 호출만 세어 나간 돈을 찍는다

// 429 를 받으면 기다렸다가 세 번까지 다시 묻는다.
async function fetchTmap(url, init) {
  for (let attempt = 0; ; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let res;
    try {
      res = await fetch(url, { ...init, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
    if (res.ok) {
      const name = API_NAMES[url.split("?")[0]] ?? url.split("?")[0];
      SPENT[name] = (SPENT[name] ?? 0) + 1;
    }
    if (res.status !== 429 || attempt === 3) return res;
    const wait = Number(res.headers.get("Retry-After")) || 2 ** attempt;
    console.log(`     ⏳ 429 — ${wait}초 기다렸다 다시 묻는다`);
    await new Promise(resolve => setTimeout(resolve, wait * 1000));
  }
}

// TMAP 에서 이름이 다른 곳이다. core/routes.py 의 TMAP_NAMES 와 같아야 한다.
const TMAP_NAMES = { "서울시립미술관 서소문본관": "서울시립미술관", "서울역": "서울역[KTX정차역]" };

// 이름이 같은 곳만 쓴다. "경복궁"을 찾다가 "경복궁역" 좌표를 쓰면 안 된다.
async function findPlace(name) {
  try {
    const res = await fetchTmap(POI + encodeURIComponent(name), {
      headers: { appKey: KEY, Accept: "application/json" } });
    const raw = await res.text();
    if (!res.ok) return { ok: false, detail: `HTTP ${res.status} ${raw.slice(0, 200)}` };
    const pois = raw.trim() ? JSON.parse(raw)?.searchPoiInfo?.pois?.poi ?? [] : [];
    const target = TMAP_NAMES[name] ?? name;
    const same = s => (s ?? "").replaceAll(" ", "") === target.replaceAll(" ", "");
    const hit = pois.find(p => same(p.name));
    if (!hit) return { ok: false, detail: `'${target}'과 이름이 같은 곳 없음. 받은 이름: ${pois.slice(0, 5).map(p => p.name).join(", ") || "없음"}` };
    // 맞는 곳인지 사람이 확인하게 주소를 같이 보여 준다.
    const address = [hit.upperAddrName, hit.middleAddrName, hit.roadName, hit.firstBuildNo].filter(Boolean).join(" ");
    const found = `TMAP '${hit.name}' · ${address || "주소 없음"}`;
    const front = Number(hit.frontLat) && Number(hit.frontLon);
    return front ? { ok: true, lat: Number(hit.frontLat), lng: Number(hit.frontLon), used: `${found} · 입구 좌표` }
                 : { ok: true, lat: Number(hit.noorLat), lng: Number(hit.noorLon), used: `${found} · 중심점 (입구 좌표 없음)` };
  } catch (error) {
    return { ok: false, detail: error?.name === "AbortError" ? "시간 초과" : String(error?.message ?? error) };
  }
}

const MUSEUM = "서울시립미술관 서소문본관";
const CASES = [
  { mode: "대중교통", from: MUSEUM, to: "경복궁", at: "202610081400", note: "T01 구간, 10월 8일 14:00 출발" },
  { mode: "대중교통", from: "광장시장", to: "서울역", at: "202610081930", note: "T04 구간, 10월 8일 19:30 출발" },
  { mode: "대중교통", from: "경복궁", to: "광장시장", at: "202703051500", note: "5개월 뒤(T14 날짜) 출발" },
  { mode: "대중교통", from: "덕수궁", to: MUSEUM, at: "202610081400", note: "아주 가까운 구간" },
  { mode: "걷기", from: "덕수궁", to: MUSEUM, note: "아주 가까운 구간" },
  { mode: "걷기", from: "경복궁", to: "창덕궁", note: "1km 남짓" },
  { mode: "자동차", from: "광장시장", to: "서울역", note: "택시 요금도 오는지" },
  { mode: "자동차 예측", from: "광장시장", to: "서울역", at: "2026-10-08T19:30:00+0900", note: "10월 8일 19:30 출발 기준" },
  { mode: "자동차 예측", from: "경복궁", to: "광장시장", at: "2027-03-05T15:00:00+0900", note: "5개월 뒤 출발 기준" },
];

function requestFor({ mode, from, to, at }) {
  const s = PLACES[from], e = PLACES[to];
  const xy = { startX: String(s.lng), startY: String(s.lat), endX: String(e.lng), endY: String(e.lat) };
  if (mode === "대중교통") {
    return { url: TRANSIT, body: { ...xy, count: 1, lang: 0, format: "json", searchDttm: at } };
  }
  const coord = { reqCoordType: "WGS84GEO", resCoordType: "WGS84GEO" };
  if (mode === "자동차 예측") {
    // predictionType 은 예측할 시각이다. "departure" 에 19:30 을 넣으면 19:30 도착, 19:05 출발이 온다.
    // 출발 시각을 넣고 도착을 받아야 하므로 "arrival" 을 쓴다.
    const point = (name, p) => ({ name, lon: String(p.lng), lat: String(p.lat) });
    return { url: PREDICTION, body: { routesInfo: { departure: point(from, s), destination: point(to, e),
                                                    predictionType: "arrival", predictionTime: at } } };
  }
  if (mode === "걷기") {
    return { url: PEDESTRIAN, body: { ...xy, ...coord, startName: encodeURIComponent(from), endName: encodeURIComponent(to) } };
  }
  return { url: CAR, body: { ...xy, ...coord } };
}

function readRoute(mode, json) {
  if (mode === "대중교통") {
    const it = json?.metaData?.plan?.itineraries?.[0];
    if (!it) return null;
    return { seconds: it.totalTime, extra: `환승 ${it.transferCount}회 · 걷기 ${Math.round(it.totalWalkTime / 60)}분` };
  }
  const p = json?.features?.[0]?.properties;
  if (p?.totalTime === undefined) return null;
  if (mode === "자동차 예측") {
    return { seconds: p.totalTime,
             extra: `택시 요금 ${p.taxiFare ?? "없음"}원 · 출발 ${p.departureTime ?? "?"} · 도착 ${p.arrivalTime ?? "?"}` };
  }
  return { seconds: p.totalTime, extra: mode === "자동차" ? `택시 요금 ${p.taxiFare ?? "없음"}원` : "" };
}

async function probe(c) {
  const { url, body } = requestFor(c);
  let res;
  try {
    res = await fetchTmap(url, {
      method: "POST",
      headers: { appKey: KEY, Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (error) {
    const timedOut = error?.name === "AbortError";
    return { ok: false, verdict: timedOut ? "TIMEOUT" : "NETWORK_ERROR",
             detail: timedOut ? `${REQUEST_TIMEOUT_MS}ms 안에 응답하지 않음` : String(error?.message ?? error) };
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
  // 대중교통은 경로가 없어도 HTTP 200 에 status 로 온다. 11 은 너무 가깝다는 뜻이다.
  // status 가 어디에 오는지 몰라 응답을 골라 찍지 않고 통째로 보여 준다.
  const verdict = res.ok ? "NO_ROUTE" : res.status === 429 ? "RATE_LIMITED" : "HTTP_ERROR";
  return { ok: false, verdict, detail: `HTTP ${res.status} ${JSON.stringify(json).slice(0, 200)}` };
}

// TMAP 약관상 받은 값은 저장 후 24시간 넘게 쓸 수 없어 파일로 남기지 않는다.
console.log("값은 화면에만 보여 주고 저장하지 않는다 (TMAP 약관: 저장 후 24시간 넘게 사용 불가)\n");
const PLACES = {};
for (const name of new Set(CASES.flatMap(c => [c.from, c.to]))) {
  const r = await findPlace(name);
  if (r.ok) PLACES[name] = r;
  console.log(r.ok ? `📍 ${name} — ${r.used}` : `❌ 장소 ${name} — ${r.detail}`);
}
console.log();

const results = [];
for (const c of CASES) {
  if (!PLACES[c.from] || !PLACES[c.to]) {
    console.log(`➖ ${c.mode} ${c.from} → ${c.to}  장소 좌표가 없어 부르지 않았다`);
    continue;
  }
  const r = await probe(c);
  results.push({ ...c, ...r });
  const label = `${c.mode.padEnd(5)} ${c.from} → ${c.to}  (${c.note})`;
  console.log(r.ok ? `✅ ${label}\n     ${Math.round(r.seconds / 60)}분  ${r.extra}`
                   : `❌ ${label}\n     [${r.verdict}] ${r.detail}`);
}
console.log("➖ 자전거  TMAP 공개 API 목록에 자전거 경로가 없어 부르지 않았다");

console.log("\n--- 수단별 ---");
for (const mode of ["대중교통", "걷기", "자동차", "자동차 예측"]) {
  const rs = results.filter(r => r.mode === mode);
  console.log(`${mode}: ${rs.filter(r => r.ok).length}/${rs.length} 경로 받음`);
}

// 무료 제공량은 하루 단위인데 오늘 쓴 양은 TMAP 콘솔에만 있어 범위로 찍는다.
console.log("\n--- 이번 실행에서 나간 돈 (TMAP) ---");
if (!Object.keys(SPENT).length) console.log("없다. 응답을 받은 호출이 없다.");
for (const [api, n] of Object.entries(SPENT)) {
  const { freePerDay, won } = TMAP_PRICES[api] ?? {};
  console.log(`${api} ${n}건 — ` + (won != null
    ? `하루 ${freePerDay.toLocaleString()}건까지 무료, 넘은 건은 건당 ${won}원 (0원 ~ ${(n * won).toFixed(2)}원)`
    : freePerDay ? `하루 ${freePerDay.toLocaleString()}건까지 무료, 넘은 뒤 단가는 확인하지 못했다`
    : "요금을 확인하지 못했다"));
}
