/* Stackarr front-end — vanilla JS, no build step. */
const Stackarr = (() => {
  const B = () => window.URL_BASE || "";
  const toast = (m) => {
    const el = document.getElementById("toast");
    el.textContent = m; el.classList.add("show");
    clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 3200);
  };
  const api = async (path, opts = {}) => {
    const r = await fetch(B() + path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (r.status === 401) { location.href = B() + "/login"; return null; }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  };
  const esc = (s) => (s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---- theme ----
  const applyTheme = () => {
    const light = localStorage.getItem("stackarr-theme") === "light";
    document.body.setAttribute("data-theme", light ? "light" : "dark");
    const cb = document.getElementById("ui-theme"); if (cb) cb.checked = light;
  };

  const STATE = { available: ["In library", "available"], handed: ["Sent", "handed"], queued: ["Queued", "queued"], failed: ["Failed", "failed"] };
  const discoverCard = (b) => {
    const badge = STATE[b.state];
    const meta = [b.series ? `${b.series}${b.sequence ? " #" + b.sequence : ""}` : "",
      b.runtime_hours ? `${b.runtime_hours}h` : "", b.rating ? `★ ${b.rating}` : ""].filter(Boolean).join(" · ");
    return `<div class="card">
      <div class="poster">${b.cover ? `<img src="${esc(b.cover)}" loading="lazy" alt="">` : ""}${badge ? `<span class="badge badge-${badge[1]}">${badge[0]}</span>` : ""}</div>
      <div class="card-body">
        <div class="card-title">${esc(b.title)}</div>
        <div class="card-author">${esc(b.author)}</div>
        <div class="card-meta">${esc(meta)}</div>
        <div class="card-stars" data-asin="${esc(b.asin)}">${[1,2,3,4,5].map(n => `<span onclick="Stackarr.rate('${esc(b.asin)}',${n},this)">★</span>`).join("")}</div>
        <div class="card-actions">
          <button class="btn" ${badge ? "disabled" : ""} onclick='Stackarr.request(${JSON.stringify(b).replace(/'/g, "&#39;")}, this)'>${badge ? badge[0] : "Request"}</button>
          <button class="btn ghost" onclick='Stackarr.markReadBook(${JSON.stringify({title:b.title,author:b.author}).replace(/'/g,"&#39;")}, this)'>Read it</button>
        </div>
      </div></div>`;
  };

  return {
    boot() { applyTheme(); },
    toggleTheme() {
      const light = localStorage.getItem("stackarr-theme") !== "light";
      localStorage.setItem("stackarr-theme", light ? "light" : "dark"); applyTheme();
    },

    async decide(id, verdict, btn) {
      btn.closest(".card-actions").querySelectorAll("button").forEach(b => b.disabled = true);
      const res = await api(`/api/suggestion/${id}/${verdict}`, { method: "POST" });
      if (!res) return;
      btn.closest(".card").style.opacity = .35;
      toast(verdict === "approve"
        ? (res.ok ? "Approved — sent to Chaptarr." : "Approved, but: " + (res.detail || "handoff failed"))
        : "Passed — you won't see this again.");
    },
    async runNow(btn) { btn.disabled = true; btn.textContent = "Working…"; const r = await api("/api/run-now", { method: "POST" }); if (r) { toast(`${r.added} new suggestion(s).`); location.reload(); } },
    async request(book, btn) {
      btn.disabled = true; btn.textContent = "Requesting…";
      const res = await api("/api/request", { method: "POST", body: JSON.stringify(book) });
      if (!res) return; btn.textContent = res.ok ? "Sent" : "Failed"; toast(res.detail || "Done.");
    },
    async rate(asin, stars, el) {
      const wrap = el.parentElement; [...wrap.children].forEach((s, i) => s.classList.toggle("on", i < stars));
      await api("/api/rate", { method: "POST", body: JSON.stringify({ asin, stars }) });
      toast(`Rated ${stars}★ — your picks just got sharper.`);
    },
    async markRead(btn) {
      const t = document.getElementById("mr-title").value.trim(); if (!t) return;
      const a = document.getElementById("mr-author").value.trim();
      btn.disabled = true; const r = await api("/api/mark-read", { method: "POST", body: JSON.stringify({ title: t, author: a }) });
      btn.disabled = false;
      if (r && r.ok) { toast(`Noted “${r.matched}” as read.`); document.getElementById("mr-title").value = ""; document.getElementById("mr-author").value = ""; }
    },
    async markReadBook(book, btn) { btn.disabled = true; const r = await api("/api/mark-read", { method: "POST", body: JSON.stringify(book) }); if (r && r.ok) toast(`Noted “${r.matched}” as read.`); },
    async retry(id) { const r = await api(`/api/request/${id}/retry`, { method: "POST" }); if (r) location.reload(); },
    async removeRequest(id) { await api(`/api/request/${id}`, { method: "DELETE" }); document.querySelector(`.req-row[data-id="${id}"]`)?.remove(); },

    async setSetting(obj) { await api("/api/settings", { method: "POST", body: JSON.stringify(obj) }); toast("Saved."); },
    pickEmailTheme(theme, btn) {
      document.querySelectorAll(".theme-tab").forEach(t => t.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("email-preview").src = B() + "/api/email/preview/" + theme;
      this.setSetting({ email_theme: theme });
    },
    initSettings(theme) { applyTheme(); const f = document.getElementById("email-preview"); if (f) f.src = B() + "/api/email/preview/" + theme; },

    initDiscover() {
      applyTheme();
      api("/api/discover").then(books => { if (books) document.getElementById("discover").innerHTML = books.map(discoverCard).join(""); });
      const q = document.getElementById("q"), results = document.getElementById("results"), disc = document.getElementById("discover");
      let timer, seq = 0;
      q.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
          const text = q.value.trim(); const mine = ++seq;
          if (!text) { results.innerHTML = ""; disc.style.display = ""; return; }
          const books = await api("/api/search?q=" + encodeURIComponent(text));
          if (!books || mine !== seq) return; disc.style.display = "none";
          results.innerHTML = books.map(discoverCard).join("") || `<div class="empty"><p>No results.</p></div>`;
        }, 350);
      });
    },
  };
})();
Stackarr.boot();
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register((window.URL_BASE || "") + "/sw.js").catch(() => {});
}
