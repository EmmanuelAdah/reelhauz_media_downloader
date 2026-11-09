// Local media remains mounted and playing while navigating between Reelhauz pages.
globalThis.structuredClone = (x) => x;
const { JSDOM } = require("jsdom");
const { IDBFactory } = require("fake-indexeddb");
const fs = require("fs");
const path = require("path");
const js = fs.readFileSync(path.join(__dirname, "..", "downloader/static/downloader/js/app.js"), "utf8");
const pagesDir = path.join(__dirname, ".pages");
const tick = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));
let failed = 0;
const check = (name, condition) => {
  console.log((condition ? "  ok   " : "  FAIL ") + name);
  if (!condition) failed++;
};

const page = (name) => fs.readFileSync(path.join(pagesDir, `${name}.html`), "utf8")
  .replace(/<script src=[^>]*><\/script>/g, "")
  .replace(/<link [^>]*>/g, "");

async function main() {
  const dom = new JSDOM(page("watch"), { runScripts: "outside-only", url: "http://localhost/watch/", pretendToBeVisual: true });
  const w = dom.window;
  const fetches = [];
  w.bootstrap = {
    Toast: class { show() {} },
    Modal: { getOrCreateInstance: () => ({ show() {}, hide() {} }) },
  };
  w.indexedDB = new IDBFactory();
  w.showOpenFilePicker = undefined;
  w.ResizeObserver = class { observe() {} };
  w.requestAnimationFrame = (callback) => setTimeout(callback, 0);
  w.scrollTo = () => {};
  w.URL.createObjectURL = () => "blob:local-media";
  w.URL.revokeObjectURL = () => {};
  w.EventSource = class { addEventListener() {} close() {} };
  w.EventSource.CLOSED = 2;
  w.fetch = async (url) => {
    fetches.push(url);
    const target = new URL(url);
    if (target.pathname === "/settings/") {
      return {
        ok: true,
        redirected: false,
        text: async () => '<main class="content"><button data-theme-set="light">Light</button><button data-theme-set="dark">Dark</button></main>',
      };
    }
    return { ok: true, redirected: false, text: async () => page(target.pathname === "/" ? "home" : "cw") };
  };
  w.eval(js);

  const media = w.document.querySelector("#player");
  let pauseCalls = 0;
  media.pause = () => { pauseCalls++; };
  const fileInput = w.document.querySelector("#file-input");
  const file = new w.File(["media"], "sample.mp4", { type: "video/mp4" });
  Object.defineProperty(fileInput, "files", { configurable: true, value: [file] });
  fileInput.dispatchEvent(new w.Event("change"));
  media.currentTime = 27;

  const homeLink = w.document.querySelector('.nav-list a[href="/"]') || w.document.querySelector('.top-nav a[href="/"]');
  homeLink.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  await tick();
  check("navigates to another in-app page without a document reload", w.location.pathname === "/" && fetches.length === 1);
  check("same video element is kept in the visible mini-player", w.document.querySelector("#persistent-player-mount #player") === media &&
    !w.document.querySelector("#persistent-player").hidden);
  check("navigation did not pause playback or reset its position", pauseCalls === 0 && media.currentTime === 27 && media.src === "blob:local-media");

  w.document.querySelector("#persistent-player-return").dispatchEvent(
    new w.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }),
  );
  await tick();
  check("returning to Play a file restores the original player in place", w.location.pathname === "/watch/" &&
    w.document.querySelector(".content #player") === media && w.document.querySelector("#persistent-player").hidden);
  check("the player remains the same instance after returning", pauseCalls === 0 && media.currentTime === 27);

  const settingsLink = w.document.createElement("a");
  settingsLink.href = "/settings/";
  settingsLink.textContent = "Settings";
  w.document.querySelector(".content").append(settingsLink);
  settingsLink.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  await tick();
  w.document.querySelector('[data-theme-set="dark"]').click();
  check("appearance buttons on dynamically loaded Settings page still change theme", w.document.documentElement.getAttribute("data-bs-theme") === "dark");
  check("selected theme is persisted", w.localStorage.getItem("rh-theme") === "dark");
  w.document.querySelector('[data-theme-set="light"]').click();
  check("light appearance button switches theme back", w.document.documentElement.getAttribute("data-bs-theme") === "light");
  dom.window.close();
}

main().then(() => { if (failed) process.exitCode = 1; });
