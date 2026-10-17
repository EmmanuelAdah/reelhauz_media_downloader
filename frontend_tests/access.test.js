// Access options: the visitor decides how a download identifies itself when YouTube asks for verification.
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
  speed_display: "—", eta_display: "—", notice: "", error_message: "", error_code: "", file_url: "", filename: "", file_size_display: "—", cancelling: false, failed_stage: "", video_id: "abcdefghijk" };
const st = (o) => ({ ...base, ...o });
const META_OK = { ok: true, video: { url: "https://www.youtube.com/watch?v=abcdefghijk", title: "Building REST APIs", uploader: "Chan", duration_display: "18:42", upload_date: "", thumbnail_url: "" },
  formats: [{ key: "mp4_720", label: "MP4 720p", size_display: "40 MB", disabled: false }, { key: "audio_mp3", label: "MP3", size_display: "", disabled: false }], default_preset: "mp4_720" };

function boot({ metadata = () => json(META_OK), ls = {} } = {}) {
  const html = fs.readFileSync(path.join(__dirname, ".pages", "home.html"), "utf8").replace(/<script src=[^>]*><\/script>/g, "").replace(/<link [^>]*>/g, "");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/", pretendToBeVisual: true });
  const w = dom.window, bodies = [], toasts = [], sources = [], clipboard = [];
  for (const [k, v] of Object.entries(ls)) w.localStorage.setItem(k, v);
  w.bootstrap = { Toast: class { constructor(el) { toasts.push(el.textContent); } show() {} }, Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) } };
  w.indexedDB = new IDBFactory(); w.requestAnimationFrame = (f) => setTimeout(f, 0);
  w.Element.prototype.scrollIntoView = function () {};
  w.EventSource = class { constructor(u) { this.url = u; this.readyState = 1; sources.push(this); } addEventListener() {} close() { this.readyState = 2; } };
  w.EventSource.CLOSED = 2;
  Object.defineProperty(w.navigator, "clipboard", { value: { writeText: async (t) => clipboard.push(t) }, configurable: true });
  w.fetch = (url, opts = {}) => {
    const body = opts.body ? JSON.parse(opts.body) : undefined;
    bodies.push([url, body]);
    if (url.includes("metadata")) return metadata(body);
    if (url.endsWith("/download/")) return json({ ok: true, video: st({ status: "pending", status_label: "Pending" }) }, 201);
    if (url.includes("/command/")) return json({ ok: true, command: `yt-dlp -f 'ba/b' '${body.url}'`, command_with_login: `yt-dlp --cookies-from-browser firefox '${body.url}'` });
    return json({ ok: false }, 404);
  };
  w.eval(js);
  const $ = (s) => w.document.querySelector(s), $$ = (s) => [...w.document.querySelectorAll(s)];
  return { w, $, $$, bodies, toasts, sources, clipboard,
    last: (needle) => [...bodies].reverse().find(([u]) => u.includes(needle)),
    async fetchUrl() { $("#url-input").value = "https://youtu.be/abcdefghijk"; $("#fetch-form").dispatchEvent(new w.Event("submit", { cancelable: true })); await tick(); },
    pick(mode) { const r = $(`input[name="access"][value="${mode}"]`); r.checked = true; r.dispatchEvent(new w.Event("change", { bubbles: true })); },
    async chooseCookies(text, name = "cookies.txt") {
      const input = $("#access-cookies-file");
      Object.defineProperty(input, "files", { value: [Object.assign(new w.File([text], name), { text: async () => text })], configurable: true });
      input.dispatchEvent(new w.Event("change")); await tick(20);
    } };
}
const COOKIES = ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tabc";

(async () => {
  console.log("PANEL: collapsed by default, nothing sent unless the visitor chooses");
  {
    const p = boot();
    check("panel is collapsed and 'Automatic' is selected", !p.$("#access").open && p.$('input[name="access"][value="auto"]').checked);
    check("browser-login option present when the app runs locally", !!p.$('input[name="access"][value="browser"]') && p.$$("#access-browser-select option").length >= 5);
    await p.fetchUrl();
    check("automatic mode sends no credentials", p.last("metadata")[1].auth === undefined);
  }

  console.log("VERIFICATION failure at the preview step opens the options");
  {
    const p = boot({ metadata: () => json({ ok: false, error: "YouTube is blocking downloads from this server right now. Please try again later.", code: "bot_check" }, 422) });
    await p.fetchUrl();
    check("error shown", /blocking downloads/.test(p.$("#fetch-error").textContent));
    check("options panel opened by itself with an explanation", p.$("#access").open && !p.$("#access-alert").hidden && /robot/i.test(p.$("#access-alert").textContent));
    p.pick("browser"); p.$("#access-browser-select").value = "firefox"; p.$("#access-browser-select").dispatchEvent(new p.w.Event("change"));
  }
  {
    const seen = [];
    const p = boot({ metadata: (b) => { seen.push(b); return b.auth ? json(META_OK) : json({ ok: false, error: "blocked", code: "bot_check" }, 422); } });
    await p.fetchUrl();
    check("first attempt (automatic) is refused", seen.length === 1 && p.$("#access").open);
    p.pick("browser"); p.$("#access-browser-select").value = "firefox"; p.$("#access-browser-select").dispatchEvent(new p.w.Event("change"));
    check("browser sub-option appears", !p.$("#access-browser").hidden);
    await p.fetchUrl();
    check("retry sends the chosen browser login", JSON.stringify(seen[1].auth) === JSON.stringify({ mode: "browser", browser: "firefox" }));
    check("preview shown after the retry", !p.$("#preview").hidden);
    p.$("#start-btn").click(); await tick();
    check("download request carries the same choice", JSON.stringify(p.last("/download/")[1].auth) === JSON.stringify({ mode: "browser", browser: "firefox" }));
  }

  console.log("COOKIES.TXT: held in memory, sent only with the request, never stored");
  {
    const p = boot();
    p.pick("cookies");
    check("file picker revealed", !p.$("#access-cookies").hidden);
    await p.fetchUrl();
    check("no file chosen: asks for it and sends nothing", /Choose your cookies\.txt/.test(p.$("#fetch-error").textContent) && p.bodies.length === 0);
    await p.chooseCookies(COOKIES);
    check("status shows the file is ready", /Ready: cookies\.txt/.test(p.$("#access-cookies-status").textContent));
    await p.fetchUrl();
    const sent = p.last("metadata")[1].auth;
    check("preview request includes the cookie text", sent.mode === "cookies" && sent.cookies === COOKIES);
    p.$("#start-btn").click(); await tick();
    check("download request includes it too", p.last("/download/")[1].auth.cookies === COOKIES);
    const stored = JSON.stringify({ ...p.w.localStorage }) + JSON.stringify(Object.keys(p.w.sessionStorage));
    check("cookies are NOT written to localStorage/sessionStorage", !stored.includes("SID") && !stored.includes(COOKIES));
    check("only the kind of access is remembered (not cookies mode)", JSON.parse(p.w.localStorage.getItem("rh-access")).mode === "auto");
  }
  {
    const p = boot(); p.pick("browser");
    check("browser choice is remembered for next time", JSON.parse(p.w.localStorage.getItem("rh-access")).mode === "browser");
    const p2 = boot({ ls: { "rh-access": JSON.stringify({ mode: "browser", browser: "brave" }) } });
    check("and restored on the next visit", p2.$('input[name="access"][value="browser"]').checked && p2.$("#access-browser-select").value === "brave");
  }

  console.log("JOB fails on the verification check: the visitor decides what to do next");
  {
    const p = boot(); await p.fetchUrl(); p.$("#start-btn").click(); await tick();
    p.sources[0].onmessage({ data: JSON.stringify(st({ status: "failed", status_label: "Failed", is_active: false, failed_stage: "fetching", error_code: "bot_check",
      error_message: "YouTube is blocking downloads from this server right now. Please try again later." })) }); await tick(40);
    check("failure explained, options opened", p.$("#access").open && /robot/i.test(p.$("#access-alert").textContent));
    check("format picker is back so Start download can be pressed again", !p.$("#preview").hidden && !!p.$('input[name="preset"]:checked'));
    p.pick("browser");
    p.$("#start-btn").click(); await tick();
    check("second try goes out with the new choice and a fresh job card", p.last("/download/")[1].auth.mode === "browser" && p.$("#preview").hidden && !p.$("#job").hidden && !p.$("#job-live").hidden);
  }
  {
    const p = boot(); await p.fetchUrl(); p.$("#start-btn").click(); await tick();
    p.sources[0].onmessage({ data: JSON.stringify(st({ status: "failed", status_label: "Failed", is_active: false, failed_stage: "downloading", error_code: "network", error_message: "Couldn't reach YouTube." })) }); await tick(40);
    check("unrelated failures do NOT pop the options open", !p.$("#access").open);
  }

  console.log("RUN IT YOURSELF: command for the visitor's own computer");
  {
    const p = boot();
    check("command panel hidden at first", p.$("#cli-panel").hidden);
    await p.fetchUrl();
    p.$("#cli-show").click(); await tick(40);
    check("click reveals a ready-made command", !p.$("#cli-panel").hidden && p.$("#cli-command").textContent.startsWith("yt-dlp") && p.$("#cli-show").getAttribute("aria-expanded") === "true");
    check("command is built for the chosen format and URL", p.last("/command/")[1].preset === "mp4_720" && p.last("/command/")[1].url.includes("abcdefghijk"));
    check("second variant with browser login shown", /--cookies-from-browser/.test(p.$("#cli-command-login").textContent));
    const r = p.$('input[name="preset"][value="audio_mp3"]'); r.checked = true; r.dispatchEvent(new p.w.Event("change", { bubbles: true })); await tick(40);
    check("changing the format refreshes the command", p.last("/command/")[1].preset === "audio_mp3");
    p.$("#cli-copy").click(); await tick(20);
    check("Copy puts the command on the clipboard", p.clipboard.length === 1 && p.clipboard[0].startsWith("yt-dlp"));
  }
  {
    const p = boot(); p.$("#cli-show").click(); await tick(20);
    check("with no URL yet it asks for one instead of failing", /Paste a YouTube URL/.test(p.$("#cli-command").textContent));
  }

  console.log(failed ? `\n${failed} CHECK(S) FAILED` : "\nall access-option UI checks passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error("ERROR", e); process.exit(2); });
