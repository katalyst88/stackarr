/* The "Next in <series>" strip on the book page: three cards after what
 * you've read, each with its state badge, a Request button only on a book
 * that's missing, catalogue strings escaped, and nothing shown at all when the
 * series can't be placed. From the repo root:
 *   npm install --no-save jsdom && node tests/js/series-strip.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

const NEXT = {
  ok: true, series: "The Last Horizon", series_asin: "B0BVQ26GG1", author: "Will Wight",
  after: 1, after_title: "The Captain", formats: ["audiobook"], multi: false,
  next: [
    { seq: 2, asin: "B0CL1HY6G4", title: "The Engineer", author: "Will Wight", cover: "https://m.media-amazon.com/images/I/x._SL500_.jpg", state: "available" },
    { seq: 3, asin: "B0D3915Q9H", title: "The Knight", author: "Will Wight", cover: "", state: "handed" },
    { seq: 4, asin: "B0F8CJQZM3", title: "The Pilot <img src=x onerror=alert(1)>", author: "Will Wight", cover: "", state: "missing" },
  ],
};

const page = (data) => {
  const dom = new JSDOM(`<!doctype html><body><div id="toast"></div>
    <div class="book-detail"></div>
    <section class="section series-strip-wrap" id="series-next" data-series-strip hidden
      data-asin="B0BVMW49D1" data-series-asin="B0BVQ26GG1" data-series="The Last Horizon"
      data-author="Will Wight" data-title="The Captain" data-seq="1.0"></section></body>`,
    { url: "http://stackarr.test/book/B0BVMW49D1", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.calls = [];
  window.fetch = async (url) => {
    window.calls.push(url);
    return { status: 200, headers: { get: () => "application/json" }, json: async () => data };
  };
  window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
  return window;
};

// 1. renders the strip from /api/series/next with the book's series target
let w = page(NEXT);
await w.Stackarr.loadSeriesStrip();
const el = w.document.getElementById("series-next");
const url = w.calls.find(u => u.startsWith("/api/series/next"));
if (!url) fail("strip never asked /api/series/next: " + JSON.stringify(w.calls));
for (const p of ["asin=B0BVMW49D1", "series_asin=B0BVQ26GG1", "seq=1.0"])
  if (!url.includes(p)) fail(`series target missing ${p}: ${url}`);
if (el.hidden) fail("strip stayed hidden");
if (!el.querySelector("h3").textContent.startsWith("Next in The Last Horizon")) fail("heading: " + el.querySelector("h3").textContent);
if (!el.querySelector("h3 .subtle").textContent.includes("after #1 The Captain")) fail("no 'after #1' context");
const cards = [...el.querySelectorAll(".series-strip .media-card")];
if (cards.length !== 3) fail("expected 3 cards, got " + cards.length);

// 2. badges per state; Request only on the missing book
const badge = (c) => (c.querySelector(".corner-badge") || {}).textContent || "";
if (badge(cards[0]) !== "In library") fail("#2 badge: " + badge(cards[0]));
if (badge(cards[1]) !== "Requested") fail("#3 badge: " + badge(cards[1]));
if (badge(cards[2]) !== "") fail("#4 should have no badge: " + badge(cards[2]));
if (cards[0].querySelector("button") || cards[1].querySelector("button")) fail("Request shown on an owned/requested book");
const btn = cards[2].querySelector("button");
if (!btn || btn.textContent !== "Request") fail("missing book has no Request button");
if (!/Stackarr\.request\(/.test(btn.getAttribute("onclick") || "")) fail("Request button isn't wired to Stackarr.request");

// 3. catalogue strings can't inject markup; coverless books use the local cover endpoint
if (el.querySelector(".series-strip img[onerror]")) fail("title injected an <img onerror>");
if (!cards[2].querySelector(".media-title").textContent.includes("<img src=x")) fail("title text not preserved literally");
const src = cards[1].querySelector(".media-poster img").getAttribute("src");
if (!src.startsWith("/coverart?") || !src.includes("title=The+Knight")) fail("coverless card src: " + src);
if (cards[0].querySelector(".media-poster").getAttribute("href") !== "/book/B0CL1HY6G4") fail("card doesn't link to its book");

// 4. a series that can't be placed shows nothing
w = page({ ok: false });
await w.Stackarr.loadSeriesStrip();
if (!w.document.getElementById("series-next").hidden) fail("unplaceable series still shown");

// 5. Home "Continue" card uses the same strip
const CONT = { ...NEXT, finished_title: "The Captain", finished_days: 3 };
w = page(CONT);
w.document.body.insertAdjacentHTML("beforeend", '<section id="continue-series" data-series-strip hidden></section>');
await w.Stackarr.loadContinueSeries();
const home = w.document.getElementById("continue-series");
if (home.hidden || !home.querySelector("h3").textContent.includes("Continue The Last Horizon")) fail("Home continue card not shown");
if (!home.querySelector("h3 .subtle").textContent.includes("3 days ago")) fail("Home card lacks the finish context");

console.log("PASS: series strip renders 3 cards with state badges | Request only on missing | escaped | hidden when unplaceable | Home continue card");
