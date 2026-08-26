# pinterest-downloader

Playwright script that scrapes the images off a Pinterest board, downloads them, and uploads them to iCloud Photos.

## Quick start

```powershell
python -m pip install -r requirements.txt
python -m playwright install
python downloader.py
```

`playwright install` only has to run once; it fetches the Firefox and Chromium binaries.

## What it does

1. Opens the board in Firefox, falling back to Chromium and to the `nl.pinterest.com` host if Pinterest blocks the
   first attempt.
2. Scrolls the board and collects pin images, stopping at the recommendation feeds (`Find some ideas for this board`,
   `More like this`) that Pinterest appends below the board itself.
3. Downloads each unique pin into `downloads/`, trying `/originals/` first and falling back to the smaller `/736x/`,
   `/564x/` and `/474x/` variants.
4. Opens iCloud Photos in a second browser and uploads the downloaded files.

Both browsers run visibly on purpose — the iCloud step needs you to sign in by hand.

## Configuration

Everything lives in the constants at the top of `downloader.py`. The four values that are personal to your setup can
also be supplied through the environment:

| Variable                | Default                                          | Purpose                          |
|-------------------------|--------------------------------------------------|----------------------------------|
| `PINTEREST_BOARD_URL`   | `https://www.pinterest.com/Raadfxrd/wallpapers/` | Board to scrape                  |
| `ICLOUD_ALBUM_NAME`     | `Pinterest wallpapers`                           | Album to look up by name         |
| `ICLOUD_ALBUM_URL`      | *(empty)*                                        | Deep link straight to an album   |

```powershell
$env:PINTEREST_BOARD_URL = "https://www.pinterest.com/<user>/<board>/"
$env:ICLOUD_ALBUM_URL = "https://www.icloud.com/photos/#/a,<ALBUM-UUID>/"
python downloader.py
```

Leave `ICLOUD_ALBUM_URL` empty to land on the library and have the script look the album up by name instead.

Other constants worth knowing:

- `MAX_SCROLLS`, `STALL_LIMIT`, `MAX_RUNTIME_SECONDS` — when the scroll loop gives up.
- `ICLOUD_UPLOAD_ENABLED` — set to `False` to stop after downloading.
- `TEST_MODE_ALL_BOARD_PINS` — disables the recommendation cutoff and keeps scrolling past stalls, for debugging why
  pins are missing.

## iCloud notes

- On the first run the script opens iCloud Photos and waits for you to sign in, then press Enter in the terminal.
- The session is saved to `icloud_storage_state.json` so later runs skip the login. That file holds live Apple auth
  cookies — it is gitignored and should stay out of any repository.
- The upload never clicks an Upload button. Clicking one opens a native file dialog that Playwright cannot drive on
  Windows, so the script writes straight to the page's `input[type=file]` instead.
- If album navigation fails because of UI or locale differences, the files still land in your library.

## Debugging

`found_image_links.log` is rewritten on every run and records `imageTop` against `cutoff` for each collected image. It
is the first place to look when pins are missing or recommendations slip through.
