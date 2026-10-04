/* Regression test: a Discover-page search result must be clickable — i.e. the
 * JS-rendered media card must wrap its poster in <a href="/book/<asin>">, the
 * same contract every server-rendered card template uses (browse.html,
 * lane.html, suggestions.html...). From the repo root:
 *   npm install --no-save jsdom && node tests/js/search-card.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";

const BOOK = { asin: "B07B3LTT4T", title: "Becoming", author: "Michelle Obama",
               cover: "https://m.media-amazon.com/images/I/91pVDB3YZDL._SL1500_.jpg",
               series: "", format: "audiobook", state: null };

const dom = new JSDOM(`<!doctype html><body>
  <div id="toast"></div>
  <form class="search"><input id="topsearch" type="search"><div class="search-suggest" id="search-suggest"></div></form>
  <div class="section" id="results-head" hidden></div>
  <div id="results" class="grid"></div>
  <div class="section" id="discover-section">
    <div id="discover" class="grid"></div>
    <div id="scroll-sentinel"></div>
  </div>
</body>`, { url: "http://stackarr.test/discover", runScripts: "outside-only", pretendToBeVisual: true });

const { window } = dom;
const calls = [];
window.fetch = async (url) => {
  calls.push(url);
  const body = url.includes("/api/search") ? [BOOK] : [];   // /api/discover -> empty
  return { status: 200, headers: { get: () => "application/json" }, json: async () => body };
};
// `const Stackarr` is eval-scoped, so hand it out to the test explicitly
window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");

const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

window.Stackarr.initDiscover();
const input = window.document.getElementById("topsearch");
input.value = "Becoming";
input.dispatchEvent(new window.Event("input"));

await new Promise(r => setTimeout(r, 600));   // 350ms debounce + fetch

const results = window.document.getElementById("results");
if (!calls.some(u => u.includes("/api/search"))) fail("search was never issued: " + JSON.stringify(calls));
const card = results.querySelector(".media-card");
if (!card) fail("no result card rendered; #results = " + results.innerHTML.slice(0, 200));

// 1. the card is the click target and opens the book detail page
const expected = "/book/" + encodeURIComponent(BOOK.asin);
if (!card.matches("a[href]"))
  fail("result card is not a link — clicking a search result does nothing.\n  card: " + card.outerHTML.slice(0, 300));
if (card.getAttribute("href") !== expected)
  fail(`card href is "${card.getAttribute("href")}", expected "${expected}"`);

// 2. poster and title both sit inside that link (whole card clickable)
if (!card.querySelector(".media-poster img")) fail("poster is not inside the card link");
if ((card.querySelector(".media-title") || {}).textContent !== BOOK.title) fail("title is not inside the card link");

// 3. overlay actions must NOT navigate: every in-overlay handler cancels the click
for (const el of card.querySelectorAll(".media-overlay button, .media-stars span")) {
  const h = el.getAttribute("onclick") || "";
  if (!/preventDefault\(\)/.test(h) || !/stopPropagation\(\)/.test(h))
    fail("overlay control would navigate instead of acting: " + el.outerHTML.slice(0, 160));
}

// 4. a result with no asin must degrade to a non-link card, never href="/book/"
window.document.getElementById("results").innerHTML = "";
const noAsin = { ...BOOK, asin: "" };
window.fetch = async (url) => ({ status: 200, headers: { get: () => "application/json" },
                                 json: async () => (url.includes("/api/search") ? [noAsin] : []) });
input.value = "no asin"; input.dispatchEvent(new window.Event("input"));
await new Promise(r => setTimeout(r, 600));
const bare = window.document.querySelector("#results .media-card");
if (!bare) fail("no card rendered for an asin-less result");
if (bare.matches("a[href]")) fail("asin-less result linked to a dead book page: " + bare.getAttribute("href"));

console.log("PASS: search result card links to", expected,
            "| overlay actions guarded | asin-less result degrades to a plain card");
