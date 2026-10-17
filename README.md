# Reelhauz: YouTube downloader and player

A Django 5 app that downloads a YouTube video with **yt-dlp** + **FFmpeg** on the server, streams it to **your
device** (you choose where to save it), and remembers where you stopped when you play it back.
Dependencies are managed with **uv**. Downloads run in background threads of the app itself, so **one command starts
everything**; Celery + Redis is available as an option for bigger deployments.

> **Use responsibly.** Only download content you own, that is licensed for download, or that you otherwise have the
> right to copy. Downloading from YouTube may violate its Terms of Service; you are responsible for how you use this software.

## What is (and isn't) stored

| Data | Where | Lifetime |
|---|---|---|
| Video / audio file | Temporary folder on the server (`DOWNLOAD_TMP_DIR`) | Deleted as soon as you've received it (or after `TRANSFER_TTL_SECONDS`) |
| Job progress (stage, %, speed, ETA) | The cache only (Redis if `REDIS_URL` is set, otherwise in-process memory), under a random token | Deleted with the file |
| Where you stopped watching | PostgreSQL, **only for logged-in users** (`PlaybackPosition`: media key, title, position, duration) | Until you remove it |
| Where you stopped watching, anonymous | Your browser's `localStorage` only; never sent to the server | Until you clear it |
| Where the file is on your device (Chrome, Edge, Brave) | Your browser's IndexedDB only (a `FileSystemFileHandle`); never sent to the server | Until you remove the video from Continue watching, use *Forget location*, or clear watch history |
| Access you choose for one download (a cookies.txt, or a browser to read) | A private owner-only temporary file, or nothing | Deleted the moment that download ends. Other sites' cookies are discarded before anything is written; never put in the cache, logs or database |

| Access you choose for one download (cookies.txt, or a browser to read) | A private (owner-only) temporary file / nothing | Deleted the moment that download ends; other sites' cookies are discarded before anything is written; never put in the cache, logs or database |

No video is ever stored in the database or in Redis. Nothing is stored at all for visitors who aren't logged in
(beyond a rate-limit counter that expires within a minute).

## How it works

```
Browser ──POST /api/quick/download/──► job state (cache) ──► background thread* ──► yt-dlp + FFmpeg ──► temp folder
   ▲                                          │ every progress tick
   ├── live push: GET /api/quick/<token>/events/ (Server-Sent Events, polling fallback)
   ├── POST /api/quick/<token>/cancel/ ──► stops the download and deletes the partial files
   └── GET /quick/<token>/file/ ──► streamed to the browser ──► server deletes file + state
   * or a Celery worker when DOWNLOAD_BACKEND=celery
```

1. **Fetch** a URL → title, thumbnail, duration and the formats that exist for that video (nothing is downloaded yet).
2. **Start** → the UI shows **Pending → Fetching → Downloading → Processing → Completed**, pushed live
   (percent, bytes, speed, ETA). The lookup from step 1 is reused, so YouTube is asked once more rather than twice.
   *Fetching* lasts until the first bytes arrive. If a download fails, the stage that failed is highlighted
   ("Failed while fetching") together with the reason.
   **Cancel download** is on the job card and in the Downloads tray for any running download. It stops yt-dlp within a
   moment, removes the partial files and frees the slot. With `DOWNLOAD_BACKEND=celery`, a missing worker is reported
   immediately, and a job no worker picks up fails after `QUEUE_TIMEOUT_SECONDS` instead of staying on "Pending".
   **If YouTube asks to verify it isn't a robot** (the usual reason downloads from a server fail), the page opens
   *"Choose how to continue"* and the visitor decides how the request identifies itself:
   * **Use my browser's YouTube login**: yt-dlp reads the login from a browser on this computer. Offered only when the app is
     being used on the machine that runs it (see `ALLOW_BROWSER_COOKIES`).
   * **Use a cookies.txt file**: exported from the visitor's own browser. Only YouTube/Google cookies are kept; they sit in an
     owner-only temporary file for that one download and are deleted when it ends.
   * **Download on my own computer**: a ready-made `yt-dlp` command that skips the server entirely and uses the visitor's own
     connection. Files named `Title [id].ext` open in *Play a file* with the place restored.
   A web page can't download from YouTube by itself (YouTube doesn't allow cross-origin requests from browsers), which is why
   the options are server-assisted or run locally.
3. **Save to my device** → in Chromium browsers (Chrome, Edge, Brave, Opera on `https://` or `localhost`) a
   *Save as…* dialog lets you pick any folder and file name, and the file is streamed straight into it. Firefox and
   Safari use the normal browser download (turn on "Always ask where to save files" to choose the folder).
   Files are named `Title [videoid].mp4`.
4. **Play a file** (`/watch/`) → the first time, open the file from your device (it plays locally and is never uploaded).
   The `[videoid]` in the name identifies the video, so your last position is restored ("Continuing from 12:34", with a
   × to dismiss it). Positions are saved every few seconds, on pause and when you leave the page.
5. **Continue watching** lists everything you've played: unfinished videos under *Continue watching*, finished or just-opened ones under *Recently watched*. **Resume reopens the file with no file picker.** Browsers never reveal real file paths and a
   server session can't hold a reference to a file on your disk, so Reelhauz keeps the browser's own file handle
   (File System Access API, Chromium browsers) in IndexedDB. A handle is saved when you use the *Save to my device*
   dialog or open a file through the player. On Resume:
   * access is re-granted inside your click (Chrome may show its small "allow" prompt after a restart), the file is
     checked, and the player opens it at your saved position;
   * if the file was moved, renamed or deleted you get an alert ("… no longer exists on this device") with
     *Locate file…* and *Remove from list*;
   * **removing a video from the list deletes its saved position *and* its remembered file location**;
   * Firefox/Safari (no File System Access API) and other devices of the same account can't know the location, so
     there you are asked to locate the file once.
   The list updates live across tabs (BroadcastChannel) and, for accounts, across devices.
6. A **Downloads** button at the top of every page tracks running jobs, survives navigation and reloads, and offers
   *Cancel* for running ones and *Save to my device* / *Discard* for finished ones.

## Run it (local)

Requirements: [uv](https://docs.astral.sh/uv/). **FFmpeg needs no separate install**: `uv sync` bundles one, and a system
FFmpeg is used instead if you have it. (PostgreSQL and Redis are optional.)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh       # macOS: brew install uv | Windows: winget install astral-sh.uv
# optional: use your own FFmpeg instead of the bundled one:  sudo apt install -y ffmpeg | brew install ffmpeg | winget install Gyan.FFmpeg

uv sync                                                 # Python 3.12 venv + locked deps (pyproject.toml / uv.lock)
cp .env.example .env                                    # then set DJANGO_SECRET_KEY and DATABASE_URL (see below)
uv run python manage.py migrate
uv run python manage.py runserver                       # http://127.0.0.1:8000: this one command is everything
```

Generate a secret key with `uv run python -c "import secrets; print(secrets.token_urlsafe(50))"`.
For a zero-setup database set `DATABASE_URL=sqlite:///db.sqlite3` (or leave it empty). For PostgreSQL:

```bash
sudo -u postgres psql -c "CREATE USER video_user WITH PASSWORD 'video_pass';" -c "CREATE DATABASE video_downloader OWNER video_user;"
```

**No separate worker and no Redis are needed.** Downloads run in background threads of the web process
(`DOWNLOAD_BACKEND=thread`, the default). Optional extras:

* **Redis** (`REDIS_URL=redis://localhost:6379/0`): required if you run several gunicorn worker processes (they share
  job state through it); recommended in production.
* **Celery** (`DOWNLOAD_BACKEND=celery`): a dedicated worker does the downloading, so it survives web restarts and scales
  out. Needs Redis and a second process: `uv run celery -A config worker --loglevel=info`. The app tells you immediately
  if it can't find a worker.

Upgrading from an earlier version that kept a server-side library? `migrate` removes the old tables; you can delete the
old `media/` folder.

## Docker

```bash
cp .env.example .env
docker compose up --build        # web (gunicorn, runs migrations) + db + redis + worker (production style: DOWNLOAD_BACKEND=celery)
```
Open http://localhost:8000. Web and worker share the `tmp_data` volume (staging folder) and Redis (job state).
The image installs FFmpeg and uses `uv sync --frozen`. Gunicorn runs with threads because each live-progress connection holds one.

## Troubleshooting

### "YouTube is blocking downloads from this server" / "Sign in to confirm you're not a bot"
This comes from YouTube, not from the app's login. YouTube challenges requests it considers automated, mostly based on
the IP address (cloud/datacenter IPs, VPNs and repeated requests get flagged). Visitors only see a short "try again
later" message; in debug mode (and the server log) you also see the operator hint.

Related messages ("The page needs to be reloaded", "Sign in to confirm you're not a bot") are all YouTube refusing a client set; the app tries the others and, if every one is refused, reports the original bot check.

**You decide, in the page.** When YouTube refuses, the app opens *"Choose how to continue"* by itself (preview or download
step) with the three options above, so no `.env` editing is needed. Use *Use my browser's YouTube login* if you're on the
machine running the app, otherwise a cookies.txt, otherwise the command for your own computer. YouTube can restrict accounts
used for automated downloads, so use an account you're comfortable with.

**What the app already does for you:** it installs `yt-dlp[default]` (the YouTube challenge solver), detects a JavaScript
runtime (Deno or Node ≥ 20, force one with `YTDLP_JS_RUNTIME`), and when YouTube shows a bot check it **automatically
retries with other YouTube client sets** (`tv`, `web_embedded`, `visionos`, `mweb`: the ones yt-dlp lists as needing no
proof-of-origin token) and remembers the one that worked for an hour.

**Find out what works from your machine:**
```bash
uv run python manage.py check_ytdlp https://youtu.be/<id>
```
It prints each client set's result. If one works, you're done (the app uses it automatically). If all say *bot_check*:

1. **Running on your own computer?** Choose *Use my browser's YouTube login* in the page, or add `YTDLP_COOKIES_FROM_BROWSER=firefox` (or `chrome`, or `chrome:Profile 1`) to `.env`,
   while logged in to YouTube in that browser. yt-dlp reads the browser's cookies directly. Chrome must be closed on some
   systems; Firefox is the most reliable. YouTube can restrict accounts used for automated downloads, so a throwaway
   account is wise.
2. **Change the IP.** A home connection (no VPN) usually works; cloud servers often don't. Or route through a residential
   proxy: `YTDLP_PROXY=http://user:pass@host:port`.
3. **Cookies file:** export `youtube.com` cookies (Netscape format) and set `YTDLP_COOKIES_FILE=/path/to/cookies.txt`
   (needed on servers where there's no browser). Never commit this file.
4. **Advanced:** `uv add bgutil-ytdlp-pot-provider` and run its provider server; yt-dlp discovers it automatically.
5. Keep yt-dlp current, YouTube changes weekly: `uv lock --upgrade-package yt-dlp && uv sync`.

### "The media tool FFmpeg couldn't be found"
FFmpeg merges video and audio and converts to MP3/M4A. The app looks for it in this order: `FFMPEG_LOCATION` (a binary or its
folder), FFmpeg on your PATH, then the copy bundled by `uv sync`. If you see this message, run `uv sync` (it installs the
bundled copy), or install FFmpeg yourself and restart the app. `uv run python manage.py check_ytdlp` shows which FFmpeg is in use.

## Configuration

See `.env.example` (every variable is documented there). Behind nginx/Caddy: set `DJANGO_BEHIND_PROXY=True`,
`DJANGO_SECURE_COOKIES=True`, `TRUSTED_PROXY_COUNT`, and disable proxy buffering for `/api/quick/*/events/`
(the app already sends `X-Accel-Buffering: no`).

## Commands

```bash
uv run python manage.py check_ytdlp [url]      # diagnose yt-dlp, JS runtime, FFmpeg, cookies, proxy, and a live extraction
uv run python manage.py test downloader        # 135 tests; includes real yt-dlp + FFmpeg runs against a local server
(cd frontend_tests && npm install && npm test) # UI checks: real pages + app.js in jsdom (live stages, cancel, remembered files, continue watching)
uv run python manage.py cleanup_transfers      # purge expired temp files now (also runs automatically)
uv add <pkg> / uv remove <pkg>                 # manage dependencies
uv lock --upgrade-package yt-dlp && uv sync    # YouTube changes often: keep yt-dlp current
```

## Security notes

Single-video YouTube URLs only; users submit a preset key, never a format string, path or command; random 256-bit job
tokens; temp files confined to a per-job folder; per-IP/user rate limits; server-wide concurrency cap; size and duration
limits; CSRF on every POST; positions only for the logged-in owner.

## Project structure

```
manage.py  pyproject.toml  uv.lock  Dockerfile  docker-compose.yml  .env.example
config/            settings, urls, wsgi/asgi, celery app
downloader/
  models.py        PlaybackPosition, UserPreference
  services/        ytdlp_service.py (yt-dlp wrapper)  quick_service.py (jobs, live state, cleanup)
  views/           quick.py (download + SSE)  playback.py (positions, pages)  auth.py  common.py
  tasks.py  forms.py  presets.py  throttle.py  utils.py  urls.py
  management/commands/cleanup_transfers.py  check_ytdlp.py
  templates/downloader/   base, home, watch, continue_watching, settings, auth/, components/
  static/downloader/      css/app.css  js/app.js
  tests/
frontend_tests/    jsdom checks for the JavaScript (live status, cancel, remembered files, continue watching)
```
