/* Two things in app.js:
 * 1. A JS-rendered card's inline handlers survive a hostile title: the HTML
 *    parser decodes entities inside attributes, so a title holding "&quot;"
 *    used to close the JSON string and run code.
 * 2. The Requests page poller rebuilds "What this download needs" from
 *    /api/requests/status with textContent (never innerHTML).
 * From the repo root:
 *   npm install --no-save jsdom && node tests/js/needs-and-escaping.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

// --- 1. escaping ---------------------------------------------------------
const EVIL = { asin: "B0EVIL0001", title: 'X &quot;});window.pwned=1;// <img src=x onerror=window.pwned=2> \'', author: "A & B",
               cover: "", series: "", format: "audiobook", state: null };
{
  const dom = new JSDOM(`<!doctype html><body><div id="toast"></div>
    <form class="search"><input id="topsearch" type="search"><div id="search-suggest"></div></form>
    <div class="section" id="results-head" hidden></div><div id="results" class="grid"></div>
    <div class="section" id="discover-section"><div id="discover" class="grid"></div><div id="scroll-sentinel"></div></div>
  </body>`, { url: "http://stackarr.test/discover", runScripts: "dangerously", pretendToBeVisual: true });
  const { window } = dom;
  const posted = [];
  window.fetch = async (url, opts) => {
    if (opts && opts.body) posted.push([url, opts.body]);
    const body = url.includes("/api/search") ? [EVIL] : ((opts && opts.method === "POST") ? { ok: true } : []);
    return { status: 200, ok: true, headers: { get: () => "application/json" }, json: async () => body };
  };
  window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
  window.Stackarr.initDiscover();
  const input = window.document.getElementById("topsearch");
  input.value = "evil";
  input.dispatchEvent(new window.Event("input"));
  await new Promise(r => setTimeout(r, 600));
  const card = window.document.querySelector("#results .media-card");
  if (!card) fail("no result card rendered");
  if (window.document.querySelector("#results img[src=x]")) fail("title markup was parsed as HTML");
  const btn = [...card.querySelectorAll("button")].find(b => /Request/.test(b.textContent));
  if (!btn) fail("no Request button");
  try { btn.click(); } catch (e) { fail("Request handler threw: " + e.message); }
  await new Promise(r => setTimeout(r, 50));
  if (window.pwned) fail("a title broke out of the inline handler (pwned=" + window.pwned + ")");
  const sent = posted.find(([u]) => u.includes("/api/request"));
  if (!sent || !sent[1].includes("&quot;")) fail("the Request handler didn't get the title intact: " + JSON.stringify(posted));
}

// --- 2. needs poller -----------------------------------------------------
{
  const dom = new JSDOM(`<!doctype html><body><div id="toast"></div>
    <div class="req-row" data-id="12"><div class="req-status">
      <span class="req-st" data-fmt="ebook"><span class="badge badge-handed">Requested</span></span></div>
      <div class="req-detail">Sent upstream — searching</div><div class="req-needs"></div></div>
  </body>`, { url: "http://stackarr.test/requests", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const STATUS = { "12": { state: "needs", label: "Needs VIP +1", cls: "needs", detail: "x", formats: { ebook: {
    state: "needs", label: "Needs VIP +1", cls: "needs", detail: "x", needs: [
      { key: "vip", label: "Needs VIP", text: "Every release is for the tracker's VIP or higher-rank members." },
      { key: "other", label: "Needs attention", text: "<b>raw</b> words" }] } } } };
  window.fetch = async () => ({ status: 200, ok: true, headers: { get: () => "application/json" }, json: async () => STATUS });
  window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
  await window.Stackarr.pollRequestStatus(true);
  const box = window.document.querySelector(".req-needs");
  const items = [...box.querySelectorAll("li")].map(li => li.textContent);
  if (!/What this download needs/.test(box.textContent)) fail("no needs heading: " + box.innerHTML);
  if (items.length !== 2 || !items[0].startsWith("Needs VIP — Every release")) fail("needs list wrong: " + JSON.stringify(items));
  if (box.querySelector("li b b")) fail("raw text was parsed as HTML");
  if (!items[1].includes("<b>raw</b> words")) fail("raw text not shown as text: " + items[1]);
  if (!window.document.querySelector(".req-detail").hidden) fail("the plain detail line should give way to the needs list");
  const badge = window.document.querySelector(".req-st .badge");
  if (badge.textContent !== "Needs VIP +1" || !badge.classList.contains("badge-needs")) fail("badge not refreshed: " + badge.outerHTML);
}
console.log("ok: needs-and-escaping");
