/* Requests → Remove: the row goes only when the server removed every attempt.
 * An admin used to click Remove on someone else's row, the server deleted
 * nothing, and the row vanished anyway. From the repo root:
 *   npm install --no-save jsdom && node tests/js/remove-request.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

const page = (answer) => {
  const dom = new JSDOM(`<!doctype html><body><div id="toast"></div>
    <div class="req-row" data-id="12">The Captain</div></body>`,
    { url: "http://stackarr.test/requests", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.deletes = [];
  window.fetch = async (url, opts = {}) => {
    if (opts.method === "DELETE") window.deletes.push(url);
    const [status, body] = answer(url);
    return { status, headers: { get: () => "application/json" }, json: async () => body };
  };
  window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
  return window;
};

// every attempt removed: the row goes
let w = page(() => [200, { ok: true }]);
await w.Stackarr.removeRequest("12,9");
if (w.deletes.join() !== "/api/request/12,/api/request/9") fail("deletes: " + w.deletes.join());
if (w.document.querySelector(".req-row")) fail("row should be gone");

// the server refused (not yours / already gone): the row stays, and it says so
w = page(() => [404, { ok: false, error: "not found" }]);
await w.Stackarr.removeRequest("12");
if (!w.document.querySelector(".req-row")) fail("row must stay when nothing was removed");
if (!w.document.getElementById("toast").textContent.includes("Couldn't remove")) fail("no message: " + w.document.getElementById("toast").textContent);

// some removed: the row stays and the message says to reload
w = page((url) => url.endsWith("/12") ? [200, { ok: true }] : [404, { ok: false }]);
await w.Stackarr.removeRequest("12,9");
if (!w.document.querySelector(".req-row")) fail("row must stay on a partial remove");
if (!w.document.getElementById("toast").textContent.includes("reload")) fail("partial message");
console.log("ok remove-request");
