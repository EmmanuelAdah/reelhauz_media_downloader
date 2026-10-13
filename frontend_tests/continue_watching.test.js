// Continue watching / Recently watched: every played video is listed, nothing vanishes.
globalThis.structuredClone = (x) => x;
const { JSDOM } = require("jsdom");
const { IDBFactory } = require("fake-indexeddb");
const fs = require("fs");
const path = require("path");
const js = fs.readFileSync(path.join(__dirname, "..", "downloader/static/downloader/js/app.js"), "utf8");
const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
let failed = 0;
const check = (name, cond) => { console.log((cond ? "  ok   " : "  FAIL ") + name); if (!cond) failed++; };

function boot(page, ls) {
  const html = fs.readFileSync(path.join(__dirname, ".pages", `${page}.html`), "utf8").replace(/<script src=[^>]*><\/script>/g, "").replace(/<link [^>]*>/g, "");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/", pretendToBeVisual: true });
  const w = dom.window;
  if (ls) for (const [k, v] of Object.entries(ls)) w.localStorage.setItem(k, v);
  w.bootstrap = { Toast: class { show() {} }, Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) } };
  w.indexedDB = new IDBFactory(); w.showOpenFilePicker = async () => { throw Object.assign(new Error(), { name: "AbortError" }); };
  w.fetch = () => Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  w.requestAnimationFrame = (f) => setTimeout(f, 0);
  w.EventSource = class { addEventListener() {} close() {} };
  w.URL.createObjectURL = () => "blob:m"; w.URL.revokeObjectURL = () => {};
  w.eval(js); return w;
}
const pos = (position, duration, title, updated) => JSON.stringify({ position, duration, title, updated });
const titles = (w, sel) => [...w.document.querySelectorAll(sel + " .vcard-title")].map((e) => e.textContent);

(async () => {
  console.log("LIST: in-progress, finished and barely-started videos are all kept");
  const ls = {
    "rh-pos:aaaaaaaaaaa": pos(300, 600, "In progress A", 5),
    "rh-pos:bbbbbbbbbbb": pos(200, 900, "In progress B", 4),
    "rh-pos:ccccccccccc": pos(598, 600, "Finished C", 3),
    "rh-pos:ddddddddddd": pos(3, 600, "Just started D", 2),
    "rh-pos:eeeeeeeeeee": pos(10, 10, "Short clip E (finished)", 1),
  };
  const w = boot("cw", ls); await tick(120);
  check("Continue watching shows the two unfinished videos, newest first", JSON.stringify(titles(w, "#cw-grid")) === JSON.stringify(["In progress A", "In progress B"]));
  check("Recently watched shows the other three (nothing is hidden)", titles(w, "#recent-grid").length === 3);
  const recent = [...w.document.querySelectorAll("#recent-grid .vcard")];
  check("finished video is labelled Watched with Play again", recent.some((c) => c.textContent.includes("Watched") && /play again/i.test(c.textContent)));
  check("barely-started video is labelled Just started", recent.some((c) => c.textContent.includes("Just started")));
  check("both section headings visible, empty state hidden", !w.document.querySelector("#cw-progress").hidden && !w.document.querySelector("#cw-recent").hidden && w.document.querySelector("#cw-empty").hidden);

  console.log("REFRESH: overlapping refreshes never duplicate or drop entries");
  for (let i = 0; i < 8; i++) w.dispatchEvent(new w.StorageEvent("storage", { key: "rh-pos:aaaaaaaaaaa" }));
  w.document.dispatchEvent(new w.Event("visibilitychange"));
  await tick(300);
  const total = w.document.querySelectorAll("#cw-grid .vcard, #recent-grid .vcard").length;
  check("exactly 5 cards after 9 concurrent refreshes", total === 5);

  console.log("PLAYER: every opened file is recorded, even when closed quickly");
  const p = boot("watch");
  const media = p.document.querySelector("#player"), input = p.document.querySelector("#file-input");
  const open = async (name, duration) => {
    Object.defineProperty(input, "files", { value: [new p.File(["x"], name, { type: "video/mp4" })], configurable: true });
    input.dispatchEvent(new p.Event("change")); await tick(40);
    Object.defineProperty(media, "duration", { value: duration, configurable: true });
    media.dispatchEvent(new p.Event("loadedmetadata")); await tick(40);
  };
  await open("Clip one [aaaaaaaaaaa].mp4", 8);          // opened, never played
  await open("Clip two [bbbbbbbbbbb].mp4", 12);          // opened, never played
  media.currentTime = 12; media.dispatchEvent(new p.Event("ended")); await tick(40); // finished
  await open("Clip three [ccccccccccc].mp4", 600);
  media.currentTime = 90; media.dispatchEvent(new p.Event("pause")); await tick(40);
  const keys = Object.keys(p.localStorage).filter((k) => k.startsWith("rh-pos:")).sort();
  check("three different files -> three saved entries", keys.length === 3);
  const e = (k) => JSON.parse(p.localStorage.getItem("rh-pos:" + k));
  check("never-played file is still listed (position 0)", e("aaaaaaaaaaa").position === 0 && e("aaaaaaaaaaa").title === "Clip one");
  check("finished file stored at its end", e("bbbbbbbbbbb").position === 12);
  check("partially watched file stores its position", e("ccccccccccc").position === 90);
  const ls2 = {}; keys.forEach((k) => (ls2[k] = p.localStorage.getItem(k)));
  const w2 = boot("cw", ls2); await tick(150);
  check("Continue watching page lists all three", w2.document.querySelectorAll("#cw-grid .vcard, #recent-grid .vcard").length === 3);

  console.log("PLAYER: opening the same file again does not wipe its saved position before resuming");
  const q = boot("watch", { "rh-pos:ccccccccccc": pos(90, 600, "Clip three", 9) });
  const qm = q.document.querySelector("#player"), qi = q.document.querySelector("#file-input");
  Object.defineProperty(qi, "files", { value: [new q.File(["x"], "Clip three [ccccccccccc].mp4", { type: "video/mp4" })], configurable: true });
  qi.dispatchEvent(new q.Event("change"));
  qm.dispatchEvent(new q.Event("pause")); // an early pause, before the saved position was looked up
  await tick(40);
  check("early pause did not overwrite 90s with 0", JSON.parse(q.localStorage.getItem("rh-pos:ccccccccccc")).position === 90);
  Object.defineProperty(qm, "duration", { value: 600, configurable: true });
  qm.dispatchEvent(new q.Event("loadedmetadata")); await tick(40);
  check("then resumes at 1:30", Math.round(qm.currentTime) === 90 && q.document.querySelector("#resume-time").textContent === "1:30");

  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nall continue-watching checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("ERROR", e); process.exit(2); });
