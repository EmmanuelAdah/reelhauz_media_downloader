// Keep object references (mock file handles) intact when stored in the fake IndexedDB.
globalThis.structuredClone = (x) => x;
const { JSDOM } = require("jsdom");
const { IDBFactory } = require("fake-indexeddb");
const fs = require("fs");
const js = fs.readFileSync(require("path").join(__dirname, "..", "downloader/static/downloader/js/app.js"), "utf8");
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const json = (o, status = 200) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(o) });
let failed = 0;
const check = (name, cond) => { console.log((cond ? "  ok   " : "  FAIL ") + name); if (!cond) failed++; };

class MockHandle {
  constructor(name, { perm = "granted", missing = false, grant = true } = {}) { this.kind = "file"; this.name = name; this.perm = perm; this.missing = missing; this.grant = grant; this.calls = { request: 0, getFile: 0 }; }
  async queryPermission() { return this.perm; }
  async requestPermission() { this.calls.request++; if (this.grant) this.perm = "granted"; return this.grant ? "granted" : "denied"; }
  async getFile() { this.calls.getFile++; if (this.perm !== "granted") { const e = new Error("no"); e.name = "NotAllowedError"; throw e; }
    if (this.missing) { const e = new Error("gone"); e.name = "NotFoundError"; throw e; }
    return new this.win.File(["x"], this.name, { type: "video/mp4" }); }
}

function boot(page, { url = "http://localhost/", idb, pickerSpy, fetchImpl, savePicker } = {}) {
  const html = fs.readFileSync(require("path").join(__dirname, ".pages", `${page}.html`), "utf8").replace(/<script src=[^>]*><\/script>/g, "").replace(/<link [^>]*>/g, "");
  const dom = new JSDOM(html, { runScripts: "outside-only", url, pretendToBeVisual: true });
  const w = dom.window;
  const toasts = [];
  w.bootstrap = { Toast: class { constructor(el) { toasts.push(el.textContent); } show() {} }, Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) } };
  w.indexedDB = idb; w.showOpenFilePicker = pickerSpy || (async () => { throw Object.assign(new Error("x"), { name: "AbortError" }); });
  if (savePicker) w.showSaveFilePicker = savePicker;
  w.fetch = fetchImpl || (() => json({ ok: false }, 404));
  w.requestAnimationFrame = (f) => setTimeout(f, 0);
  w.EventSource = class { constructor() { this.readyState = 1; } addEventListener() {} close() {} };
  w.EventSource.CLOSED = 2;
  w.URL.createObjectURL = () => "blob:mock"; w.URL.revokeObjectURL = () => {};
  w.eval(js);
  return { w, toasts, $: (s) => w.document.querySelector(s), $$: (s) => [...w.document.querySelectorAll(s)] };
}
const adopt = (h, w) => { h.win = w; return h; };
const seedIDB = async (idb, key, handle, name) => new Promise((res, rej) => {
  const r = idb.open("rh-files", 1); r.onupgradeneeded = () => r.result.createObjectStore("handles");
  r.onsuccess = () => { const t = r.result.transaction("handles", "readwrite"); t.objectStore("handles").put({ handle, name, added: 1 }, key); t.oncomplete = () => { r.result.close(); res(); }; };
  r.onerror = () => rej(r.error);
});
const idbHas = (idb, key) => new Promise((res) => { const r = idb.open("rh-files", 1); r.onupgradeneeded = () => r.result.createObjectStore("handles");
  r.onsuccess = () => { const q = r.result.transaction("handles").objectStore("handles").get(key); q.onsuccess = () => { r.result.close(); res(!!q.result); }; }; });
const seedPos = (w, key, extra = {}) => w.localStorage.setItem("rh-pos:" + key, JSON.stringify({ position: 754, duration: 1122, title: "Building REST APIs", updated: 5, ...extra }));
const KEY = "dQw4w9WgXcQ", NAME = `Building REST APIs [${KEY}].mp4`;

(async () => {
  console.log("CONTINUE WATCHING: remembered file, no picker");
  {
    const idb = new IDBFactory(); const picker = []; 
    const h = new MockHandle(NAME, { perm: "prompt" });
    await seedIDB(idb, KEY, h, NAME);
    const p = boot("cw", { idb, pickerSpy: async () => { picker.push(1); } });
    h.win = p.w; seedPos(p.w, KEY); p.w.dispatchEvent(new p.w.StorageEvent("storage", { key: "rh-pos:" + KEY })); await tick(80);
    const btn = p.$("#cw-grid a.btn");
    check("button says Resume (file location is remembered)", btn && btn.textContent.includes("Resume") && !btn.textContent.includes("Locate"));
    btn.click(); await tick(80);
    check("re-grants read access inside the click (one browser prompt, no file picker)", h.calls.request === 1 && picker.length === 0);
    check("verified the file exists before opening the player", h.calls.getFile >= 1);
    check("no 'missing' warning shown", !p.$("#cw-grid .card-error"));
  }
  {
    const idb = new IDBFactory();
    const h = new MockHandle(NAME, { perm: "granted", missing: true });
    await seedIDB(idb, KEY, h, NAME);
    const p = boot("cw", { idb }); h.win = p.w; seedPos(p.w, KEY); p.w.dispatchEvent(new p.w.StorageEvent("storage", { key: "rh-pos:" + KEY })); await tick(80);
    const err = p.$("#cw-grid .card-error");
    check("file deleted from disk: card shows an alert", err && err.textContent.includes("no longer exists on this device") && err.getAttribute("role") === "alert");
    check("button switches to 'Locate file'", p.$("#cw-grid a.btn").textContent.includes("Locate file"));
    p.$("#cw-grid a.btn").click(); await tick(40);
  }
  {
    const idb = new IDBFactory();
    const h = new MockHandle(NAME, { perm: "prompt", missing: true });
    await seedIDB(idb, KEY, h, NAME);
    const p = boot("cw", { idb }); h.win = p.w; seedPos(p.w, KEY); p.w.dispatchEvent(new p.w.StorageEvent("storage", { key: "rh-pos:" + KEY })); await tick(80);
    p.$("#cw-grid a.btn").click(); await tick(80);
    check("missing file discovered on click: toast + card alert", p.toasts.some((t) => /no longer exists/.test(t)) && !!p.$("#cw-grid .card-error"));
  }
  {
    const idb = new IDBFactory();
    const p = boot("cw", { idb }); seedPos(p.w, KEY); p.w.dispatchEvent(new p.w.StorageEvent("storage", { key: "rh-pos:" + KEY })); await tick(80);
    check("no remembered file (other device / non-Chromium): offers 'Locate file & resume'", p.$("#cw-grid a.btn").textContent.includes("Locate file"));
  }
  {
    const idb = new IDBFactory(); const h = new MockHandle(NAME);
    await seedIDB(idb, KEY, h, NAME);
    const p = boot("cw", { idb }); h.win = p.w; seedPos(p.w, KEY); p.w.dispatchEvent(new p.w.StorageEvent("storage", { key: "rh-pos:" + KEY })); await tick(80);
    check("(before removal) location is stored", await idbHas(idb, KEY));
    p.$("#cw-grid button[aria-label^='Remove']").click(); await tick(80);
    check("removing from the list deletes the remembered file location", !(await idbHas(idb, KEY)));
    check("...and the saved position", p.w.localStorage.getItem("rh-pos:" + KEY) === null);
    check("...and the card disappears, empty state shows", p.$$("#cw-grid .vcard").length === 0 && !p.$("#cw-empty").hidden);
  }

  console.log("PLAYER: opened from Continue watching (?v=key)");
  {
    const idb = new IDBFactory(); const picker = [];
    const h = new MockHandle(NAME, { perm: "granted" }); await seedIDB(idb, KEY, h, NAME);
    const p = boot("watch", { idb, url: `http://localhost/watch/?v=${KEY}`, pickerSpy: async () => { picker.push(1); } }); h.win = p.w; seedPos(p.w, KEY);
    await tick(100);
    const media = p.$("#player"); Object.defineProperty(media, "duration", { value: 1122, configurable: true });
    media.dispatchEvent(new p.w.Event("loadedmetadata")); await tick(30);
    check("opens automatically (permission already granted), no picker", !p.$("#player-section").hidden && picker.length === 0 && p.$("#media-title").textContent === "Building REST APIs");
    check("resumes at the saved position", Math.round(media.currentTime) === 754 && !p.$("#resume-notice").hidden);
    p.$("#resume-close").click();
    check("'Continuing from…' notice can be closed with its × button", p.$("#resume-notice").hidden);
    check("Start over button still present in the notice markup", !!p.$("#restart-btn"));
  }
  {
    const idb = new IDBFactory(); const h = new MockHandle(NAME, { perm: "prompt" }); await seedIDB(idb, KEY, h, NAME);
    const p = boot("watch", { idb, url: `http://localhost/watch/?v=${KEY}` }); h.win = p.w; seedPos(p.w, KEY); await tick(100);
    const btn = p.$("#file-alert-actions button");
    check("permission lapsed: one 'Continue watching' button, not a file picker", !p.$("#file-alert").hidden && btn && /Continue watching/.test(btn.textContent) && p.$("#player-section").hidden);
    btn.click(); await tick(100);
    check("clicking it re-grants access and opens the file", h.calls.request === 1 && !p.$("#player-section").hidden && p.$("#file-alert").hidden);
  }
  {
    const idb = new IDBFactory(); const h = new MockHandle(NAME, { perm: "granted", missing: true }); await seedIDB(idb, KEY, h, NAME);
    const p = boot("watch", { idb, url: `http://localhost/watch/?v=${KEY}` }); h.win = p.w; seedPos(p.w, KEY); await tick(100);
    check("file gone: alert + toast, player stays closed", !p.$("#file-alert").hidden && /can't be found/.test(p.$("#file-alert-text").textContent) && p.toasts.some((t) => /no longer exists/.test(t)) && p.$("#player-section").hidden);
    const labels = p.$$("#file-alert-actions button").map((b) => b.textContent);
    check("offers Locate file… and Remove from list", labels.includes("Locate file…") && labels.includes("Remove from list"));
    p.$$("#file-alert-actions button").find((b) => b.textContent === "Remove from list").click(); await tick(80);
    check("'Remove from list' forgets the path and the position", !(await idbHas(idb, KEY)) && p.w.localStorage.getItem("rh-pos:" + KEY) === null && p.$("#file-alert").hidden);
  }
  {
    const idb = new IDBFactory();
    const p = boot("watch", { idb, url: `http://localhost/watch/?v=${KEY}` }); seedPos(p.w, KEY); p.w.location.reload; await tick(100);
    // seed after boot happened -> re-run hint path via a fresh boot with the position present
    const p2 = boot("watch", { idb, url: `http://localhost/watch/?v=${KEY}` });
    p2.w.localStorage.setItem("rh-pos:" + KEY, p.w.localStorage.getItem("rh-pos:" + KEY));
    const p3 = new JSDOM(fs.readFileSync(require("path").join(__dirname, ".pages", "watch.html"), "utf8").replace(/<script src=[^>]*><\/script>/g, ""), { runScripts: "outside-only", url: `http://localhost/watch/?v=${KEY}` });
    p3.window.localStorage.setItem("rh-pos:" + KEY, JSON.stringify({ position: 754, duration: 1122, title: "Building REST APIs" }));
    p3.window.bootstrap = { Toast: class { show() {} }, Modal: { getOrCreateInstance: () => ({}) } };
    p3.window.indexedDB = idb; p3.window.showOpenFilePicker = async () => {}; p3.window.fetch = () => json({}, 404);
    p3.window.requestAnimationFrame = (f) => setTimeout(f, 0); p3.window.EventSource = class { addEventListener() {} close() {} };
    p3.window.eval(js); await tick(100);
    const hint = p3.window.document.querySelector("#resume-hint");
    check("no remembered file: only then the user is asked to choose it", !hint.hidden && /Choose the file for/.test(hint.textContent) && hint.textContent.includes("12:34"));
    p3.window.document.querySelector("#resume-hint-close").click();
    check("that hint can be closed too", hint.hidden);
  }

  console.log("PLAYER: picking a file remembers its location; saved-files list reopens it with no picker");
  {
    const idb = new IDBFactory(); const h = new MockHandle(NAME, { perm: "granted" }); let picked = 0;
    const p = boot("watch", { idb, pickerSpy: async () => { picked++; return [h]; } }); h.win = p.w; await tick(50);
    p.$("#dropzone").click(); await tick(100);
    check("first time: file picked via the picker", picked === 1 && !p.$("#player-section").hidden);
    check("its location is now remembered", await idbHas(idb, KEY));
    const p2 = boot("watch", { idb, pickerSpy: async () => { picked++; return [h]; } }); h.win = p2.w; await tick(100);
    check("later visit lists the remembered file", !p2.$("#saved-files").hidden && p2.$("#saved-list").textContent.includes(NAME));
    p2.$("#saved-list button.btn-primary").click(); await tick(100);
    check("Play opens it with no picker", picked === 1 && !p2.$("#player-section").hidden);
    p2.$$("#saved-list button").find((b) => /Forget/.test(b.textContent)).click(); await tick(60);
    check("'Forget location' removes only the stored path", !(await idbHas(idb, KEY)));
  }

  console.log("DOWNLOAD PAGE: saving through the Save dialog remembers the file");
  {
    const idb = new IDBFactory(); const saved = new MockHandle(NAME);
    let written = 0;
    saved.createWritable = async () => ({ write: async (c) => { written += c.length; }, close: async () => {}, abort: async () => {} });
    const job = { id: "T".repeat(43), title: "Building REST APIs", video_id: KEY, is_active: false, is_completed: true, status: "completed", status_label: "Completed", percent: 100,
      downloaded_display: "—", total_display: "—", speed_display: "—", eta_display: "—", notice: "", error_message: "", file_url: "/quick/x/file/", filename: NAME, file_size_display: "3 B", total_bytes: 3, downloaded_bytes: 3 };
    let sent = false;
    const fetchImpl = (url) => {
      if (url.includes("metadata")) return json({ ok: true, video: { url: "https://www.youtube.com/watch?v=" + KEY, title: "T", uploader: "", duration_display: "1:00", upload_date: "", thumbnail_url: "" }, formats: [{ key: "mp4_720", label: "MP4 720p", size_display: "", disabled: false }], default_preset: "mp4_720" });
      if (url.includes("/download/")) return json({ ok: true, video: { ...job, is_active: true, is_completed: false, status: "pending", status_label: "Pending" } }, 201);
      if (url.includes("/file/")) { sent = true; let n = 0; return Promise.resolve({ ok: true, headers: { get: () => "3" }, body: { getReader: () => ({ read: async () => (n++ ? { done: true } : { done: false, value: new Uint8Array(3) }) }) } }); }
      return json({}, 404);
    };
    const srcs = [];
    const p = boot("home", { idb, fetchImpl, savePicker: async () => saved }); saved.win = p.w;
    p.w.EventSource = class { constructor() { srcs.push(this); this.readyState = 1; this.h = {}; } addEventListener() {} close() {} };
    p.$("#url-input").value = "https://youtu.be/" + KEY; p.$("#fetch-form").dispatchEvent(new p.w.Event("submit", { cancelable: true })); await tick();
    p.$("#start-btn").click(); await tick();
    srcs[0].onmessage({ data: JSON.stringify(job) }); await tick(40);
    p.$("#job-save").click(); await tick(150);
    check("file streamed into the chosen destination", sent && written === 3);
    check("handle remembered under the video id for Continue watching", await idbHas(idb, KEY));
  }

  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nall file-handle UI checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("ERROR", e); process.exit(2); });
