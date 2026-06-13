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
  const hires = (u) => (u || "").replace(/\._S[XYL]\d+_\./, "._SL1500_.");

  const applyTheme = () => {
    const light = localStorage.getItem("stackarr-theme") === "light";
    document.body.setAttribute("data-theme", light ? "light" : "dark");
  };

  // Seer-style tag system: none -> Requested -> Available
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
        ${b.cover ? `<img src="${esc(hires(b.cover))}" loading="lazy" alt="">` : ""}
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
    boot() {
      applyTheme();
      if (localStorage.getItem("stackarr-nav") === "collapsed") document.body.classList.add("nav-collapsed");
      document.body.classList.add("loaded");
      this.initSearchSuggest();
      this.fitCovers();
      this.wireStarHover();
    },
    // Hover-preview: light up stars from the left up to the cursor (CSS can't
    // select preceding siblings, so do it here).
    wireStarHover() {
      document.querySelectorAll(".rate-stars:not(.disabled)").forEach(row => {
        const stars = [...row.querySelectorAll(".star")];
        stars.forEach((star, i) => {
          star.addEventListener("mouseenter", () => stars.forEach((s, j) => s.classList.toggle("preview", j <= i)));
        });
        row.addEventListener("mouseleave", () => stars.forEach(s => s.classList.remove("preview")));
      });
    },
    fitImg(img) {
      const w = img.naturalWidth, h = img.naturalHeight;
      // only non-square covers get black bars (letterbox); square ones fill
      img.classList.toggle("letterbox", !!(w && h) && Math.abs(w - h) / Math.max(w, h) > 0.03);
    },
    fitCovers() {
      document.querySelectorAll(".media-poster img").forEach(img => {
        if (img.complete && img.naturalWidth) this.fitImg(img);
        else img.addEventListener("load", () => this.fitImg(img), { once: true });
      });
    },
    initSearchSuggest() {
      const inp = document.getElementById("topsearch"), box = document.getElementById("search-suggest");
      if (!inp || !box) return;
      let t;
      inp.addEventListener("input", () => {
        clearTimeout(t);
        const q = inp.value.trim();
        if (q.length < 2) { box.classList.remove("open"); return; }
        t = setTimeout(async () => {
          const rs = await api("/api/suggest?q=" + encodeURIComponent(q));
          if (!rs) return;
          box.innerHTML = rs.map(b => `<a class="ss-item" href="${B()}/book/${encodeURIComponent(b.asin)}">
            <img src="${esc(hires(b.cover))}" alt=""><div style="min-width:0"><div class="ss-title">${esc(b.title)}</div>
            <div class="ss-sub">${esc(b.author)}${b.series ? " · " + esc(b.series) : ""}</div></div></a>`).join("");
          box.classList.toggle("open", rs.length > 0);
        }, 250);
      });
      document.addEventListener("click", e => { if (!box.contains(e.target) && e.target !== inp) box.classList.remove("open"); });
    },
    async bookRequest(b, btn) { btn.disabled = true; btn.textContent = "Requesting…"; const r = await api("/api/request", { method: "POST", body: JSON.stringify(b) }); if (r) { btn.textContent = r.ok ? "Requested" : "Failed"; toast(r.detail || "Done."); } },
    async bookMarkRead(b, btn) { btn.disabled = true; await api("/api/markread-book", { method: "POST", body: JSON.stringify(b) }); toast("Marked as read — your picks will improve."); },
    async bookIgnore(b, btn) { btn.disabled = true; await api("/api/ignore", { method: "POST", body: JSON.stringify(b) }); toast("Ignored — you won't see this again."); },
    async addAllByAuthor(author, btn) {
      btn.disabled = true; btn.textContent = "Adding…";
      const r = await api("/api/author/add", { method: "POST", body: JSON.stringify({ author }) });
      btn.textContent = r && r.ok ? "Added to Chaptarr" : "Failed";
      if (r) toast(r.detail || "Done.");
    },
    toggleNav() {
      const c = !document.body.classList.contains("nav-collapsed");
      document.body.classList.toggle("nav-collapsed", c);
      localStorage.setItem("stackarr-nav", c ? "collapsed" : "open");
    },
    slide(btn, dir) {
      const s = btn.parentElement.querySelector(".slider");
      if (s) s.scrollBy({ left: dir * s.clientWidth * 0.82, behavior: "smooth" });
    },
    filterFormat(fmt, pill) {
      document.querySelectorAll("#fmt-filter .fmt-pill").forEach(p => p.classList.toggle("active", p === pill));
      try { localStorage.setItem("stackarr-fmt", fmt); } catch (e) {}
      // filter every format-tagged item on the page (cards, list rows, series cards…)
      document.querySelectorAll("[data-format]").forEach(el => {
        el.style.display = (fmt === "all" || el.dataset.format === fmt) ? "" : "none";
      });
      // hide any group wrapper (lane section, etc.) left with no visible items
      document.querySelectorAll(".fmt-group").forEach(g => {
        const items = g.querySelectorAll("[data-format]");
        if (!items.length) return;
        g.style.display = [...items].some(i => i.style.display !== "none") ? "" : "none";
      });
    },
    initFormatFilter() {
      // re-apply the last chosen format on load so it sticks across pages
      const f = (() => { try { return localStorage.getItem("stackarr-fmt"); } catch (e) { return null; } })();
      if (!f || f === "all") return;
      const pill = document.querySelector(`#fmt-filter .fmt-pill[data-fmt="${f}"]`);
      if (pill) this.filterFormat(f, pill);
    },
    async getSeries(name, author, btn) {
      if (!author) { toast("No author found for this series."); return; }
      if (btn) { btn.disabled = true; btn.textContent = "Sending…"; }
      const r = await api("/api/series/add", { method: "POST", body: JSON.stringify({ series: name, author }) });
      toast(r.detail || (r.ok ? "Sent to Chaptarr." : "Couldn't add right now."));
      if (btn) { btn.disabled = false; btn.textContent = r.ok ? "✓ Requested" : "＋ Get full series"; }
    },
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
        : verdict === "read" ? "Marked as read — your picks will improve."
        : "Ignored — you won't see this again.");
    },

    async request(book, btn) {
      btn.disabled = true; btn.textContent = "Requesting…";
      const res = await api("/api/request", { method: "POST", body: JSON.stringify(book) });
      if (!res) return; btn.textContent = res.ok ? "Requested" : "Failed"; toast(res.detail || "Done.");
    },
    async rate(asin, stars, el) {
      const row = el.parentElement;   // .rate-stars / .media-stars
      [...row.children].forEach((s, i) => { s.classList.toggle("on", i < stars); s.classList.remove("preview", "pop"); });
      requestAnimationFrame(() => [...row.children].forEach((s, i) => { if (i < stars) s.classList.add("pop"); }));
      const item = el.closest(".rate-item");
      // Library books usually have no real ASIN, so send title/author too — the
      // recommender boosts on author, and api_rate keeps them on the rating.
      // Prefer data-* on the stars row; fall back to the History row's text.
      const payload = { asin, stars };
      const title = row.dataset.title || (item && item.querySelector(".rate-title")?.textContent.trim());
      const author = row.dataset.author || (item && item.querySelector(".rate-author")?.textContent.trim());
      if (title) payload.title = title;
      if (author) payload.author = author;
      await api("/api/rate", { method: "POST", body: JSON.stringify(payload) });
      toast(`Rated ${stars}★ — your picks just got sharper.`);
      // History list: rate → remove (if "hide after rating" on) or sink to bottom.
      if (item) {
        const list = item.parentElement;
        if (list.dataset.hideRated === "1") this._removeRated(item);
        else this._sinkRated(item);
      }
      // Onboarding card: tick it off and bump the counter.
      const onb = el.closest(".onboard-item");
      if (onb) this._onboardRated(onb);
    },
    async submitReview(btn) {
      const sec = document.getElementById("reviews");
      const starsRow = document.getElementById("my-stars");
      const stars = starsRow ? starsRow.querySelectorAll(".star.on").length : 0;
      if (!stars) { toast("Pick a star rating first."); return; }
      const review = (document.getElementById("my-review-text")?.value || "").trim();
      if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
      const spoiler = !!(document.getElementById("my-review-spoiler") || {}).checked;
      await api("/api/rate", { method: "POST", body: JSON.stringify({
        asin: sec.dataset.key, stars, review, spoiler,
        title: sec.dataset.title, author: sec.dataset.author, format: sec.dataset.format }) });
      if (btn) { btn.disabled = false; btn.textContent = "Save review"; }
      toast("Review saved — thanks for sharing.");
    },
    async setShelf(state, btn) {
      const bar = btn.closest(".shelf-bar");
      const on = btn.classList.contains("on");
      const next = on ? "" : state;
      bar.querySelectorAll(".shelf-btn").forEach(b => b.classList.toggle("on", b === btn && !on));
      await api("/api/shelf", { method: "POST", body: JSON.stringify({
        key: bar.dataset.key, state: next, title: bar.dataset.title,
        author: bar.dataset.author, cover: bar.dataset.cover, format: bar.dataset.format }) });
      toast(next ? `On your "${state}" shelf.` : "Removed from shelves.");
    },
    async getOtherFormat(book, btn) {
      if (btn) { btn.disabled = true; }
      const r = await api("/api/get-other-format", { method: "POST", body: JSON.stringify(book) });
      toast(r.detail || (r.ok ? "Requested." : "Couldn't add."));
      if (btn) { btn.disabled = false; }
    },
    async feedback(book, direction, btn) {
      await api("/api/feedback", { method: "POST", body: JSON.stringify({
        author: book.author, title: book.title, direction, format: book.format }) });
      toast(direction === "more" ? "More like this — noted." : "Less like this — noted.");
      if (btn) { btn.classList.add("on"); }
    },
    async voteReview(id, btn) {
      const r = await api("/api/review/vote", { method: "POST", body: JSON.stringify({ rating_id: id }) });
      const c = btn.querySelector(".vcount"); if (c) c.textContent = r.votes;
      btn.classList.toggle("voted");
    },
    async pollRequestStatus() {
      try {
        const s = await api("/api/requests/status");
        for (const [id, st] of Object.entries(s || {})) {
          const row = document.querySelector(`.req-row[data-id="${id}"] .badge`);
          if (row && (st === "downloading" || st === "importing")) {
            row.textContent = st === "importing" ? "Importing" : "Downloading";
            row.className = "badge badge-handed";
          }
        }
      } catch (e) {}
    },
    async checkLibraries(btn) {
      if (btn) { btn.disabled = true; btn.textContent = "Checking…"; }
      const r = await api("/api/requests/check", { method: "POST", body: "{}" });
      toast(r.detail || "Checked.");
      if (r.flipped) setTimeout(() => location.reload(), 600);
      else if (btn) { btn.disabled = false; btn.textContent = "↻ Check libraries"; }
    },
    async follow(btn) {
      const r = await api("/api/follow", { method: "POST", body: JSON.stringify({ author: btn.dataset.author }) });
      btn.classList.toggle("on", r.following);
      btn.textContent = r.following ? "✓ Following" : "＋ Follow";
      toast(r.following ? "Following — you're on the radar." : "Unfollowed.");
    },
    async setAdventurousness(v) {
      await api("/api/adventurousness", { method: "POST", body: JSON.stringify({ value: parseInt(v, 10) }) });
      toast("Updated — your next refresh reflects it.");
    },
    async pickVibes(btn) {
      const moods = [...document.querySelectorAll(".vibe-chip.on")].map(c => c.dataset.mood);
      if (!moods.length) { toast("Pick a few vibes first."); return; }
      await api("/api/vibes", { method: "POST", body: JSON.stringify({ moods }) });
      document.getElementById("vibe-card")?.remove();
      toast("Vibes saved — updating your picks…");
      this.scan && this.scan();
    },
    toggleVibe(el) { el.classList.toggle("on"); },
    async saveGoal(btn) {
      const n = parseInt((document.getElementById("goal-input") || {}).value, 10) || 0;
      await api("/api/goal", { method: "POST", body: JSON.stringify({ goal: n }) });
      toast("Goal saved."); setTimeout(() => location.reload(), 400);
    },
    async surprise(fmt) {
      const n = Math.floor(Date.now() / 60000);   // varies each minute
      const q = new URLSearchParams({ n: String(n) });
      if (fmt) q.set("format", fmt);
      const r = await api("/api/surprise?" + q.toString());
      if (r.ok && r.book && r.book.asin) location.href = (window.URL_BASE || "") + "/book/" + r.book.asin;
      else toast("No pick right now — try Update Suggestions.");
    },
    _onboardRated(onb) {
      if (onb.dataset.done === "1") return;
      onb.dataset.done = "1";
      onb.classList.add("done");
      const cnt = document.getElementById("onboard-count");
      if (cnt) cnt.textContent = String((parseInt(cnt.textContent, 10) || 0) + 1);
    },
    async undoSignal(sid, btn) {
      const row = btn.closest(".tune-row");
      if (row) { row.style.transition = "opacity .25s, transform .25s"; row.style.opacity = 0; row.style.transform = "translateX(-8px)"; }
      await api(`/api/signal/${sid}/delete`, { method: "POST" });
      toast("Done — your picks will update.");
      setTimeout(() => row && row.remove(), 260);
    },
    async clearRating(key, btn) {
      const row = btn.closest(".tune-row");
      if (row) { row.style.transition = "opacity .25s, transform .25s"; row.style.opacity = 0; row.style.transform = "translateX(-8px)"; }
      await api("/api/rating/delete", { method: "POST", body: JSON.stringify({ key }) });
      toast("Rating cleared.");
      setTimeout(() => row && row.remove(), 260);
    },
    async markDnf(btn) {
      const t = document.getElementById("dnf-title"), a = document.getElementById("dnf-author");
      const title = t.value.trim(); if (!title) return;
      btn.disabled = true;
      const r = await api("/api/dnf", { method: "POST", body: JSON.stringify({ title, author: a.value.trim() }) });
      btn.disabled = false;
      if (r && r.ok) { toast(`Noted “${r.matched}” as did-not-finish.`); t.value = ""; a.value = ""; }
    },
    async dismissOnboard(btn) {
      const card = document.getElementById("onboard-card");
      if (card) { card.style.transition = "opacity .3s"; card.style.opacity = 0; }
      await api("/api/onboard/dismiss", { method: "POST" });
      setTimeout(() => card && card.remove(), 320);
    },
    _retally(list) {
      const sub = document.querySelector(".page-title .subtle");
      if (!sub) return;
      const total = list.querySelectorAll(".rate-item").length;
      const rated = list.querySelectorAll('.rate-item[data-rated="1"]').length;
      sub.textContent = `${total} book${total !== 1 ? "s" : ""}` + (rated ? ` · ${rated} rated` : "");
    },
    _removeRated(item) {
      const list = item.parentElement;
      item.classList.add("moving");
      setTimeout(() => { item.remove(); this._retally(list); }, 360);
    },
    async removeFromHistory(key, btn) {
      const item = btn.closest(".rate-item");
      if (item) item.classList.add("moving");
      await api("/api/history/remove", { method: "POST", body: JSON.stringify({ key }) });
      toast("Removed from history.");
      if (!item) return;
      const list = item.parentElement;
      setTimeout(() => { item.remove(); this._retally(list); }, 360);
    },
    _sinkRated(item) {
      const list = item.parentElement;
      const wasRated = item.dataset.rated === "1";
      item.dataset.rated = "1";
      item.classList.add("rated");
      if (wasRated) return;   // already in the rated pile — leave it where it is
      this._retally(list);
      // fade out, drop to the bottom of the list, fade back in
      item.classList.add("moving");
      setTimeout(() => {
        list.appendChild(item);
        requestAnimationFrame(() => item.classList.remove("moving"));
      }, 360);
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

    async setSetting(obj, reload) { await api("/api/settings", { method: "POST", body: JSON.stringify(obj) }); toast("Saved."); if (reload) setTimeout(() => location.reload(), 400); },
    _gather(catId) {
      const obj = {};
      document.querySelectorAll(`#cat-${catId} [data-setting]`).forEach(el => {
        obj[el.dataset.setting] = el.type === "checkbox" ? el.checked : el.value;
      });
      return obj;
    },
    async saveCategory(catId, btn) {
      if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
      await api("/api/settings", { method: "POST", body: JSON.stringify(this._gather(catId)) });
      if (btn) { btn.disabled = false; btn.textContent = btn.textContent.replace("Saving…", "Save"); }
      toast("Settings saved.");
    },
    async testConn(service, btn) {
      const out = document.getElementById("test-" + service);
      if (out) { out.textContent = "Testing…"; out.className = "conn-result"; }
      btn.disabled = true;
      // save the connection fields first so the test uses what's on screen
      const res = await api("/api/test/" + service, { method: "POST", body: JSON.stringify(this._gather("connections")) });
      btn.disabled = false;
      if (out && res) { out.textContent = (res.ok ? "✓ " : "✗ ") + (res.detail || ""); out.className = "conn-result " + (res.ok ? "ok" : "err"); }
    },
    pickEmailTheme(theme, btn) {
      document.querySelectorAll(".theme-tab").forEach(t => t.classList.remove("active")); btn.classList.add("active");
      const f = document.getElementById("email-preview"); if (f) f.src = B() + "/api/email/preview/" + theme;
      this.setSetting({ email_theme: theme });
    },
    settingsCat(cat, el) {
      document.querySelectorAll(".settings-nav button").forEach(b => b.classList.toggle("active", b === el));
      document.querySelectorAll(".settings-cat").forEach(s => s.classList.toggle("active", s.id === "cat-" + cat));
      if (cat === "logs") this.loadLogs();
    },
    async loadLogs() {
      const lvl = (document.getElementById("log-level") || {}).value || "INFO";
      const r = await api("/api/logs?level=" + lvl);
      const v = document.getElementById("log-view");
      if (v && r) v.textContent = (r.lines || []).join("\n") || "(no entries at this level)";
    },
    downloadLogs() {
      const lvl = (document.getElementById("log-level") || {}).value || "DEBUG";
      window.open(B() + "/api/logs/download?level=" + lvl, "_blank");
    },
    subTab(group, name, el) {
      el.parentElement.querySelectorAll(".sub-tab").forEach(b => b.classList.remove("active")); el.classList.add("active");
      document.querySelectorAll(`.sub-panel[data-group="${group}"]`).forEach(p => p.classList.toggle("active", p.dataset.panel === name));
    },
    initSettings(theme) {
      applyTheme();
      const f = document.getElementById("email-preview"); if (f) f.src = B() + "/api/email/preview/" + theme;
    },

    initDiscoverGallery() {
      const disc = document.getElementById("home-discover"), sentinel = document.getElementById("home-sentinel");
      if (!disc) return;
      let page = 0, loading = false, done = false;
      const more = async () => {
        if (loading || done) return;
        loading = true;
        const b = await api("/api/discover?page=" + page);
        loading = false;
        if (!b || !b.length) { done = true; if (sentinel) sentinel.textContent = ""; return; }
        disc.insertAdjacentHTML("beforeend", b.map(mediaCard).join(""));
        this.fitCovers();
        page++;
      };
      if (sentinel && "IntersectionObserver" in window)
        new IntersectionObserver(es => { if (es[0].isIntersecting) more(); }, { rootMargin: "700px" }).observe(sentinel);
      more();
    },
    initDiscover() {
      applyTheme();
      const params = new URLSearchParams(location.search);
      const pre = params.get("q");
      const results = document.getElementById("results"), disc = document.getElementById("discover"),
            rhead = document.getElementById("results-head"), sentinel = document.getElementById("scroll-sentinel"),
            discSec = document.getElementById("discover-section");

      // endless scroll of the discovery gallery
      let page = 0, loading = false, done = false;
      const loadMore = async () => {
        if (loading || done) return;
        loading = true;
        const books = await api("/api/discover?page=" + page);
        loading = false;
        if (!books || !books.length) { done = true; if (sentinel) sentinel.textContent = ""; return; }
        disc.insertAdjacentHTML("beforeend", books.map(mediaCard).join(""));
        page++;
      };
      if (sentinel && "IntersectionObserver" in window) {
        new IntersectionObserver((es) => { if (es[0].isIntersecting) loadMore(); },
          { rootMargin: "600px" }).observe(sentinel);
      }
      loadMore();

      // search overrides the gallery
      let timer, seq = 0;
      const doSearch = async (text) => {
        const mine = ++seq;
        if (!text) { results.innerHTML = ""; rhead.hidden = true; if (discSec) discSec.style.display = ""; return; }
        const books = await api("/api/search?q=" + encodeURIComponent(text));
        if (!books || mine !== seq) return;
        if (discSec) discSec.style.display = "none"; rhead.hidden = false;
        results.innerHTML = books.map(mediaCard).join("") || `<div class="empty"><p>No results.</p></div>`;
        Stackarr.fitCovers();
      };
      const q = document.getElementById("topsearch");
      if (q) { q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => doSearch(q.value.trim()), 350); }); }
      if (pre) doSearch(pre);
    },
  };
})();
Stackarr.boot();
if ("serviceWorker" in navigator) navigator.serviceWorker.register((window.URL_BASE || "") + "/sw.js").catch(() => {});
