# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
python -m pip install -r requirements.txt
python -m playwright install          # required once; installs firefox + chromium binaries
copy .env.example .env                # personal config; gitignored
python downloader.py                  # the only entry point
```

There is no test suite, linter, build step, or package manifest. Python 3.13 is the configured SDK.

## What this is

A single-file script (`downloader.py`, ~750 lines) that scrapes images off one Pinterest board with Playwright,
downloads them with `requests`, then drives a second browser to upload them to iCloud Photos. Everything is module-level
procedural code inside one `with sync_playwright() as p:` block at the bottom of the file; the functions above it are
helpers with no classes or state objects. All configuration is uppercase constants at the top of the file — there is no
CLI. Four of them (`BOARD_URL`, `ICLOUD_ALBUM_NAME`, `ICLOUD_ALBUM_URL`, `ICLOUD_STORAGE_STATE_FILE`) read an env var
so nothing personal is hardcoded; everything else is edited in place. `load_dotenv(BASE_DIR / ".env")` runs before
those constants are evaluated — keep that ordering. A real env var wins over `.env`, which is what makes a one-off
shell override work. Paths hang off `BASE_DIR` (`Path(__file__).resolve().parent`), so the cwd does not matter.

Both browsers run **headless=False on purpose** and the iCloud phase **blocks on `input()`** waiting for the user to
sign in and press Enter. This is an interactive script, not something that can be run unattended or verified end-to-end
by an agent. Do not "fix" it into headless mode.

## Architecture

**Phase 1 — scrape (`SCRAPER_BROWSER_ENGINES`)**: tries firefox, falls back to chromium; each engine also retries across
`www.pinterest.com` / `nl.pinterest.com` host variants (`build_board_url_variants`, `open_board_with_retries`).
Pinterest blocks inconsistently per engine/host, which is why both fallback layers exist.

**Phase 2 — scroll + collect**: the core problem is that Pinterest appends recommendation feeds ("Find some ideas for
this board", "More like this") below the actual board, and both live in the same DOM/masonry grid. The solution is a *
*y-coordinate cutoff**, not a DOM boundary:

- `recommendation_cutoff_y` finds the absolute page-Y of the first boundary heading.
- Once that heading scrolls near the viewport, `maybe_lock_cutoff` **locks** the value into `cutoff_lock_y` and it is
  never recomputed — the headings move as content lazy-loads, so a live cutoff would drift and let recommendations
  through. `current_cutoff_y` is the single accessor: locked value, else a live probe, else `None` in test mode.
- `collect_image_urls` drops any `<img>` whose absolute top is `>= cutoff_y`.

Collection also runs *three times per iteration* (before scrolling, after each 1000px increment, and during settle
checks) because masonry tiles render late and get recycled out of the DOM if you only sample once per scroll. Because it
runs that often, `collect_image_urls` pulls every tile's href/src/top in a single `page.evaluate`
(`COLLECT_IMAGES_SCRIPT`) rather than one round-trip per `<img>` — keep it that way.

Dedup is keyed on the normalized pin URL (`normalize_pin_href` → `https://www.pinterest.com/pin/<id>/`) plus a secondary
set of seen image URLs. `build_image_url_candidates` rewrites the pinimg size segment to produce an ordered fallback
list `/originals/ → /736x/ → /564x/ → /474x/`; the download loop tries each until one returns 200, since `/originals/`
404s for some pins.

Loop exits on: `expected_pin_count` reached (parsed from the "N Pins" text on the page), `STALL_LIMIT` consecutive
iterations with zero new URLs, `MAX_SCROLLS`, or `MAX_RUNTIME_SECONDS` (checked by `runtime_exceeded` at the top of the
iteration *and* inside the inner scroll loop, which otherwise burns ~8s of sleeps past the limit). Abbreviated pin
counts ("1.2k Pins") deliberately return `None` — rounding them into a target would stop the scroll early.

`TEST_MODE_ALL_BOARD_PINS = True` disables the cutoff entirely and keeps scrolling past stalls — use it to see
everything the page yields when debugging why pins are missing.

**Phase 3 — download**: plain `requests.Session` streaming to `downloads/img_<n>.<ext>`, where the extension comes from
the URL suffix or `Content-Type` (`image_extension`) and `<n>` counts files actually written, so a pin that fails every
candidate leaves no gap. `downloads/` is wiped at startup by `clear_downloads_dir`.

**Phase 4 — iCloud upload**: fresh chromium context, restoring cookies from `icloud_storage_state.json` if present (
re-saved after each sign-in). The upload deliberately **never clicks an Upload button** — clicking one opens a native OS
file dialog that Playwright cannot drive on Windows. Instead `upload_files_to_icloud_page` walks every frame looking for
an `input[type=file]` and calls `set_input_files` directly. Preserve this constraint in any change to the upload path.

iCloud's UI has no completion signal, so `wait_for_icloud_upload_completion` polls all frames for
progress/spinner/status-text heuristics and treats N consecutive quiet polls as done, while repeatedly calling
`accept_icloud_duplicate_prompt` to dismiss "already exists" dialogs. That dismisser has an explicit `blockedWords`
list (`upload`, `add`, `import`, `file`, …) so it never re-triggers a file chooser — keep that guard if you touch the
button matching. Read the class off `getAttribute('class')`, never `el.className`: on an SVG that is an
`SVGAnimatedString` and `.toLowerCase()` throws, which silently kills the dismisser for that whole frame. Both the
duplicate prompt and album lookup carry Dutch label variants alongside English.

iCloud does not paint a progress indicator immediately, so `ICLOUD_UPLOAD_START_GRACE_SECONDS` stops the quiet-poll
heuristic concluding "done" during the first seconds and closing the browser mid-transfer.

`ICLOUD_ALBUM_URL` is empty by default and set from the environment when you want a deep link to a specific album. If
the page doesn't resolve to an album view (`/#/a,` in the URL), the code falls back to `open_or_create_album`
label-clicking, and failing that just uploads into the library.

## Notes

- `found_image_links.log` is rewritten on every run when `ENABLE_IMAGE_LOGGING` is on; it's the primary debugging
  artifact for cutoff/collection issues (each line records `imageTop` vs `cutoff`).
- `icloud_storage_state.json` holds a live iCloud session (real Apple auth cookies). It is gitignored — never commit or
  share it. `ICLOUD_STORAGE_STATE_FILE` can relocate it outside the repo.
- `.env` is gitignored and `.env.example` is not. Anything personal belongs in the former; keep the latter to
  placeholders only.