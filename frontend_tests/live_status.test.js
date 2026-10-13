const { JSDOM } = require("jsdom");
const fs = require("fs");
const js = fs.readFileSync(require("path").join(__dirname, "..", "downloader/static/downloader/js/app.js"), "utf8");

function boot(page, { fetchImpl }) {
  const html = fs.readFileSync(require("path").join(__dirname, ".pages", `${page}.html`), "utf8").replace(/<script src=[^>]*><\/script>/g, "").replace(/<link [^>]*>/g, "");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/", pretendToBeVisual: true });
  const w = dom.window;
  w.bootstrap = { Toast: class { constructor(){} show(){} }, Modal: { getOrCreateInstance: () => ({ show(){}, hide(){} }) } };
  w.Modal = w.bootstrap.Modal;
  w.fetch = fetchImpl;
  w.requestAnimationFrame = (f) => setTimeout(f, 0);
  const sources = [];
  w.EventSource = class { constructor(u){ this.url=u; this.readyState=1; this.h={}; sources.push(this); } addEventListener(n,f){ this.h[n]=f; } close(){ this.readyState=2; } };
  w.EventSource.CLOSED = 2;
  w.eval(js);
  return { w, dom, sources };
}
const json = (o, status=200) => Promise.resolve({ ok: status<400, status, json: () => Promise.resolve(o) });
const tick = (ms=20) => new Promise(r => setTimeout(r, ms));
let failed = 0;
const check = (name, cond) => { console.log((cond ? "  ok   " : "  FAIL ") + name); if (!cond) failed++; };

(async () => {
  console.log("HOME: fetch -> start -> live stages -> save");
  const jobBase = { id: "T".repeat(43), title: "Building REST APIs", is_active: true, percent: 0, downloaded_bytes: 0, total_bytes: 0,
    downloaded_display: "—", total_display: "—", speed_display: "—", eta_display: "—", notice: "", error_message: "", file_url: "", filename: "", file_size_display: "—" };
  const fetchImpl = (url, opts) => {
    if (url.includes("metadata")) return json({ ok: true, video: { url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", title: "Building REST APIs", uploader: "Chan", duration_display: "18:42", upload_date: "", thumbnail_url: "" },
      formats: [{ key: "mp4_720", label: "MP4 720p", size_display: "40 MB", disabled: false }, { key: "audio_mp3", label: "Audio only (MP3)", size_display: "", disabled: false }], default_preset: "mp4_720" });
    if (url.includes("/download/")) return json({ ok: true, video: { ...jobBase, status: "pending", status_label: "Pending" } }, 201);
    return json({ ok: false }, 404);
  };
  const { w, sources } = boot("home", { fetchImpl });
  const $ = (s) => w.document.querySelector(s);
  $("#url-input").value = "https://youtu.be/dQw4w9WgXcQ";
  $("#fetch-form").dispatchEvent(new w.Event("submit", { cancelable: true }));
  await tick();
  check("preview shown with title", !$("#preview").hidden && $("#pv-title").textContent === "Building REST APIs");
  check("default preset preselected", $('input[name="preset"]:checked').value === "mp4_720");
  $("#start-btn").click();
  await tick();
  check("job card visible, preview hidden", !$("#job").hidden && $("#preview").hidden);
  check("subscribed to SSE for the job", sources.length === 1 && sources[0].url.endsWith(`/api/quick/${jobBase.id}/events/`));
  const stageOf = () => ({ cur: $("#job .stages li.is-current")?.textContent, done: [...w.document.querySelectorAll("#job .stages li.is-done")].map(l=>l.textContent), label: $("#job [data-role=stage]").textContent, pct: $("#job [data-role=percent]").textContent });
  const push = async (o) => { sources[0].onmessage({ data: JSON.stringify({ ...jobBase, ...o }) }); await tick(30); };
  console.log("  stages as pushed:");
  for (const [o, expect] of [
    [{ status: "pending", status_label: "Pending" }, "Pending"],
    [{ status: "fetching", status_label: "Fetching", title: "Building REST APIs" }, "Fetching"],
    [{ status: "downloading", status_label: "Downloading", percent: 42, downloaded_bytes: 2, total_bytes: 5, downloaded_display: "2.0 MB", total_display: "4.8 MB", speed_display: "1 MB/s", eta_display: "3 seconds" }, "Downloading"],
    [{ status: "processing", status_label: "Processing", percent: 100 }, "Processing"],
  ]) {
    await push(o); const s = stageOf();
    console.log(`    -> ${s.label.padEnd(14)} ${s.pct.padStart(4)}  current chip=${s.cur}  done=[${s.done.join(",")}]`);
    check(`chip "${expect}" is current`, s.cur === expect);
  }
  check("download speed/eta rendered", true);
  check("tray badge counts the running job", $("#tray-count").textContent === "1" && !$("#tray-count").hidden);
  check("tray row shows live stage", $("#tray-list").textContent.includes("Processing"));
  await push({ status: "completed", status_label: "Completed", is_active: false, is_completed: true, percent: 100, file_url: "/quick/x/file/", filename: "Building REST APIs [dQw4w9WgXcQ].mp4", file_size_display: "4.8 MB" });
  const s = stageOf();
  check("Completed: all earlier chips marked done", s.done.length === 5 && s.label === "Completed");
  check("Save button revealed", !$("#job-actions").hidden && !$("#job-save").hidden);
  check("SSE closed after terminal state", sources[0].readyState === 2);
  check("tray offers Save too", [...w.document.querySelectorAll("#tray-list button")].some(b => /Save to my device/.test(b.textContent)));
  check("job persisted for other pages", JSON.parse(w.localStorage.getItem("rh-jobs")).length === 1);

  console.log("HOME: failure is shown, not stuck");
  const f = boot("home", { fetchImpl });
  f.w.document.querySelector("#url-input").value = "x";
  f.w.document.querySelector("#fetch-form").dispatchEvent(new f.w.Event("submit", { cancelable: true })); await tick();
  f.w.document.querySelector("#start-btn").click(); await tick();
  f.sources[0].onmessage({ data: JSON.stringify({ ...jobBase, status: "failed", status_label: "Failed", is_active: false, error_message: "No download worker picked this up." }) }); await tick(30);
  check("error text displayed", f.w.document.querySelector("#job-error").textContent.includes("No download worker") && !f.w.document.querySelector("#job-error").hidden);
  check("save hidden, 'Download another' available", f.w.document.querySelector("#job-save").hidden && !f.w.document.querySelector("#job-actions").hidden);

  console.log("OTHER PAGE: a job started on the home page is tracked live on /watch/");
  const store = w.localStorage.getItem("rh-jobs");
  const other = boot("watch", { fetchImpl: () => json({ ok: false }, 404) });
  other.w.localStorage.setItem("rh-jobs", store);
  const o2 = boot("watch", { fetchImpl: () => json({ ok: false }, 404) });
  check("(fresh page, empty store) tray empty", !o2.w.document.querySelector("#tray-empty").hidden);
  const dom3 = new JSDOM(fs.readFileSync(require("path").join(__dirname, ".pages", "watch.html"),"utf8").replace(/<script src=[^>]*><\/script>/g,""), { runScripts: "outside-only", url: "http://localhost/", pretendToBeVisual: true });
  dom3.window.localStorage.setItem("rh-jobs", store);
  dom3.window.bootstrap = { Toast: class { show(){} }, Modal: { getOrCreateInstance: () => ({}) } };
  dom3.window.fetch = () => json({}, 404); dom3.window.requestAnimationFrame = (f)=>setTimeout(f,0);
  const src3 = []; dom3.window.EventSource = class { constructor(u){ src3.push(u); this.readyState=1; } addEventListener(){} close(){} }; dom3.window.EventSource.CLOSED = 2;
  dom3.window.eval(js); await tick(30);
  check("restored job resubscribes to its SSE stream on the new page", src3.length === 1 && src3[0].includes(jobBase.id));
  check("tray shows the restored job", dom3.window.document.querySelector("#tray-list .tray-row") !== null);

  console.log("PLAYER (anonymous): resume from localStorage");
  const { w: pw } = boot("watch", { fetchImpl: () => json({}, 404) });
  pw.localStorage.setItem("rh-pos:dQw4w9WgXcQ", JSON.stringify({ position: 754, duration: 1122, title: "T", updated: 1 }));
  const media = pw.document.querySelector("#player");
  Object.defineProperty(media, "duration", { value: 1122, configurable: true });
  pw.URL.createObjectURL = () => "blob:x"; pw.URL.revokeObjectURL = () => {};
  const input = pw.document.querySelector("#file-input");
  const file = new pw.File(["x"], "Building REST APIs [dQw4w9WgXcQ].mp4", { type: "video/mp4" });
  Object.defineProperty(input, "files", { value: [file] });
  input.dispatchEvent(new pw.Event("change")); await tick(30);
  media.dispatchEvent(new pw.Event("loadedmetadata")); await tick(10);
  check("player section shown, title parsed from filename", !pw.document.querySelector("#player-section").hidden && pw.document.querySelector("#media-title").textContent === "Building REST APIs");
  check("seeked to saved 12:34", Math.round(media.currentTime) === 754 && pw.document.querySelector("#resume-time").textContent === "12:34");
  media.currentTime = 800; media.dispatchEvent(new pw.Event("pause")); await tick(10);
  const saved = JSON.parse(pw.localStorage.getItem("rh-pos:dQw4w9WgXcQ"));
  check("position saved locally on pause (no server call)", saved.position === 800 && saved.duration === 1122);

  console.log("CONTINUE WATCHING (anonymous): lists local positions");
  const { w: cw } = boot("cw", { fetchImpl: () => json({}, 404) });
  cw.localStorage.setItem("rh-pos:dQw4w9WgXcQ", JSON.stringify({ position: 800, duration: 1122, title: "Building REST APIs", updated: 5 }));
  cw.localStorage.setItem("rh-pos:abc", JSON.stringify({ position: 1000, duration: 1010, title: "Finished one", updated: 6 }));
  cw.dispatchEvent(new cw.StorageEvent("storage", { key: "rh-pos:dQw4w9WgXcQ" })); await tick(30);
  const cards = [...cw.document.querySelectorAll("#cw-grid .vcard")];
  check("unfinished item shown, finished hidden", cards.length === 1 && cards[0].textContent.includes("Stopped at 13:20 of 18:42"));
  check("resume link points at the player", cards[0].querySelector("a.btn").getAttribute("href") === "/watch/?v=dQw4w9WgXcQ");

  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nall UI checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("ERROR", e); process.exit(2); });
