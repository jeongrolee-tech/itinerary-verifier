/** 확인 스크립트가 같이 쓰는 키 입력, 시간 제한이 있는 POST, Google 비용 줄이다. */

import readline from "node:readline";

export const REQUEST_TIMEOUT_MS = 20000;
let answered = 0;  // 응답을 받은 호출만 세어 끝에 나간 돈을 찍는다

/** given 이 비어 있으면 입력한 키가 화면에 보이지 않게 묻는다. 끝내 비어 있으면 멈춘다. */
export async function askKey(label, given) {
  const key = (given || await new Promise(resolve => {
    const prompt = `${label} 입력: `;
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    let asked = false;
    rl._writeToOutput = s => {
      if (!asked) { process.stdout.write(prompt); asked = true; return; }
      if (s.includes("\n")) process.stdout.write("\n");
    };
    rl.question(prompt, ans => { rl.close(); resolve(ans); });
  })).trim();
  if (!key) {
    console.error("키가 비어 있다.");
    process.exit(1);
  }
  return key;
}

/** 실패하면 이유(failure_type)를 나눠 ok: false 로 돌려준다. */
export async function postJson(url, headers, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", ...headers },
                             body: JSON.stringify(body), signal: controller.signal });
  } catch (error) {
    const timedOut = error?.name === "AbortError";
    return { ok: false, failure_type: timedOut ? "TIMEOUT" : "NETWORK_ERROR",
             detail: timedOut ? `${REQUEST_TIMEOUT_MS}ms 안에 응답하지 않음` : String(error?.message ?? error), http: null };
  } finally {
    clearTimeout(timer);
  }
  if (res.ok) answered += 1;

  const raw = await res.text();
  let json;
  try {
    json = JSON.parse(raw);
  } catch {
    return { ok: false, failure_type: "UNPARSEABLE_RESPONSE", detail: "응답이 JSON 형식이 아님", http: res.status, raw };
  }
  if (!res.ok) {
    return { ok: false, failure_type: "HTTP_ERROR", detail: json?.error?.message ?? raw.slice(0, 200), http: res.status, raw: json };
  }
  return { ok: true, http: res.status, json };
}

/** 무료 사용량은 달 단위인데 이번 달에 쓴 양은 Google Cloud 콘솔에만 있어 범위로 찍는다. */
export function printGoogleSpend(api, freePerMonth, perCallUsd) {
  console.log("\n--- 이번 실행에서 나간 돈 ---");
  console.log(`${api} ${answered}건 — 매달 ${freePerMonth.toLocaleString("en-US")}건까지 무료, 넘은 건은 건당 $${perCallUsd}`
              + ` ($0 ~ $${(answered * perCallUsd).toFixed(3)})`);
}
