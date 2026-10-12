/* Cards added after page load get the same cover handling as server-rendered
 * ones: initCovers() wires the blank-cover/404 fallback (data-cw="1"). The
 * Discover gallery's endless scroll and the search results used to skip it, so
 * a broken Audible image stayed a grey box. Also: a card's status badge and
 * Request button come from the server's status (status.py), escaped.
 *   npm install --no-save jsdom && node tests/js/covers-rebind.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";
const BOOK = (asin, status) => ({ asin, title: "Dungeon Crawler Carl", author: "Matt Dinniman",
  cover: "https://m.media-amazon.com/images/I/x._SL500_.jpg", state: status ? status.state : "none", status });

const dom = new JSDOM(`<!doctype html><body>
  <div id="toast"></div>
  <form class="search"><input id="topsearch" type="search"><div class="search-suggest" id="search-suggest"></div></form>
  <div class="section" id="results-head" hidden></div>
  <div id="results" class="grid"></div>
  <div class="section" id="discover-section"><div id="discover" class="grid"></div><div id="scroll-sentinel"></div></div>
</body>`, { url: "http://stackarr.test/discover", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
let page = 0;
window.fetch = async (url) => {
  let body = [];
  if (url.includes("/api/search")) body = [BOOK("B08V7VS8H2", { state: "downloading", label: "Downloading", cls: "downloading",
                                                                   detail: '3% <img src=x onerror="alert(1)">' })];
  else if (url.includes("/api/discover") && page++ === 0) body = [BOOK("B0934VH42B", null)];
  return { status: 200, headers: { get: () => "application/json" }, json: async () => body };
};
window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

window.Stackarr.initDiscover();
await new Promise(r => setTimeout(r, 50));
const gal = window.document.querySelector("#discover .media-poster img");
if (!gal) fail("discover gallery rendered no card");
if (gal.dataset.cw !== "1") fail("discover loadMore cards skipped initCovers()");
if (!window.document.querySelector("#discover .ov-actions .btn:not([disabled])")) fail("a book with no status should be requestable");

const input = window.document.getElementById("topsearch");
input.value = "carl"; input.dispatchEvent(new window.Event("input"));
await new Promise(r => setTimeout(r, 600));
const img = window.document.querySelector("#results .media-poster img");
if (!img) fail("search rendered no card");
if (img.dataset.cw !== "1") fail("search result cards skipped initCovers()");
const badge = window.document.querySelector("#results .corner-badge");
if (!badge || badge.textContent !== "Downloading" || !badge.classList.contains("downloading")) fail("badge: " + (badge && badge.outerHTML));
if (window.document.querySelector("#results img[src='x']")) fail("status detail was not escaped");
const btn = window.document.querySelector("#results .ov-actions .btn");
if (!btn.disabled || btn.textContent !== "Downloading") fail("a downloading book must not offer Request: " + btn.outerHTML);
console.log("PASS: dynamic cards get cover fallback + server status");
