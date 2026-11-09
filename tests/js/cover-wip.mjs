/* A cover answered with the "Fetching cover…" placeholder is asked for again
 * (cache-busted) until the real art arrives; after the last try it shows the
 * designed "No cover" art — never a broken image.
 *   npm install --no-save jsdom && node tests/js/cover-wip.mjs
 */
import { JSDOM } from "jsdom";
import { readFileSync } from "node:fs";

const appJs = process.argv[2] || "./stackarr/static/app.js";
const dom = new JSDOM(`<!doctype html><body><div id="toast"></div>
  <div class="media-poster"><img src="/coverart?title=Some+Book&author=Someone"></div></body>`,
  { url: "http://stackarr.test/home", runScripts: "outside-only" });
const { window } = dom;
window.fetch = async () => ({ status: 200, headers: { get: () => "application/json" }, json: async () => [] });
window.eval(readFileSync(appJs, "utf8") + "\n;window.Stackarr = Stackarr;");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const S = window.Stackarr;
S.WIP_RETRY_S = [0.01, 0.01];
const img = window.document.querySelector("img");
// what the browser has after the /coverart redirect: the URL is still
// /coverart (currentSrc doesn't follow redirects); the art is the 301x301 marker
Object.defineProperty(img, "naturalWidth", { get: () => 301, configurable: true });
Object.defineProperty(img, "naturalHeight", { get: () => 301, configurable: true });
Object.defineProperty(img, "complete", { get: () => true, configurable: true });
img.dispatchEvent(new window.Event("load"));      // boot() wired it; the marker -> first re-ask
await new Promise(r => setTimeout(r, 60));
if (!img.getAttribute("src").startsWith("/coverart?title=Some+Book&author=Someone&_r=1")) fail("not re-asked: " + img.getAttribute("src"));
S._repoll(img);
await new Promise(r => setTimeout(r, 60));
if (!img.getAttribute("src").endsWith("&_r=2")) fail("second re-ask: " + img.getAttribute("src"));
S._repoll(img);                                    // out of tries
if (!img.getAttribute("src").endsWith("cover-placeholder.svg")) fail("no final placeholder: " + img.getAttribute("src"));
console.log("PASS: WIP covers are re-asked, then fall back to the designed placeholder");
