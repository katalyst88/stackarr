/* "Request remaining": the confirm dialog lists exactly the books (and
 * formats) that will be requested, skips finished / owned / requested /
 * unreleased ones with a reason, and posts only the books left ticked. Cancel
 * sends nothing. From the repo root:
 *   npm install --no-save jsdom && node tests/js/request-remaining.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const tick = () => new Promise(r => setTimeout(r, 0));

const B = (seq, title, state, extra = {}) => ({
  seq, title, asin: "B0TEST000" + seq, key: "B0TEST000" + seq, author: "Will Wight", cover: "",
  owned: [], requested: [], finished: false, upcoming: false, state, ...extra });
const BOOKS = [
  B(1, "The Captain", "finished", { finished: true, owned: ["audiobook"] }),
  B(2, "The Engineer", "available", { owned: ["audiobook"] }),
  B(3, "The Knight", "handed", { requested: ["audiobook"] }),
  B(4, "The Pilot <b>bold</b>", "missing"),
  B(5, "The Commander", "upcoming", { upcoming: true, release_date: "2026-12-01" }),
  B(6, "The Sixth", "missing"),
];
const DATA = (formats) => ({ ok: true, series: "The Last Horizon", series_asin: "B0BVQ26GG1", author: "Will Wight",
  after: 1, after_title: "The Captain", books: BOOKS, next: BOOKS.slice(1, 4), formats, multi: formats.length > 1,
  cap: 10, needs_approval: false });

const page = (data) => {
  const dom = new JSDOM(`<!doctype html><body><div id="toast"></div>
    <div class="book-detail"></div>
    <section class="section series-strip-wrap" id="series-next" data-series-strip hidden data-asin="B0TEST0002"
      data-series-asin="B0BVQ26GG1" data-series="The Last Horizon" data-author="Will Wight"></section>
    <div id="modal" class="modal" hidden><div class="modal-card"><h3 id="modal-title"></h3><div id="modal-body"></div>
      <div class="modal-actions"><button class="btn ghost" type="button">Cancel</button>
      <button class="btn" type="button">Continue</button></div></div></div></body>`,
    { url: "http://stackarr.test/book/B0TEST0002", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.posts = []; window.gets = [];
  window.fetch = async (url, opts = {}) => {
    if ((opts.method || "GET") === "POST") window.posts.push({ url, body: JSON.parse(opts.body) });
    else window.gets.push(url);
    const body = url.startsWith("/api/series/next") ? data : { ok: true, detail: "Requested 1 book." };
    return { status: 200, headers: { get: () => "application/json" }, json: async () => body };
  };
  window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
  return window;
};
const $ = (w, s) => w.document.querySelector(s);
const $$ = (w, s) => [...w.document.querySelectorAll(s)];
const okBtn = (w) => $(w, "#modal .modal-actions .btn:not(.ghost)");

// --- single-format install --------------------------------------------------
let w = page(DATA(["audiobook"]));
await w.Stackarr.loadSeriesStrip();
const btn = $$(w, "#series-next .series-strip-head button")[0];
if (!btn || btn.textContent !== "Request remaining 2") fail("strip button: " + (btn && btn.textContent));

// 1. the dialog lists EVERY book in series order; requestable ones ticked,
//    the rest greyed with a disabled checkbox and the reason in the row
let done = w.Stackarr.requestRemaining(btn);
await tick();
if ($(w, "#modal").hidden) fail("confirm dialog didn't open");
if (!$(w, "#modal-title").textContent.includes("The Last Horizon")) fail("dialog title: " + $(w, "#modal-title").textContent);
let items = $$(w, "#modal-body .rr-item");
const val = (i) => i.querySelector("input").value;
if (items.map(val).join() !== BOOKS.map(b => b.key).join()) fail("not every book, in order: " + items.map(val).join());
const enabled = items.filter(i => !i.querySelector("input").disabled);
if (enabled.map(val).join() !== "B0TEST0004,B0TEST0006") fail("selectable: " + enabled.map(val).join());
if (!enabled.every(i => i.querySelector("input").checked)) fail("requestable books should start ticked");
const off = Object.fromEntries(items.filter(i => i.querySelector("input").disabled).map(i => [val(i), i]));
const want = { B0TEST0001: "In library · Finished", B0TEST0002: "In library", B0TEST0003: "Requested", B0TEST0005: "Out 2026-12-01" };
for (const [k, label] of Object.entries(want)) {
  const r = off[k];
  if (!r) fail(`${k} should be greyed out`);
  if (r.querySelector("input").checked) fail(`${k} is disabled but ticked`);
  if (!r.classList.contains("is-off") || r.getAttribute("aria-disabled") !== "true") fail(`${k} lacks is-off / aria-disabled`);
  if (r.querySelector(".rr-status").textContent !== label) fail(`${k} status "${r.querySelector(".rr-status").textContent}" != "${label}"`);
}
if ($(w, "#modal-body .rr-skip")) fail("the separate 'Skipping …' sentence should be gone");
if ($(w, "#modal-body .rr-fmt")) fail("format picker shown on a single-format install");
if ($(w, "#modal-body b")) fail("a catalogue title injected markup into the dialog");
if (!$(w, "#modal-body .rr-intro").textContent.includes("nothing else by Will Wight")) fail("no 'nothing else by' promise");
if (okBtn(w).textContent !== "Request 2") fail("OK label: " + okBtn(w).textContent);

// 2. untick one → only the ticked book is posted
enabled[1].querySelector("input").checked = false;
enabled[1].querySelector("input").dispatchEvent(new w.Event("change", { bubbles: true }));
if (okBtn(w).textContent !== "Request 1") fail("OK label after untick: " + okBtn(w).textContent);
w.Stackarr._modalOk();
await done;
if (w.posts.length !== 1) fail("expected one POST, got " + w.posts.length);
const sent = w.posts[0];
if (sent.url !== "/api/series/request-remaining") fail("posted to " + sent.url);
if (JSON.stringify(sent.body.keys) !== '["B0TEST0004"]') fail("posted keys " + JSON.stringify(sent.body.keys));
if (sent.body.format !== "audiobook" || sent.body.series_asin !== "B0BVQ26GG1" || sent.body.asin !== "B0TEST0002")
  fail("posted target " + JSON.stringify(sent.body));
if (!$(w, "#modal").hidden) fail("dialog left open");
if (okBtn(w).disabled) fail("shared modal OK button left disabled");

// 3. cancel sends nothing
w = page(DATA(["audiobook"]));
await w.Stackarr.loadSeriesStrip();
done = w.Stackarr.requestRemaining($(w, "#series-next .series-strip-head button"));
await tick();
w.Stackarr._modalCancel();
await done;
if (w.posts.length) fail("cancel still posted");

// --- both formats: per-book formats, and the picker re-plans -----------------
w = page(DATA(["audiobook", "ebook"]));
await w.Stackarr.loadSeriesStrip();
done = w.Stackarr.requestRemaining($(w, "#series-next .series-strip-head button"));
await tick();
const row = (k) => $$(w, "#modal-body .rr-item").find(i => i.querySelector("input").value === k);
if (!$(w, "#modal-body .rr-fmt input[value=both]").checked) fail("'Both' should be the default pick");
if (!row("B0TEST0002") || row("B0TEST0002").querySelector("input").disabled || row("B0TEST0002").querySelector(".rr-fmts").textContent !== "eBook")
  fail("#2 (audiobook owned) should be listed for its eBook only");
if (row("B0TEST0004").querySelector(".rr-fmts").textContent !== "audiobook + eBook") fail("#4 formats");
const audio = $(w, "#modal-body .rr-fmt input[value=audiobook]");
audio.checked = true; audio.dispatchEvent(new w.Event("change", { bubbles: true }));
if (!row("B0TEST0002").querySelector("input").disabled || row("B0TEST0002").querySelector(".rr-status").textContent !== "In library")
  fail("switching to audiobook should grey out #2 (owned as audiobook)");
w.Stackarr._modalOk();
await done;
if (w.posts[0].body.format !== "audiobook") fail("posted format " + w.posts[0].body.format);

// --- Up Next card: no strip, the series is fetched by name first --------------
w = page(DATA(["audiobook"]));
w.document.body.insertAdjacentHTML("beforeend", '<button id="upnext">Request remaining…</button>');
done = w.Stackarr.requestRemaining($(w, "#upnext"), { series: "The Last Horizon", author: "Will Wight", asin: "", title: "The Captain" });
await new Promise(r => setTimeout(r, 10));
const g = w.gets.find(u => u.startsWith("/api/series/next"));
if (!g || !g.includes("series=The+Last+Horizon") || !g.includes("author=Will+Wight")) fail("Up Next lookup: " + g);
if ($(w, "#modal").hidden) fail("Up Next confirm didn't open");
w.Stackarr._modalOk();
await done;
if (JSON.stringify(w.posts[0].body.keys) !== '["B0TEST0004","B0TEST0006"]') fail("Up Next posted " + JSON.stringify(w.posts[0].body.keys));

// --- cap: past 10, books are listed unticked but selectable; ticking an 11th is refused
const LONG = Array.from({ length: 12 }, (_, i) => B(i + 1, `Long ${i + 1}`, "missing"));
w = page({ ...DATA(["audiobook"]), books: LONG, next: LONG.slice(0, 3) });
await w.Stackarr.loadSeriesStrip();
done = w.Stackarr.requestRemaining($(w, "#series-next .series-strip-head button"));
await tick();
const boxes = $$(w, "#modal-body .rr-item input");
if (boxes.filter(b => b.checked).length !== 10 || boxes.some(b => b.disabled)) fail("cap: expected 10 ticked + 2 unticked selectable");
if (!$(w, "#modal-body .rr-note").textContent.includes("Up to 10")) fail("cap note missing");
const flip = (b, on) => { b.checked = on; b.dispatchEvent(new w.Event("change", { bubbles: true })); };
flip(boxes[10], true);
if (boxes[10].checked) fail("an 11th tick past the cap was accepted");
flip(boxes[0], false); flip(boxes[11], true);
if (!boxes[11].checked || okBtn(w).textContent !== "Request 10") fail("swap within the cap refused: " + okBtn(w).textContent);
w.Stackarr._modalOk();
await done;
const k = w.posts[0].body.keys;
if (k.length !== 10 || k.includes("B0TEST0001") || !k.includes("B0TEST00012")) fail("cap posted " + JSON.stringify(k));

console.log("PASS: cap 10 (rest unticked+selectable, 11th refused) | every book listed in order, skipped ones greyed + disabled with the reason | untick respected | cancel posts nothing | per-format plan + picker | Up Next path");
