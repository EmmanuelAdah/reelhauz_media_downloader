// Cancel button, "Failed while <stage>", cancelled state, tray actions, stable title.
globalThis.structuredClone = (x) => x;
const { JSDOM } = require("jsdom");
const { IDBFactory } = require("fake-indexeddb");
const fs = require("fs");
const path = require("path");
const js = fs.readFileSync(path.join(__dirname, "..", "downloader/static/downloader/js/app.js"), "utf8");
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const json = (o, status = 200) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(o) });
let failed = 0;
const check = (name, cond) => { console.log((cond ? "  ok   " : "  FAIL ") + name); if (!cond) failed++; };

const ID = "T".repeat(43);
const base = { id: ID, title: "", is_active: true, is_completed: false, percent: 0, downloaded_bytes: 0, total_bytes: 0, downloaded_display: "—", total_display: "—",
  speed_display: "—", eta_display: "—", notice: "", error_message: "", file_url: "", filename: "", file_size_display: "—", cancelling: false, failed_stage: "", video_id: "abcdefghijk" };
const st = (o) => ({ ...base, ...o });

function boot({ cancelResponse } = {}) {
  const html = fs.readFileSync(path.join(__dirname, ".pages", "home.html"), "utf8").replace(/<script src=[^>]*><\/script>/g, "").replace(/<link [^>]*>/g, "");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/", pretendToBeVisual: true });
  const w = dom.window, calls = [], toasts = [], sources = [];
  w.bootstrap = { Toast: class { constructor(el) { toasts.push(el.textContent); } show() {} }, Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) } };
  w.indexedDB = new IDBFactory(); w.showOpenFilePicker = undefined;
  w.requestAnimationFrame = (f) => setTimeout(f, 0);
  w.EventSource = class { constructor(u) { this.url = u; this.readyState = 1; sources.push(this); } addEventListener() {} close() { this.readyState = 2; } };
  w.EventSource.CLOSED = 2;
  w.fetch = (url, opts = {}) => {
    calls.push([opts.method || "GET", url]);
    if (url.includes("metadata")) return json({ ok: true, video: { url: "https://www.youtube.com/watch?v=abcdefghijk", title: "Building REST APIs", uploader: "Chan", duration_display: "18:42", upload_date: "", thumbnail_url: "" },
      formats: [{ key: "mp4_720", label: "MP4 720p", size_display: "40 MB", disabled: false }], default_preset: "mp4_720" });
    if (url.endsWith("/download/")) return json({ ok: true, video: st({ status: "pending", status_label: "Pending" }) }, 201);
    if (url.includes("/cancel/")) return cancelResponse ? cancelResponse() : json({ ok: true, discarded: false, video: st({ status: "downloading", status_label: "Downloading", cancelling: true }) });
    return json({ ok: false }, 404);
  };
  w.eval(js);
  return { w, calls, toasts, sources, $: (s) => w.document.querySelector(s), $$: (s) => [...w.document.querySelectorAll(s)] };
}
async function startJob(p) {
  p.$("#url-input").value = "https://youtu.be/abcdefghijk";
  p.$("#fetch-form").dispatchEvent(new p.w.Event("submit", { cancelable: true })); await tick();
  p.$("#start-btn").click(); await tick();
}
const push = async (p, o) => { p.sources[0].onmessage({ data: JSON.stringify(st(o)) }); await tick(30); };
const stage = (p) => p.$("#job [data-role=stage]").textContent;

(async () => {
  console.log("CANCEL on the download page");
  {
    const p = boot(); await startJob(p);
    check("Cancel download button visible while the job runs", !p.$("#job-live").hidden && /Cancel download/.test(p.$("#job-cancel").textContent));
    check("Save/Download-another hidden while running", p.$("#job-actions").hidden);
    await push(p, { status: "downloading", status_label: "Downloading", percent: 40, title: "Building REST APIs" });
    p.$("#job-cancel").click(); await tick(40);
    check("click sends POST /api/quick/<id>/cancel/", p.calls.some(([m, u]) => m === "POST" && u === `/api/quick/${ID}/cancel/`));
    check("button immediately shows Cancelling… and is disabled", p.$("#job-cancel").disabled && /Cancelling/.test(p.$("#job-cancel").textContent));
    check("progress label says Cancelling…", stage(p) === "Cancelling…");
    await push(p, { status: "cancelled", status_label: "Cancelled", is_active: false, percent: 40 });
    check("then the live stream's final state shows Cancelled", stage(p) === "Cancelled");
    check("cancel button gone, 'Download another' offered, Save hidden", p.$("#job-live").hidden && !p.$("#job-actions").hidden && p.$("#job-save").hidden);
    check("message explains nothing was kept", /cancelled/i.test(p.$("#job-hint").textContent) && !p.$("#job-hint").hidden);
    check("toast: Download cancelled.", p.toasts.includes("Download cancelled."));
    check("stream closed after the terminal state", p.sources[0].readyState === 2);
  }
  {
    console.log("CANCEL while still pending (cancelled on the spot)");
    const p = boot({ cancelResponse: () => json({ ok: true, discarded: false, video: st({ status: "cancelled", status_label: "Cancelled", is_active: false }) }) });
    await startJob(p); p.$("#job-cancel").click(); await tick(60);
    check("shows Cancelled straight from the response", stage(p) === "Cancelled" && p.$("#job-live").hidden);
  }
  {
    console.log("CANCEL request fails: the button comes back");
    const p = boot({ cancelResponse: () => json({ ok: false, error: "This download has expired or doesn't exist." }, 404) });
    await startJob(p); p.$("#job-cancel").click(); await tick(60);
    check("error toast + button usable again", p.toasts.some((t) => /expired/.test(t)) && !p.$("#job-cancel").disabled && /Cancel download/.test(p.$("#job-cancel").textContent));
  }

  console.log("FAILURE: shows where it stopped and keeps the video title");
  {
    const p = boot(); await startJob(p);
    check("title shows the preview's title while the server hasn't named it", p.$("#job-title").textContent === "Building REST APIs");
    await push(p, { status: "fetching", status_label: "Fetching", title: "" });
    check("empty server title doesn't revert to 'Preparing download…'", p.$("#job-title").textContent === "Building REST APIs");
    await push(p, { status: "failed", status_label: "Failed", is_active: false, failed_stage: "fetching", error_message: "YouTube is blocking downloads from this server right now. Please try again later." });
    check('label reads "Failed while fetching"', stage(p) === "Failed while fetching");
    const chips = Object.fromEntries(p.$$("#job .stages li").map((li) => [li.dataset.stage, li.className]));
    check("Fetching chip is marked failed, Pending marked done", /is-failed/.test(chips.fetching) && /is-done/.test(chips.pending) && !/is-done|is-current/.test(chips.downloading));
    check("error text shown, cancel gone, Download another offered", /blocking downloads/.test(p.$("#job-error").textContent) && p.$("#job-live").hidden && !p.$("#job-actions").hidden);
  }
  {
    const p = boot(); await startJob(p);
    await push(p, { status: "failed", status_label: "Failed", is_active: false, failed_stage: "downloading", percent: 55, error_message: "x" });
    check('failure mid-download reads "Failed while downloading"', stage(p) === "Failed while downloading");
  }

  console.log("TRAY: Cancel on running jobs, Discard on unsaved files");
  {
    const p = boot(); await startJob(p);
    await push(p, { status: "downloading", status_label: "Downloading", percent: 30, title: "Building REST APIs" });
    const trayBtns = () => p.$$("#tray-list button").map((b) => b.textContent.trim());
    check("running job has a Cancel button in the Downloads tray", trayBtns().includes("Cancel"));
    p.$$("#tray-list button").find((b) => /Cancel/.test(b.textContent)).click(); await tick(40);
    check("tray Cancel posts to the cancel endpoint", p.calls.filter(([m, u]) => m === "POST" && u.includes("/cancel/")).length === 1);
    await push(p, { status: "completed", status_label: "Completed", is_active: false, is_completed: true, percent: 100, file_url: "/quick/x/file/", filename: "T [abcdefghijk].mp4" });
    check("finished-but-unsaved job offers Save and Discard", trayBtns().includes("Save to my device") && trayBtns().includes("Discard"));
    p.$$("#tray-list button").find((b) => b.textContent === "Discard").click(); await tick(20);
    check("Discard asks for confirmation first", /Discard this download/.test(p.$("#confirm-title").textContent) && p.calls.filter(([, u]) => u.includes("/cancel/")).length === 1);
    p.$("#confirm-ok").click(); await tick(60);
    check("after confirming: server told to delete the file, row removed", p.calls.filter(([, u]) => u.includes("/cancel/")).length === 2 && p.$$("#tray-list .tray-row").length === 0);
  }
  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nall cancel/status UI checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("ERROR", e); process.exit(2); });
