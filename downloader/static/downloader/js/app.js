/* Reelhouse front end: vanilla JS only. */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const csrf = () => ($('meta[name="csrf-token"]') || {}).content || "";
  const authed = document.body.dataset.authenticated === "true";
  const sync = "BroadcastChannel" in window ? new BroadcastChannel("rh-sync") : null;

  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0") : m + ":" + String(s).padStart(2, "0");
  }

  async function api(url, { method = "GET", body, keepalive = false } = {}) {
    const opts = { method, credentials: "same-origin", keepalive, headers: { Accept: "application/json" } };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-CSRFToken"] = csrf();
    }
    if (body !== undefined) opts.body = JSON.stringify(body);
    try {
      const res = await fetch(url, opts);
      let data = {};
      try { data = await res.json(); } catch (_) { /* non-JSON error page */ }
      return { ok: res.ok, status: res.status, data };
    } catch (_) {
      return { ok: false, status: 0, data: { error: "Can't reach the server. Check your connection." } };
    }
  }

  /* ---------------------------------------------------------------- toasts */
  function toast(message, kind = "success") {
    const box = $("#toast-container");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast toast-" + (kind === "error" ? "error" : "success");
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    const inner = document.createElement("div");
    inner.className = "d-flex";
    const body = document.createElement("div");
    body.className = "toast-body";
    body.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn-close me-2 m-auto";
    close.setAttribute("data-bs-dismiss", "toast");
    close.setAttribute("aria-label", "Close");
    inner.append(body, close);
    el.append(inner);
    box.append(el);
    const t = new bootstrap.Toast(el, { delay: kind === "error" ? 8000 : 4000 });
    el.addEventListener("hidden.bs.toast", () => el.remove());
    t.show();
  }
  $$("[data-toast]").forEach((n) => toast(n.textContent.trim(), /error|danger/.test(n.dataset.toast) ? "error" : "success"));

  /* --------------------------------------------------------- confirm modal */
  function confirmModal({ title, text, okLabel = "Confirm" }) {
    return new Promise((resolve) => {
      const el = $("#confirm-modal");
      const modal = bootstrap.Modal.getOrCreateInstance(el);
      $("#confirm-title").textContent = title;
      $("#confirm-text").textContent = text;
      const ok = $("#confirm-ok");
      ok.textContent = okLabel;
      let answered = false;
      const done = (v) => { if (answered) return; answered = true; ok.removeEventListener("click", onOk); el.removeEventListener("hidden.bs.modal", onHide); resolve(v); };
      const onOk = () => { modal.hide(); done(true); };
      const onHide = () => done(false);
      ok.addEventListener("click", onOk);
      el.addEventListener("hidden.bs.modal", onHide);
      modal.show();
    });
  }

  /* ---------------------------------------------------------- theme/sidebar */
  function setTheme(t) {
    document.documentElement.setAttribute("data-bs-theme", t);
    try { localStorage.setItem("rh-theme", t); } catch (_) {}
    syncThemeIcon();
  }
  function syncThemeIcon() {
    const dark = document.documentElement.getAttribute("data-bs-theme") === "dark";
    $$("#theme-toggle i").forEach((i) => (i.className = "bi " + (dark ? "bi-sun" : "bi-moon-stars")));
  }
  syncThemeIcon();
  $$("#theme-toggle").forEach((b) => b.addEventListener("click", () =>
    setTheme(document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark")));
  $$("[data-theme-set]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeSet)));
  const sidebar = $("#sidebar"), backdrop = $("#sidebar-backdrop");
  function toggleSidebar(open) { if (sidebar) { sidebar.classList.toggle("open", open); backdrop.hidden = !open; } }
  const sbToggle = $("#sidebar-toggle");
  if (sbToggle) sbToggle.addEventListener("click", () => toggleSidebar(!sidebar.classList.contains("open")));
  if (backdrop) backdrop.addEventListener("click", () => toggleSidebar(false));

  /* ------------------------------------------------------ progress widget */
  const STAGES = ["pending", "fetching", "downloading", "processing", "completed"];
  function renderProgress(root, v) {
    const panel = root.matches("[data-role=progress]") ? root : $("[data-role=progress]", root);
    if (!panel) return;
    const pct = Math.round(v.percent || 0);
    const active = v.is_active || v.is_completed;
    $("[data-role=stage]", panel).textContent = v.status === "failed" ? "Failed" : v.status_label + (v.is_active ? "…" : "");
    $("[data-role=percent]", panel).textContent = pct + "%";
    $("[data-role=bar]", panel).style.width = pct + "%";
    $("[data-role=track]", panel).setAttribute("aria-valuenow", String(pct));
    $("[data-role=bytes]", panel).textContent = v.total_bytes ? v.downloaded_display + " / " + v.total_display : "";
    $("[data-role=speed]", panel).textContent = v.is_active ? v.speed_display : "—";
    $("[data-role=eta]", panel).textContent = v.is_active ? v.eta_display : "—";
    const notice = $("[data-role=notice]", panel);
    notice.hidden = !v.notice;
    notice.textContent = v.notice || "";
    panel.classList.toggle("is-failed", v.status === "failed");
    const idx = STAGES.indexOf(v.status);
    $$(".stages li", panel).forEach((li) => {
      const i = STAGES.indexOf(li.dataset.stage);
      // Boolean() matters: classList.toggle(name, undefined) flips the class instead of forcing it.
      li.classList.toggle("is-done", Boolean((active && idx > -1 && i < idx) || (v.is_completed && i <= idx)));
      li.classList.toggle("is-current", Boolean(i === idx && v.status !== "completed"));
    });
  }

  /* ------------------------------------------- remembered files on this device */
  // Browsers never reveal real file paths, and a server session cannot hold a reference to a
  // file on your disk. Instead we keep the browser's own FileSystemFileHandle (File System
  // Access API; Chromium browsers) in this browser's IndexedDB, keyed by the media key.
  const fileStore = (function () {
    const DB = "rh-files", ST = "handles";
    const supported = !!(window.indexedDB && window.showOpenFilePicker);
    const open = () => new Promise((resolve, reject) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore(ST);
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
    const run = (mode, fn) => open().then((db) => new Promise((resolve, reject) => {
      const t = db.transaction(ST, mode);
      const out = fn(t.objectStore(ST));
      t.oncomplete = () => { db.close(); resolve(out); };
      t.onerror = t.onabort = () => { db.close(); reject(t.error); };
    }));
    return {
      supported,
      async put(key, handle, name) { if (supported && handle) { try { await run("readwrite", (s) => s.put({ handle, name, added: Date.now() }, key)); } catch (_) {} } },
      async get(key) { if (!supported) return null; try { return (await run("readonly", (s) => s.get(key))).result || null; } catch (_) { return null; } },
      async remove(key) { if (supported) { try { await run("readwrite", (s) => s.delete(key)); } catch (_) {} } },
      async clear() { if (supported) { try { await run("readwrite", (s) => s.clear()); } catch (_) {} } },
      async list() {
        if (!supported) return [];
        try {
          const r = await run("readonly", (s) => ({ k: s.getAllKeys(), v: s.getAll() }));
          return r.k.result.map((k, i) => ({ key: k, ...r.v.result[i] })).sort((a, b) => b.added - a.added);
        } catch (_) { return []; }
      },
    };
  })();

  /** "granted" | "prompt" | "denied". Requesting needs a user gesture (a click). */
  async function readPermission(handle, request) {
    try {
      const opts = { mode: "read" };
      if ((await handle.queryPermission(opts)) === "granted") return "granted";
      if (!request) return "prompt";
      return (await handle.requestPermission(opts)) === "granted" ? "granted" : "denied";
    } catch (_) { return "denied"; }
  }
  /** {file} or {error: "missing" | "denied" | "error"}. "missing" = moved, renamed or deleted. */
  async function fileFromHandle(handle) {
    try { return { file: await handle.getFile() }; }
    catch (e) {
      if (e && e.name === "NotFoundError") return { error: "missing" };
      if (e && (e.name === "NotAllowedError" || e.name === "SecurityError")) return { error: "denied" };
      return { error: "error" };
    }
  }

  /* -------------------------------------------------------- save to device */
  const MIME = { mp4: "video/mp4", m4a: "audio/mp4", mp3: "audio/mpeg" };
  /** Lets the user pick the destination (File System Access API where available,
   *  otherwise the browser's normal download, which can also prompt for a location). */
  async function saveToDevice(v, onProgress) {
    const ext = (v.filename.split(".").pop() || "").toLowerCase();
    if (window.showSaveFilePicker) {
      let handle = null;
      try {
        handle = await window.showSaveFilePicker({
          suggestedName: v.filename,
          types: [{ description: "Media file", accept: { [MIME[ext] || "application/octet-stream"]: ["." + ext] } }],
        });
      } catch (e) {
        if (e && e.name === "AbortError") return { cancelled: true };
        handle = null; // picker blocked: fall through to the normal download
      }
      if (handle) {
        const res = await fetch(v.file_url, { credentials: "same-origin" });
        if (!res.ok || !res.body) throw new Error("The file is no longer available. Please download it again.");
        const total = parseInt(res.headers.get("Content-Length") || "0", 10);
        const writable = await handle.createWritable();
        const reader = res.body.getReader();
        let done = 0;
        try {
          for (;;) {
            const { done: finished, value } = await reader.read();
            if (finished) break;
            await writable.write(value);
            done += value.length;
            if (onProgress) onProgress(done, total);
          }
          await writable.close();
        } catch (err) {
          try { await writable.abort(); } catch (_) {}
          throw err;
        }
        return { saved: true, name: handle.name, handle };
      }
    }
    const a = document.createElement("a");
    a.href = v.file_url;
    a.download = v.filename || "";
    document.body.append(a);
    a.click();
    a.remove();
    return { saved: true, viaBrowser: true };
  }

  /* ------------------------------------------------------------ job tracker */
  // Pushes live updates with Server-Sent Events (falls back to polling) and keeps
  // running jobs visible on every page, including after navigation or reload.
  const JOBS_KEY = "rh-jobs";
  const JOB_MAX_AGE = 3600 * 1000;
  const tracker = (function () {
    const jobs = new Map(); // id -> {id, title, added, v}
    const streams = new Map();
    const listeners = new Set();

    const readStore = () => { try { return JSON.parse(localStorage.getItem(JOBS_KEY) || "[]"); } catch (_) { return []; } };
    const persist = () => {
      try { localStorage.setItem(JOBS_KEY, JSON.stringify([...jobs.values()].map((j) => ({ id: j.id, title: j.title, added: j.added })))); } catch (_) {}
    };
    const emit = (job) => listeners.forEach((fn) => fn(job));

    function stop(id) {
      const s = streams.get(id);
      if (!s) return;
      if (s.es) s.es.close();
      if (s.timer) clearInterval(s.timer);
      streams.delete(id);
    }
    function apply(id, v) {
      const job = jobs.get(id);
      if (!job) return;
      const prev = job.v && job.v.status;
      job.v = v;
      job.title = v.title;
      emit(job);
      if (prev && prev !== v.status) {
        if (v.status === "completed") toast("Ready to save: " + v.title);
        if (v.status === "failed") toast(v.error_message || "A download failed.", "error");
      }
      if (!v.is_active) stop(id);
    }
    function gone(id) {
      const job = jobs.get(id);
      if (job && !(job.v && job.v.is_completed && job.saved)) toast("A download expired before it was saved.", "error");
      remove(id);
    }
    function poll(id) {
      const s = streams.get(id) || {};
      streams.set(id, s);
      s.timer = setInterval(async () => {
        const { ok, status, data } = await api("/api/quick/" + encodeURIComponent(id) + "/status/");
        if (status === 404) return gone(id);
        if (ok) apply(id, data.video);
      }, 1200);
    }
    function subscribe(id) {
      if (streams.has(id)) return;
      if (!window.EventSource) return poll(id);
      const es = new EventSource("/api/quick/" + encodeURIComponent(id) + "/events/");
      streams.set(id, { es });
      es.onmessage = (e) => { try { apply(id, JSON.parse(e.data)); } catch (_) {} };
      es.addEventListener("gone", () => { stop(id); gone(id); });
      es.onerror = () => {
        // EventSource retries by itself; if the browser gave up, fall back to polling.
        if (es.readyState === EventSource.CLOSED) { streams.delete(id); poll(id); }
      };
    }
    function add(v) {
      if (jobs.has(v.id)) return;
      jobs.set(v.id, { id: v.id, title: v.title, added: Date.now(), v });
      persist();
      subscribe(v.id);
      emit(jobs.get(v.id));
    }
    function remove(id) {
      stop(id);
      if (jobs.delete(id)) { persist(); emit({ id, removed: true }); }
    }
    function restore() {
      const now = Date.now();
      readStore().filter((j) => now - j.added < JOB_MAX_AGE).forEach((j) => {
        if (!jobs.has(j.id)) { jobs.set(j.id, { ...j, v: null }); subscribe(j.id); }
      });
      persist();
    }
    // Other tabs: keep the list in sync (a download started or saved elsewhere).
    window.addEventListener("storage", (e) => {
      if (e.key !== JOBS_KEY) return;
      const ids = new Set(readStore().map((j) => j.id));
      [...jobs.keys()].filter((id) => !ids.has(id)).forEach((id) => { stop(id); jobs.delete(id); emit({ id, removed: true }); });
      restore();
    });
    return { add, remove, on: (fn) => listeners.add(fn), all: () => [...jobs.values()], restore, get: (id) => jobs.get(id) };
  })();

  /* Save button logic shared by the tray and the download page. */
  async function handleSave(job, button, label) {
    const original = label ? label.textContent : button.textContent;
    const setLabel = (t) => { (label || button).textContent = t; };
    button.disabled = true;
    try {
      const r = await saveToDevice(job.v, (done, total) => setLabel(total ? "Saving " + Math.round((done / total) * 100) + "%" : "Saving…"));
      if (r.cancelled) { button.disabled = false; setLabel(original); return; }
      job.saved = true;
      if (r.handle && job.v.video_id) await fileStore.put(job.v.video_id, r.handle, r.name); // so Continue watching can reopen it
      toast(r.viaBrowser ? "Your browser is saving the file." : "Saved as " + r.name);
      setTimeout(() => tracker.remove(job.id), r.viaBrowser ? 2500 : 600);
    } catch (e) {
      toast((e && e.message) || "Couldn't save the file.", "error");
      button.disabled = false;
      setLabel(original);
    }
  }

  /* ----------------------------------------------------------- downloads tray */
  (function initTray() {
    const list = $("#tray-list"), empty = $("#tray-empty"), count = $("#tray-count");
    if (!list) return;
    function row(job) {
      const v = job.v;
      const el = document.createElement("div");
      el.className = "tray-row";
      el.dataset.id = job.id;
      const title = document.createElement("div");
      title.className = "tray-title";
      title.textContent = job.title || "Download";
      const meta = document.createElement("div");
      meta.className = "tray-meta";
      const chip = document.createElement("span");
      chip.className = "status status-" + (v ? v.status : "pending");
      chip.textContent = v ? v.status_label : "Connecting";
      meta.append(chip);
      if (v && v.is_active) {
        const pct = document.createElement("span");
        pct.textContent = Math.round(v.percent) + "%" + (v.speed_display && v.status === "downloading" ? " · " + v.speed_display : "");
        meta.append(pct);
      }
      el.append(title, meta);
      if (v && v.is_active) {
        const bar = document.createElement("div");
        bar.className = "mini-bar";
        const fill = document.createElement("span");
        fill.style.width = Math.round(v.percent) + "%";
        bar.append(fill);
        el.append(bar);
      }
      const actions = document.createElement("div");
      actions.className = "tray-actions";
      if (v && v.is_completed) {
        const save = document.createElement("button");
        save.type = "button";
        save.className = "btn btn-primary btn-sm";
        save.textContent = "Save to my device";
        save.addEventListener("click", () => handleSave(job, save));
        actions.append(save);
      }
      if (v && v.status === "failed") {
        const err = document.createElement("div");
        err.className = "card-error";
        err.textContent = v.error_message;
        el.append(err);
      }
      if (!v || !v.is_active) {
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "btn btn-ghost btn-sm";
        dismiss.textContent = "Dismiss";
        dismiss.addEventListener("click", () => tracker.remove(job.id));
        actions.append(dismiss);
      }
      if (actions.children.length) el.append(actions);
      return el;
    }
    function render() {
      const jobs = tracker.all();
      list.textContent = "";
      jobs.forEach((j) => list.append(row(j)));
      empty.hidden = jobs.length > 0;
      const attention = jobs.filter((j) => !j.v || j.v.is_active || j.v.is_completed || j.v.status === "failed").length;
      count.hidden = !attention;
      count.textContent = attention;
      const tray = $("#tray");
      tray.classList.toggle("is-busy", jobs.some((j) => j.v && j.v.is_active));
      tray.classList.toggle("is-ready", jobs.some((j) => j.v && j.v.is_completed));
    }
    let queued = false;
    tracker.on(() => { if (queued) return; queued = true; requestAnimationFrame(() => { queued = false; render(); }); });
    tracker.restore();
    render();
  })();

  /* ------------------------------------------------------- download page */
  const dlApp = $("#download-app");
  if (dlApp) initDownloadPage(dlApp);

  function initDownloadPage(app) {
    const form = $("#fetch-form"), input = $("#url-input"), fetchBtn = $("#fetch-btn");
    const errBox = $("#fetch-error"), skeleton = $("#skeleton"), preview = $("#preview");
    const job = $("#job"), startBtn = $("#start-btn");
    const saveBtn = $("#job-save"), saveLabel = $("span", saveBtn);
    let current = null, jobId = null;

    const showError = (msg) => { errBox.textContent = msg; errBox.hidden = !msg; };

    async function fetchMetadata(url) {
      showError("");
      preview.hidden = true;
      job.hidden = true;
      skeleton.hidden = false;
      fetchBtn.disabled = true;
      fetchBtn.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Fetching…';
      const { ok, data } = await api(app.dataset.metadataUrl, { method: "POST", body: { url } });
      skeleton.hidden = true;
      fetchBtn.disabled = false;
      fetchBtn.innerHTML = '<i class="bi bi-search"></i> Fetch video';
      if (!ok) { showError(data.error || "Couldn't fetch that video."); return; }
      current = data;
      renderPreview(data);
    }

    function renderPreview(data) {
      const v = data.video;
      const img = $("#pv-thumb");
      img.src = v.thumbnail_url || "";
      img.style.visibility = v.thumbnail_url ? "visible" : "hidden";
      $("#pv-title").textContent = v.title;
      $("#pv-uploader").textContent = v.uploader;
      $("#pv-duration").textContent = v.duration_display;
      $("#pv-date").textContent = v.upload_date ? "Uploaded " + v.upload_date : "";
      const list = $("#format-list");
      list.textContent = "";
      const usable = data.formats.filter((f) => !f.disabled);
      const wanted = (usable.find((f) => f.key === (data.default_preset || app.dataset.defaultPreset)) || usable[0] || {}).key;
      data.formats.forEach((f) => {
        const wrap = document.createElement("div");
        wrap.className = "format-opt";
        const radio = document.createElement("input");
        radio.type = "radio"; radio.name = "preset"; radio.value = f.key; radio.id = "fmt-" + f.key;
        radio.checked = f.key === wanted;
        radio.disabled = !!f.disabled;
        const label = document.createElement("label");
        label.htmlFor = radio.id;
        const name = document.createElement("span");
        name.textContent = f.label;
        const size = document.createElement("small");
        size.textContent = f.disabled ? "Over the size limit" : f.size_display ? "About " + f.size_display : "Size unknown";
        label.append(name, size);
        wrap.append(radio, label);
        list.append(wrap);
      });
      startBtn.disabled = !usable.length;
      preview.hidden = false;
    }

    async function startDownload() {
      const chosen = $('input[name="preset"]:checked');
      if (!chosen || !current) return;
      startBtn.disabled = true;
      const { ok, data } = await api(app.dataset.downloadUrl, { method: "POST", body: { url: current.video.url, preset: chosen.value } });
      startBtn.disabled = false;
      if (!ok) { toast(data.error || "Couldn't start the download.", "error"); return; }
      preview.hidden = true;
      showJob(data.video);
      tracker.add(data.video);
    }

    function showJob(video) {
      jobId = video.id;
      job.hidden = false;
      $("#job-title").textContent = current ? current.video.title : video.title;
      $("#job-error").hidden = true;
      $("#job-actions").hidden = true;
      saveBtn.hidden = false;
      saveBtn.disabled = false;
      saveLabel.textContent = "Save to my device";
      $("#job-hint").hidden = true;
      const holder = $("#job-progress");
      holder.textContent = "";
      holder.append($("#progress-template").content.cloneNode(true));
      renderProgress(holder, video);
    }

    // Live updates for this page's job come from the shared tracker.
    tracker.on((j) => {
      if (!j || j.id !== jobId) return;
      if (j.removed) { return; }
      const v = j.v;
      if (!v) return;
      $("#job-title").textContent = v.title;
      renderProgress($("#job-progress"), v);
      const err = $("#job-error"), actions = $("#job-actions"), hint = $("#job-hint");
      if (v.is_completed) {
        actions.hidden = false;
        saveBtn.hidden = false;
        hint.hidden = false;
        hint.textContent = (v.file_size_display ? v.file_size_display + " · " : "") + ("showSaveFilePicker" in window
          ? "You'll choose where to save it. Reelhouse remembers the location in this browser so you can resume without choosing the file again. The server copy is deleted afterwards."
          : "Your browser will save it to its download location (turn on “Ask where to save files” to choose a folder).");
      } else if (v.status === "failed") {
        err.textContent = v.error_message || "The download failed.";
        err.hidden = false;
        actions.hidden = false;
        saveBtn.hidden = true;
      }
    });
    saveBtn.addEventListener("click", async () => {
      const j = tracker.get(jobId);
      if (j && j.v && j.v.is_completed) {
        await handleSave(j, saveBtn, saveLabel);
        if (j.saved) { saveLabel.textContent = "Saved"; $("#job-hint").textContent = "Saved. The temporary server copy has been removed."; }
      }
    });
    $("#job-again").addEventListener("click", () => {
      job.hidden = true; jobId = null;
      input.value = ""; input.focus(); window.scrollTo({ top: 0, behavior: "smooth" });
    });
    form.addEventListener("submit", (e) => { e.preventDefault(); const u = input.value.trim(); if (u) fetchMetadata(u); else showError("Paste a YouTube video link to continue."); });
    startBtn.addEventListener("click", startDownload);
    if (app.dataset.initialUrl) fetchMetadata(app.dataset.initialUrl);
  }

  /* ------------------------------------------------- playback position stores */
  // Accounts: positions live on the server. Anonymous: only in this browser's localStorage.
  const POS_PREFIX = "rh-pos:";
  const localStore = {
    async get(key) { try { return JSON.parse(localStorage.getItem(POS_PREFIX + key)); } catch (_) { return null; } },
    async set(key, d) { try { localStorage.setItem(POS_PREFIX + key, JSON.stringify({ ...d, updated: Date.now() })); } catch (_) {} },
    async remove(key) { localStorage.removeItem(POS_PREFIX + key); },
    async list() {
      const out = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (!k || !k.startsWith(POS_PREFIX)) continue;
        try {
          const d = JSON.parse(localStorage.getItem(k));
          const key = k.slice(POS_PREFIX.length);
          out.push({ key, title: d.title || key, position: d.position || 0, duration: d.duration || 0, updated: d.updated || 0,
            thumbnail_url: /^[A-Za-z0-9_-]{11}$/.test(key) ? "https://i.ytimg.com/vi/" + key + "/hqdefault.jpg" : "" });
        } catch (_) {}
      }
      return out;
    },
    async clear() { (await this.list()).forEach((i) => localStorage.removeItem(POS_PREFIX + i.key)); },
  };
  const serverStore = {
    async get(key) {
      const { ok, data } = await api("/api/playback/" + encodeURIComponent(key) + "/");
      return ok && data.item ? data.item : null;
    },
    async set(key, d, keepalive) { await api("/api/playback/" + encodeURIComponent(key) + "/", { method: "POST", body: d, keepalive }); },
    async remove(key) { await api("/api/playback/" + encodeURIComponent(key) + "/", { method: "DELETE" }); },
    async list() {
      const { ok, data } = await api("/api/playback/");
      return ok ? data.results.map((i) => ({ ...i, updated: Date.parse(i.updated_at) })) : [];
    },
    async clear() { await api("/api/playback/", { method: "DELETE" }); },
  };
  const store = authed ? serverStore : localStore;
  const notifyPosition = (key) => { if (sync) sync.postMessage({ type: "position", key }); };

  /* --------------------------------------------------------- player page */
  function hashKey(str) { // cyrb53: tiny, stable, dependency-free
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < str.length; i++) {
      const ch = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ ch, 2654435761); h2 = Math.imul(h2 ^ ch, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16);
  }
  const ID_IN_NAME = /\s*\[([A-Za-z0-9_-]{11})\]\.[A-Za-z0-9]+$/;
  const keyFor = (file) => { const m = file.name.match(ID_IN_NAME); return m ? m[1] : "local-" + hashKey(file.name + "|" + file.size); };
  const titleFor = (file) => file.name.replace(ID_IN_NAME, "").replace(/\.[^.]+$/, "");
  const MEDIA_RE = /\.(mp4|m4a|mp3|webm|mkv|mov|ogg|wav)$/i;
  const PICKER_TYPES = [{ description: "Video or audio", accept: { "video/*": [".mp4", ".webm", ".mkv", ".mov"], "audio/*": [".mp3", ".m4a", ".ogg", ".wav"] } }];

  /** Remove everything remembered about one video: its position and the file location. */
  async function forgetMedia(key) {
    await store.remove(key);
    await fileStore.remove(key);
    notifyPosition(key);
  }
  const dismissible = (btnSel, boxSel) => { const b = $(btnSel); if (b) b.addEventListener("click", () => ($(boxSel).hidden = true)); };

  function initWatch(app) {
    const input = $("#file-input"), dz = $("#dropzone"), section = $("#player-section"), media = $("#player");
    const alertBox = $("#file-alert"), alertText = $("#file-alert-text"), alertActions = $("#file-alert-actions");
    let key = null, title = "", objectUrl = null, saved = null, lastSent = -1, lastTick = 0;
    let lookupDone = false; // never write a position before the saved one has been read (it would be clobbered)

    function showAlert(text, actions = []) {
      alertText.textContent = text;
      alertActions.textContent = "";
      actions.forEach(({ label, primary, onClick }) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn btn-sm " + (primary ? "btn-primary" : "btn-ghost");
        b.textContent = label;
        b.addEventListener("click", onClick);
        alertActions.append(b);
      });
      alertBox.hidden = false;
    }
    const clearAlert = () => { alertBox.hidden = true; alertActions.textContent = ""; };

    function openFile(file, opts = {}) {
      if (!file) return false;
      if (!/^(video|audio)\//.test(file.type) && !MEDIA_RE.test(file.name)) {
        toast("That doesn't look like a video or audio file.", "error"); return false;
      }
      if (key) flush(true);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      key = opts.key || keyFor(file);
      title = titleFor(file);
      objectUrl = URL.createObjectURL(file);
      if (opts.handle && !opts.fromStore) fileStore.put(key, opts.handle, file.name).then(renderSaved); // remember where it is
      lastSent = -1;
      saved = null;
      lookupDone = false;
      clearAlert();
      $("#resume-notice").hidden = true;
      $("#resume-hint").hidden = true;
      $("#media-title").textContent = title;
      media.poster = /^[A-Za-z0-9_-]{11}$/.test(key) ? "https://i.ytimg.com/vi/" + key + "/hqdefault.jpg" : "";
      media.src = objectUrl;
      section.hidden = false;
      dz.hidden = true;
      const openedKey = key;
      store.get(openedKey).then((s) => { if (key === openedKey) { saved = s; applyResume(); } })
        .finally(() => { if (key === openedKey) { lookupDone = true; flush(true); } }); // list it right away, even if closed quickly
      document.title = title + " · Reelhouse";
      return true;
    }

    function applyResume() {
      if (!saved || !isFinite(media.duration) || !media.duration) return;
      const pos = saved.position || 0;
      if (pos > 5 && pos < media.duration - 5) {
        media.currentTime = pos;
        $("#resume-time").textContent = fmtTime(pos);
        $("#resume-notice").hidden = false;
      }
      saved = null;
    }
    media.addEventListener("loadedmetadata", () => { applyResume(); flush(true); }); // listed as soon as its length is known
    media.addEventListener("canplay", applyResume, { once: true });

    function flush(force, keepalive) {
      if (!key || !media.src || !lookupDone) return;
      const pos = Math.floor(media.ended ? media.duration || 0 : media.currentTime || 0);
      if (!force && Math.abs(pos - lastSent) < 3) return;
      if (!isFinite(media.duration) || !media.duration) return;
      lastSent = pos;
      store.set(key, { position: pos, duration: Math.floor(media.duration), title }, keepalive).then(() => notifyPosition(key));
    }
    media.addEventListener("timeupdate", () => { const n = Date.now(); if (n - lastTick >= 8000) { lastTick = n; flush(false); } });
    media.addEventListener("play", () => flush(true));
    media.addEventListener("pause", () => flush(true));
    media.addEventListener("ended", () => flush(true));
    window.addEventListener("pagehide", () => flush(true, true));
    document.addEventListener("visibilitychange", () => { if (document.hidden) flush(true, true); });

    $("#speed-select").addEventListener("change", (e) => (media.playbackRate = parseFloat(e.target.value)));
    const pip = $("#pip-btn");
    if (pip && document.pictureInPictureEnabled) {
      pip.hidden = false;
      pip.addEventListener("click", async () => {
        try { document.pictureInPictureElement ? await document.exitPictureInPicture() : await media.requestPictureInPicture(); }
        catch (_) { toast("Picture-in-picture isn't available for this file.", "error"); }
      });
    }
    $("#fs-btn").addEventListener("click", () => { if (media.requestFullscreen) media.requestFullscreen(); });
    $("#restart-btn").addEventListener("click", () => { media.currentTime = 0; media.play().catch(() => {}); $("#resume-notice").hidden = true; });
    dismissible("#resume-close", "#resume-notice");
    dismissible("#resume-hint-close", "#resume-hint");

    /* ---- choosing a file (only needed the first time, or on a different device) ---- */
    async function pickFile() {
      if (window.showOpenFilePicker) {
        try {
          const [handle] = await window.showOpenFilePicker({ multiple: false, types: PICKER_TYPES });
          openFile(await handle.getFile(), { handle });
        } catch (e) { if (!e || e.name !== "AbortError") toast("Couldn't open that file.", "error"); }
      } else input.click();
    }
    $("#change-file").addEventListener("click", pickFile);
    dz.addEventListener("click", pickFile);
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickFile(); } });
    input.addEventListener("change", () => openFile(input.files[0]));
    ["dragenter", "dragover"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("is-over"); }));
    ["dragleave", "drop"].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("is-over"); }));
    document.addEventListener("drop", async (e) => {
      const dt = e.dataTransfer;
      if (!dt) return;
      const item = dt.items && dt.items[0];
      const pendingHandle = item && item.getAsFileSystemHandle ? item.getAsFileSystemHandle() : null; // must start inside the event
      let handle = null, file = null;
      try { const h = pendingHandle && (await pendingHandle); if (h && h.kind === "file") { handle = h; file = await h.getFile(); } } catch (_) {}
      if (!file && dt.files.length) file = dt.files[0];
      if (file) openFile(file, { handle });
    });

    /* ---- reopening a remembered file: no picker ---- */
    async function openFromRecord(k, rec, request) {
      const perm = await readPermission(rec.handle, request);
      if (perm !== "granted") return perm;
      const r = await fileFromHandle(rec.handle);
      if (r.file) return openFile(r.file, { handle: rec.handle, key: k, fromStore: true }) ? "ok" : "error";
      return r.error;
    }
    function reportMissing(k, rec) {
      toast("“" + rec.name + "” no longer exists on this device.", "error");
      showAlert("“" + rec.name + "” can't be found. It may have been moved, renamed or deleted from this device.", [
        { label: "Locate file…", primary: true, onClick: pickFile },
        { label: "Remove from list", onClick: async () => { await forgetMedia(k); clearAlert(); renderSaved(); toast("Removed from the list."); } },
      ]);
    }
    function handleOutcome(k, rec, res) {
      if (res === "ok") return;
      if (res === "missing") return reportMissing(k, rec);
      showAlert("Reelhouse wasn't allowed to read “" + rec.name + "”. Choose the file instead.", [{ label: "Choose file…", primary: true, onClick: pickFile }]);
    }

    const savedPanel = $("#saved-files"), savedList = $("#saved-list");
    async function renderSaved() {
      const items = await fileStore.list();
      savedPanel.hidden = !items.length;
      savedList.textContent = "";
      items.forEach((rec) => {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "saved-name";
        name.textContent = rec.name;
        const play = document.createElement("button");
        play.type = "button"; play.className = "btn btn-primary btn-sm"; play.innerHTML = '<i class="bi bi-play-fill"></i> Play';
        play.addEventListener("click", async () => handleOutcome(rec.key, rec, await openFromRecord(rec.key, rec, true)));
        const forget = document.createElement("button");
        forget.type = "button"; forget.className = "btn btn-ghost btn-sm"; forget.textContent = "Forget location";
        forget.setAttribute("aria-label", "Forget the location of " + rec.name);
        forget.addEventListener("click", async () => { await fileStore.remove(rec.key); toast("Forgot where “" + rec.name + "” is stored."); renderSaved(); });
        li.append(name, play, forget);
        savedList.append(li);
      });
    }
    renderSaved();

    // Arrived from "Continue watching" (?v=key): open the remembered file straight away.
    const wanted = new URLSearchParams(location.search).get("v");
    if (wanted && /^[A-Za-z0-9_-]{1,32}$/.test(wanted)) (async () => {
      const [rec, item] = await Promise.all([fileStore.get(wanted), store.get(wanted)]);
      const when = item ? " to continue from " + fmtTime(item.position) : "";
      if (!rec) { // other device/browser, or no File System Access: this is the only time a picker is needed
        $("#resume-hint-text").textContent = "Choose the file for “" + ((item && item.title) || wanted) + "”" + when + "." +
          (/^[A-Za-z0-9_-]{11}$/.test(wanted) ? " Look for a name ending in [" + wanted + "]." : "");
        $("#resume-hint").hidden = false;
        return;
      }
      const res = await openFromRecord(wanted, rec, false);
      if (res === "prompt") { // the browser only needs one click to re-allow access; still no file picker
        showAlert("Allow Reelhouse to reopen “" + rec.name + "”" + when + ".", [{
          label: "Continue watching", primary: true,
          onClick: async () => handleOutcome(wanted, rec, await openFromRecord(wanted, rec, true)),
        }]);
      } else handleOutcome(wanted, rec, res);
    })();
  }

  /* ---------------------------------------------------- continue watching */
  function initContinue() {
    const grid = $("#cw-grid"), recentGrid = $("#recent-grid"), empty = $("#cw-empty");
    const sectionProgress = $("#cw-progress"), sectionRecent = $("#cw-recent");
    const isFinished = (i) => !!(i.duration && i.position >= i.duration * 0.95);
    let renderToken = 0;
    async function render() {
      const mine = ++renderToken;
      const all = (await store.list()).sort((a, b) => (b.updated || 0) - (a.updated || 0));
      const inProgress = all.filter((i) => i.position > 5 && !isFinished(i));
      const recent = all.filter((i) => !inProgress.includes(i)).slice(0, 24); // finished or just started
      const fragA = document.createDocumentFragment(), fragB = document.createDocumentFragment();
      for (const i of inProgress) fragA.append(await card(i));
      for (const i of recent) fragB.append(await card(i));
      if (mine !== renderToken) return; // a newer refresh superseded this one; never draw a stale list
      grid.replaceChildren(fragA);
      recentGrid.replaceChildren(fragB);
      sectionProgress.hidden = !inProgress.length;
      sectionRecent.hidden = !recent.length;
      empty.hidden = all.length > 0;
    }
    function markMissing(el, name) {
      let msg = $(".card-error", el);
      if (!msg) { msg = document.createElement("p"); msg.className = "card-error"; msg.setAttribute("role", "alert"); $(".vcard-meta", el).after(msg); }
      msg.textContent = "“" + name + "” no longer exists on this device.";
      const btn = $("a.btn", el);
      btn.innerHTML = '<i class="bi bi-folder2-open"></i> Locate file';
      btn.dataset.missing = "1";
    }
    async function card(i) {
      const rec = await fileStore.get(i.key); // remembered location (if any), no picker involved
      const el = document.createElement("article");
      el.className = "vcard";
      const href = "/watch/?v=" + encodeURIComponent(i.key);
      const thumb = document.createElement("a");
      thumb.className = "vcard-thumb";
      thumb.setAttribute("href", href);
      if (i.thumbnail_url) {
        const img = document.createElement("img");
        img.src = i.thumbnail_url; img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer";
        thumb.append(img);
      } else {
        const ph = document.createElement("span");
        ph.className = "thumb-empty";
        ph.innerHTML = '<i class="bi bi-film"></i>';
        thumb.append(ph);
      }
      const badge = document.createElement("span");
      badge.className = "play-badge";
      badge.innerHTML = '<i class="bi bi-play-fill"></i>';
      const track = document.createElement("span");
      track.className = "resume-track";
      const fill = document.createElement("span");
      fill.style.width = (i.duration ? Math.min(100, (i.position / i.duration) * 100) : 0) + "%";
      if (isFinished(i)) track.hidden = true;
      track.append(fill);
      thumb.append(badge, track);

      const body = document.createElement("div");
      body.className = "vcard-body";
      const h = document.createElement("h3");
      h.className = "vcard-title";
      h.textContent = i.title;
      const meta = document.createElement("div");
      meta.className = "vcard-meta";
      const s = document.createElement("span");
      const finished = isFinished(i);
      if (finished) { s.className = "watched-badge"; s.innerHTML = '<i class="bi bi-check-circle-fill"></i> Watched'; }
      else if (i.position <= 5) s.textContent = "Just started";
      else s.textContent = "Stopped at " + fmtTime(i.position) + (i.duration ? " of " + fmtTime(i.duration) : "");
      meta.append(s);
      const actions = document.createElement("div");
      actions.className = "vcard-actions";
      const resume = document.createElement("a");
      resume.className = "btn btn-primary btn-sm";
      resume.setAttribute("href", href);
      const verb = isFinished(i) ? "Play again" : "Resume";
      resume.innerHTML = rec ? '<i class="bi bi-play-fill"></i> ' + verb : '<i class="bi bi-folder2-open"></i> Locate file & ' + verb.toLowerCase();
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-ghost btn-sm ms-auto";
      del.setAttribute("aria-label", "Remove " + i.title + " from the list");
      del.innerHTML = '<i class="bi bi-x-lg"></i>';
      del.addEventListener("click", async () => { await forgetMedia(i.key); toast("Removed from the list."); render(); });
      actions.append(resume, del);
      body.append(h, meta, actions);
      el.append(thumb, body);

      if (rec) {
        // Resume: re-grant read access inside this click (the browser requires a gesture), verify
        // the file still exists, then go to the player, which opens it with no file picker.
        const onResume = async (e) => {
          if (e.currentTarget.dataset.missing) return; // "Locate file": normal navigation to the picker
          e.preventDefault();
          const perm = await readPermission(rec.handle, true);
          if (perm !== "granted") { toast("Access to “" + rec.name + "” wasn't allowed.", "error"); return; }
          const r = await fileFromHandle(rec.handle);
          if (r.error === "missing") { markMissing(el, rec.name); toast("“" + rec.name + "” no longer exists on this device.", "error"); return; }
          location.href = href;
        };
        thumb.addEventListener("click", onResume);
        resume.addEventListener("click", onResume);
        // Quietly flag files that have disappeared (only when access is already granted).
        readPermission(rec.handle, false).then(async (p) => { if (p === "granted" && (await fileFromHandle(rec.handle)).error === "missing") markMissing(el, rec.name); });
      }
      return el;
    }
    render();
    if (sync) sync.addEventListener("message", (e) => { if (e.data && e.data.type === "position") render(); });
    window.addEventListener("storage", (e) => { if (e.key && e.key.startsWith(POS_PREFIX)) render(); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) render(); });
    if (authed) setInterval(() => { if (!document.hidden) render(); }, 15000);
  }

  /* ------------------------------------------------------------- settings */
  const clearBtn = $("#clear-history");
  if (clearBtn) clearBtn.addEventListener("click", async () => {
    const yes = await confirmModal({ title: "Clear watch history?", text: "Saved positions and the remembered file locations in this browser will be removed. Your files are not affected.", okLabel: "Clear history" });
    if (!yes) return;
    await serverStore.clear();
    await fileStore.clear();
    toast("Watch history cleared.");
    notifyPosition("*");
    setTimeout(() => location.reload(), 600);
  });

  /* ------------------------------------------- start the page-specific code */
  // Kept at the very end so every const above is initialised before it runs.
  const watchApp = $("#watch-app");
  if (watchApp) initWatch(watchApp);
  const cwApp = $("#cw-app");
  if (cwApp) initContinue(cwApp);
})();
