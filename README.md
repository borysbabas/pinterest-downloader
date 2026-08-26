# pinterest-downloader

Playwright script that scrapes the images off a Pinterest board, downloads them, and uploads them to iCloud Photos.

## Quick start

```powershell
python -m pip install -r requirements.txt
python -m playwright install
copy .env.example .env
python downloader.py
```

`playwright install` only has to run once; it fetches the Firefox and Chromium binaries.

## What it does

1. Opens the board in Firefox, falling back to Chromium and to the `nl.pinterest.com` host if Pinterest blocks the
   first attempt. If Pinterest answers with its signup wall instead of the board, the run pauses for you to sign in
   and then caches the session.
2. Scrolls the board and collects pin images, stopping at the recommendation feeds (`Find some ideas for this board`,
   `More like this`) that Pinterest appends below the board itself.
3. Downloads each unique pin into `downloads/`, trying `/originals/` first and falling back to the smaller `/736x/`,
   `/564x/` and `/474x/` variants.
4. Opens iCloud Photos in a second browser and uploads the downloaded files.

Both browsers run visibly on purpose — both the Pinterest and the iCloud step may need you to sign in by hand.

## Configuration

Anything personal lives in `.env` next to the script, which is gitignored. Copy `.env.example` to get started:

```dotenv
PINTEREST_BOARD_URL=https://www.pinterest.com/<user>/<board>/
ICLOUD_ALBUM_NAME=Pinterest wallpapers
ICLOUD_ALBUM_URL=https://www.icloud.com/photos/#/a,<ALBUM-UUID>/
```

| Variable                       | Default                                          | Purpose                                    |
|--------------------------------|--------------------------------------------------|--------------------------------------------|
| `PINTEREST_BOARD_URL`          | `https://www.pinterest.com/Raadfxrd/wallpapers/` | Board to scrape                            |
| `PINTEREST_STORAGE_STATE_FILE` | `./pinterest_storage_state.json`                 | Where the signed-in Pinterest session lives |
| `ICLOUD_ALBUM_NAME`            | `Pinterest wallpapers`                           | Album to look up by name                   |
| `ICLOUD_ALBUM_URL`             | *(empty)*                                        | Deep link straight to an album             |
| `ICLOUD_STORAGE_STATE_FILE`    | `./icloud_storage_state.json`                    | Where the signed-in iCloud session lives   |

Leave `ICLOUD_ALBUM_URL` empty to land on the library and have the script look the album up by name instead. A real
environment variable beats whatever `.env` says, so a one-off run can override any of these:

```powershell
$env:PINTEREST_BOARD_URL = "https://www.pinterest.com/<user>/<other-board>/"
python downloader.py
```

Everything else is an uppercase constant at the top of `downloader.py`:

- `MAX_SCROLLS`, `STALL_LIMIT`, `MAX_RUNTIME_SECONDS` — when the scroll loop gives up.
- `ICLOUD_UPLOAD_ENABLED` — set to `False` to stop after downloading.
- `TEST_MODE_ALL_BOARD_PINS` — disables the recommendation cutoff and keeps scrolling past stalls, for debugging why
  pins are missing.

## Pinterest login

Pinterest shows a signup/login wall to logged-out visitors instead of the board, and there are no pin tiles behind it
to scrape. The script detects that (no `a[href*="/pin/"]` tiles, a login URL, or a signup modal) and stops to let you
sign in:

1. Sign in inside the browser window the script already opened — do not open a separate one, the cookies have to land
   in that context.
2. Press Enter in the terminal. The script reloads the board and checks again.
3. The session is written to `pinterest_storage_state.json`, so later runs load straight into the board.

That file holds live Pinterest cookies. It is gitignored, and `PINTEREST_STORAGE_STATE_FILE` can move it outside the
repo. Delete it if the session goes stale and you want to be prompted again. `PINTEREST_LOGIN_WAIT_ENABLED = False` in
`downloader.py` turns the pause into a hard error instead, for unattended runs with a known-good session file.

## iCloud notes

- On the first run the script opens iCloud Photos and waits for you to sign in, then press Enter in the terminal.
- The session is saved to `icloud_storage_state.json` so later runs skip the login. That file holds live Apple auth
  cookies — it is gitignored, and `ICLOUD_STORAGE_STATE_FILE` can move it outside the repo entirely.
- The upload never clicks an Upload button. Clicking one opens a native file dialog that Playwright cannot drive on
  Windows, so the script writes straight to the page's `input[type=file]` instead.
- If album navigation fails because of UI or locale differences, the files still land in your library.

## Debugging

`found_image_links.log` is rewritten on every run and records `imageTop` against `cutoff` for each collected image. It
is the first place to look when pins are missing or recommendations slip through.
