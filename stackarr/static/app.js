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

  const applyTheme = () => {
    const light = localStorage.getItem("stackarr-theme") === "light";
    document.body.setAttribute("data-theme", light ? "light" : "dark");
  };

  // Seerr-style tag system: none -> Requested -> Available
  const TAG = {
    available: ["Available", "available", "In library"],
    handed:    ["Requested", "handed",   "Requested"],
    queued:    ["Requested", "queued",   "Requested"],
    failed:    ["Failed",    "failed",   "Retry"],
  };

  const mediaCard = (b) => {
    const tag = TAG[b.state];
    const reason = b.reason || [b.series ? `${b.series}${b.sequence ? " #" + b.sequence : ""}` : "",
      b.runtime_hours ? `${b.runtime_hours}h` : "", b.rating ? `★ ${b.rating}` : ""].filter(Boolean).join(" · ");
    const canReq = !tag || tag[1] === "failed";
    const reqLabel = tag ? tag[2] : "Request";
    const j = (o) => JSON.stringify(o).replace(/'/g, "&#39;");
    return `<div class="media-card">
      <div class="media-poster">
        ${b.cover ? `<img src="${esc(b.cover)}" loading="lazy" alt="">` : ""}
        ${tag ? `<span class="corner-badge ${tag[1]}">${tag[0]}</span>` : ""}
        <div class="media-overlay">
          <div class="ov-reason">${esc(reason)}</div>
          <div class="media-stars" data-asin="${esc(b.asin)}">${[1,2,3,4,5].map(n => `<span onclick="Stackarr.rate('${esc(b.asin)}',${n},this)">★</span>`).join("")}</div>
          <div class="ov-actions">
            <button class="btn" ${canReq ? "" : "disabled"} onclick='Stackarr.request(${j(b)}, this)'>${esc(reqLabel)}</button>
            <button class="btn ghost" onclick='Stackarr.markReadBook(${j({title:b.title,author:b.author})}, this)'>Read it</button>
          </div>
        </div>
      </div>
      <div class="media-foot">
        <div class="media-title" title="${esc(b.title)}">${esc(b.title)}</div>
        <div class="media-author">${esc(b.author)}</div>
      </div>
    </div>`;
  };

  let pollTimer = null;
  const startLoaderPoll = (firstRun) => {
    const loader = document.getElementById("shelf-loader");
    const content = document.getElementById("sugg-content");
    const empty = document.getElementById("empty-state");
    if (loader) loader.hidden = false;
    if (content) content.hidden = true;
    let waited = 0;
    const tick = async () => {
      const s = await api("/api/suggestions/status");
      if (!s) return;
      waited += 2;
      if (s.pending > 0) { location.reload(); return; }
      if (!s.running && (waited > 4 || !firstRun)) {           // done, nothing produced
        if (loader) loader.hidden = true;
        if (content) { content.hidden = false; }
        if (empty) empty.hidden = false;
        clearInterval(pollTimer);
      }
    };
    pollTimer = setInterval(tick, 2000); tick();
  };

  return {
    boot() { applyTheme(); },
    toggleTheme() {
      localStorage.setItem("stackarr-theme", localStorage.getItem("stackarr-theme") === "light" ? "dark" : "light");
      applyTheme();
    },

    initSuggestions(noLanes) {
      applyTheme();
      // if nothing shown, either a run is happening (loader) or truly empty
      if (noLanes) {
        api("/api/suggestions/status").then(s => {
          if (s && (s.running || s.pending === 0)) startLoaderPoll(s ? s.running : true);
        });
      }
    },
    async scan() {
      await api("/api/run-now", { method: "POST" });
      startLoaderPoll(true);
    },

    async decide(id, verdict, btn) {
      btn.closest(".ov-actions").querySelectorAll("button").forEach(b => b.disabled = true);
      const res = await api(`/api/suggestion/${id}/${verdict}`, { method: "POST" });
      if (!res) return;
      const card = btn.closest(".media-card");
      card.style.transition = "opacity .3s, transform .3s"; card.style.opacity = .25; card.style.transform = "scale(.9)";
      toast(verdict === "approve"
        ? (res.ok ? "Approved — sent to Chaptarr." : "Approved, but: " + (res.detail || "handoff failed"))
        : "Ignored — you won't see this again.");
    },

    async request(book, btn) {
      btn.disabled = true; btn.textContent = "Requesting…";
      const res = await api("/api/request", { method: "POST", body: JSON.stringify(book) });
      if (!res) return; btn.textContent = res.ok ? "Requested" : "Failed"; toast(res.detail || "Done.");
    },
    async rate(asin, stars, el) {
      [...el.parentElement.children].forEach((s, i) => s.classList.toggle("on", i < stars));
      await api("/api/rate", { method: "POST", body: JSON.stringify({ asin, stars }) });
      toast(`Rated ${stars}★ — your picks just got sharper.`);
    },
    async markRead(btn) {
      const t = document.getElementById("mr-title").value.trim(); if (!t) return;
      const a = document.getElementById("mr-author").value.trim();
      btn.disabled = true;
      const r = await api("/api/mark-read", { method: "POST", body: JSON.stringify({ title: t, author: a }) });
      btn.disabled = false;
      if (r && r.ok) { toast(`Noted “${r.matched}” as read.`); document.getElementById("mr-title").value = ""; document.getElementById("mr-author").value = ""; }
    },
    async markReadBook(book, btn) { btn.disabled = true; const r = await api("/api/mark-read", { method: "POST", body: JSON.stringify(book) }); if (r && r.ok) toast(`Noted “${r.matched}” as read.`); },
    async retry(id) { const r = await api(`/api/request/${id}/retry`, { method: "POST" }); if (r) location.reload(); },
    async removeRequest(id) { await api(`/api/request/${id}`, { method: "DELETE" }); document.querySelector(`.req-row[data-id="${id}"]`)?.remove(); },

    async setSetting(obj) { await api("/api/settings", { method: "POST", body: JSON.stringify(obj) }); toast("Saved."); },
    pickEmailTheme(theme, btn) {
      document.querySelectorAll(".theme-tab").forEach(t => t.classList.remove("active")); btn.classList.add("active");
      const f = document.getElementById("email-preview"); if (f) f.src = B() + "/api/email/preview/" + theme;
      this.setSetting({ email_theme: theme });
    },
    settingsCat(cat, el) {
      document.querySelectorAll(".settings-nav button").forEach(b => b.classList.toggle("active", b === el));
      document.querySelectorAll(".settings-cat").forEach(s => s.classList.toggle("active", s.id === "cat-" + cat));
    },
    subTab(group, name, el) {
      el.parentElement.querySelectorAll(".sub-tab").forEach(b => b.classList.remove("active")); el.classList.add("active");
      document.querySelectorAll(`.sub-panel[data-group="${group}"]`).forEach(p => p.classList.toggle("active", p.dataset.panel === name));
    },
    initSettings(theme) {
      applyTheme();
      const f = document.getElementById("email-preview"); if (f) f.src = B() + "/api/email/preview/" + theme;
    },

    initDiscover() {
      applyTheme();
      const params = new URLSearchParams(location.search);
      const pre = params.get("q");
      const q = document.getElementById("q"), results = document.getElementById("results"),
            disc = document.getElementById("discover"), rhead = document.getElementById("results-head");
      api("/api/discover").then(books => { if (books) document.getElementById("discover").innerHTML = books.map(mediaCard).join(""); });
      let timer, seq = 0;
      const doSearch = async (text) => {
        const mine = ++seq;
        if (!text) { results.innerHTML = ""; rhead.hidden = true; disc.style.display = ""; return; }
        const books = await api("/api/search?q=" + encodeURIComponent(text));
        if (!books || mine !== seq) return;
        disc.style.display = "none"; rhead.hidden = false;
        results.innerHTML = books.map(mediaCard).join("") || `<div class="empty"><p>No results.</p></div>`;
      };
      if (q) { q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => doSearch(q.value.trim()), 350); });
        if (pre) { q.value = pre; doSearch(pre); } }
      else if (pre) doSearch(pre);
    },
  };
})();
Stackarr.boot();
if ("serviceWorker" in navigator) navigator.serviceWorker.register((window.URL_BASE || "") + "/sw.js").catch(() => {});
