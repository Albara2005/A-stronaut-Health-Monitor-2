// Browser-level smoke test of frontend/index.html against a RUNNING stack (jsdom, real fetch).
//   1) start broker + backend:   mosquitto &   uvicorn orion_app:app --port 8000
//   2) feed one co2 scenario:    python simulator.py --scenario co2 --device-id sim-01
//   3) run:                      npm i jsdom && node tests/dom_smoke_test.js
const { JSDOM } = require("jsdom");
const BASE = "http://localhost:8000";
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failed = 0;
const check = (name, ok, extra="") => { console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  -> " + extra : "")); if(!ok) failed++; };

(async () => {
  const html = await (await fetch(BASE + "/")).text();
  const errors = [];
  const dom = new JSDOM(html, {
    url: BASE + "/", runScripts: "dangerously", pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, BASE), o);          // same-origin relative URLs -> real backend
      w.addEventListener("error", e => errors.push(e.message));
    }
  });
  const d = dom.window.document, $ = id => d.getElementById(id);
  await sleep(1500);

  check("page loaded, no JS errors", errors.length === 0, errors.join("; "));
  check("API chip green", $("c-api").className.includes("ok"), $("c-api").textContent);
  check("MQTT chip green", $("c-mqtt").className.includes("ok"), $("c-mqtt").textContent);
  check("status = CRITICAL (Arabic label)", $("status").className === "CRITICAL" && $("status").textContent === "حرج", $("status").textContent);
  check("device selector shows sim-01", [...$("dev").options].some(o => o.value === "sim-01") && $("dev").value === "sim-01");
  const tiles = [...d.querySelectorAll(".tile")].map(t => t.textContent);
  check("telemetry tiles rendered (CO2 1300, scrubber INACTIVE)", tiles.some(t => t.includes("1300")) && tiles.some(t => t.includes("INACTIVE")), `${tiles.length} tiles`);
  const rules = [...d.querySelectorAll(".rule")].map(r => r.textContent);
  check("rules ECLSS-CO2-002 + 001 shown with recommendation", rules.length === 2 && rules[0].includes("ECLSS-CO2-002") && rules[0].length > 40, rules[0]);
  check("data quality chip fresh", $("quality").className.includes("ok"), $("quality").textContent);
  check("history table populated", d.querySelectorAll("#hist tr").length >= 1);
  check("disclaimer shown", $("disc").textContent.includes("not validated"));

  // language toggle
  $("lang").click(); await sleep(1500);
  check("EN toggle -> dir=ltr, label CRITICAL", d.documentElement.dir === "ltr" && $("status").textContent === "CRITICAL");

  // demo button -> POST -> backend -> expert system -> DOM
  const btn = [...d.querySelectorAll("#demo button")].find(b => b.textContent === "Low SpO2");
  btn.click(); await sleep(1500);
  check("demo 'Low SpO2' selects web-demo, status HIGH", $("dev").value === "web-demo" && $("status").textContent === "HIGH", `${$("dev").value}/${$("status").textContent}`);
  check("rule CREW-SPO2-001 rendered", [...d.querySelectorAll(".rule")].some(r => r.textContent.includes("CREW-SPO2-001")));
  const nominal = [...d.querySelectorAll("#demo button")].find(b => b.textContent === "Nominal");
  nominal.click(); await sleep(1500);
  check("demo 'Nominal' -> NOMINAL, no rules", $("status").textContent === "NOMINAL" && d.querySelectorAll(".rule").length === 0 && $("rules").textContent.includes("No rules matched"),
        `status=${$("status").textContent} rules=${d.querySelectorAll(".rule").length} text="${$("rules").textContent}"`);

  // XSS safety: hostile-looking device id must be rendered as text
  const r = await fetch(BASE + "/api/embedded/telemetry", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({device_id:"<img src=x onerror=alert(1)>", data:{}})});
  check("hostile device_id rejected by backend (422)", r.status === 422, String(r.status));
  dom.window.close();
  console.log(failed ? `\n${failed} FAILED` : "\nALL DOM CHECKS PASSED");
  process.exit(failed ? 1 : 0);
})();
