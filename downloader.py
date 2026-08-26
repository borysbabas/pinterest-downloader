import os
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright

BOARD_URL = "https://www.pinterest.com/Raadfxrd/wallpapers/"
BOARD_GOTO_TIMEOUT_MS = 45000
BOARD_GOTO_RETRIES = 3
BOARD_RETRY_DELAY_SECONDS = 2
SCRAPER_BROWSER_ENGINES = ("firefox", "chromium")
MAX_SCROLLS = 100
STALL_LIMIT = 3
SCROLL_STEP = 4000
SCROLL_INCREMENT = 1000
SCROLL_SETTLE_CHECKS = 2
SCROLL_SETTLE_SECONDS = 0.8
MAX_RUNTIME_SECONDS = 900
TEST_MODE_ALL_BOARD_PINS = False
SUGGESTION_BOUNDARY_TEXTS = (
    "Find some ideas for this board",
    "More like this",
)
ICLOUD_UPLOAD_ENABLED = True
ICLOUD_ALBUM_NAME = "Pinterest wallpapers"
ICLOUD_PHOTOS_URL = "https://www.icloud.com/photos/"
# Optional deep link to a specific album, e.g.
#   https://www.icloud.com/photos/#/a,<ALBUM-UUID>/
# Leave empty to open the library and look the album up by name instead.
ICLOUD_ALBUM_URL = os.environ.get("ICLOUD_ALBUM_URL", "")
ICLOUD_STORAGE_STATE_FILE = "icloud_storage_state.json"
ICLOUD_UPLOAD_WAIT_TIMEOUT_SECONDS = 900
ICLOUD_UPLOAD_POLL_SECONDS = 2
ICLOUD_UPLOAD_STABLE_POLLS = 5
ICLOUD_UPLOAD_START_GRACE_SECONDS = 30
ICLOUD_FILE_INPUT_WAIT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_BYTES = 65536
ENABLE_IMAGE_LOGGING = True
IMAGE_LOG_FILE = "found_image_links.log"

os.makedirs("downloads", exist_ok=True)

PIN_HREF_RE = re.compile(r"/pin/([^/?#]+)/?")
PINIMG_SIZE_SEGMENT_RE = re.compile(r"/(?:\d+x|originals)/")
THUMBNAIL_PATH_HINTS = (
    "/75x75_RS/",
    "/60x60_RS/",
    "/30x30_RS/",
    "/15x15_RS/",
)
BOARD_PIN_COUNT_RE = re.compile(r"\b(\d[\d.,]*)\s*([km])?\s+Pins?\b", re.IGNORECASE)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def clear_downloads_dir(downloads_dir):
    folder = Path(downloads_dir)
    if not folder.exists():
        return

    for entry in folder.iterdir():
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
        except Exception as cleanup_error:
            print(f"Could not remove old download item: {entry} ({cleanup_error})")


def init_image_log():
    if not ENABLE_IMAGE_LOGGING:
        return

    with open(IMAGE_LOG_FILE, "w", encoding="utf-8") as log_file:
        log_file.write("# Found image links\n")


def log_found_image(iteration, key, src, pin_href, image_top, cutoff_y):
    if not ENABLE_IMAGE_LOGGING:
        return

    line = (
        f"iter={iteration} imageTop={image_top} cutoff={cutoff_y} "
        f"key={key} pinHref={pin_href} src={src}"
    )
    print(f"[FOUND] {line}")
    with open(IMAGE_LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")


def normalize_pin_href(href):
    if not href:
        return None

    href = href.split("?", 1)[0].split("#", 1)[0]
    match = PIN_HREF_RE.search(href)
    if not match:
        return href

    return f"https://www.pinterest.com/pin/{match.group(1)}/"


def normalize_image_src(src):
    if not src:
        return None

    parts = urlsplit(src)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def build_image_url_candidates(src):
    base = normalize_image_src(src)
    if not isinstance(base, str) or not base:
        return []

    candidates = []
    for size in ("/originals/", "/736x/", "/564x/", "/474x/"):
        alt = PINIMG_SIZE_SEGMENT_RE.sub(size, base, count=1)
        if alt not in candidates:
            candidates.append(alt)

    if base not in candidates:
        candidates.append(base)

    return candidates


def image_extension(url, content_type):
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix

    subtype = (content_type or "").split(";", 1)[0].strip().lower().removeprefix("image/")
    if subtype:
        mapped = ".jpg" if subtype == "jpeg" else f".{subtype}"
        if mapped in IMAGE_EXTENSIONS:
            return mapped

    return ".jpg"


def download_image(session, url, index, downloads_dir="downloads"):
    with session.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, stream=True) as response:
        response.raise_for_status()
        extension = image_extension(url, response.headers.get("Content-Type"))
        output_path = Path(downloads_dir) / f"img_{index}{extension}"
        with open(output_path, "wb") as image_file:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    image_file.write(chunk)

    return output_path.resolve()


def recommendation_cutoff_y(page):
    scroll_y = page.evaluate("window.scrollY")
    cutoff = None

    for text in SUGGESTION_BOUNDARY_TEXTS:
        locator = page.get_by_text(text, exact=True)
        if locator.count() == 0:
            continue

        try:
            box = locator.first.bounding_box()
        except Exception:
            continue

        if not box:
            continue

        absolute_y = box["y"] + scroll_y
        if cutoff is None or absolute_y < cutoff:
            cutoff = absolute_y

    return cutoff


def extract_expected_pin_count(page):
    try:
        body_text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None

    match = BOARD_PIN_COUNT_RE.search(body_text or "")
    if not match:
        return None

    # Pinterest abbreviates large boards ("1.2k Pins"). Rounding that back up to
    # an exact target would stop the scroll early, so let the stall detection run.
    if match.group(2):
        print(f"Board pin count is approximate ({match.group(0).strip()}); relying on stall detection.")
        return None

    try:
        return int(match.group(1).replace(",", "").replace(".", ""))
    except Exception:
        return None


def build_board_url_variants(board_url):
    parsed = urlsplit(board_url)
    if not parsed.netloc:
        return [board_url]

    hosts = [parsed.netloc]
    for host in ("www.pinterest.com", "nl.pinterest.com"):
        if host not in hosts:
            hosts.append(host)

    variants = []
    for host in hosts:
        url = urlunsplit((parsed.scheme or "https", host, parsed.path, parsed.query, parsed.fragment))
        if url not in variants:
            variants.append(url)

    return variants


def open_board_with_retries(page, board_url):
    urls = build_board_url_variants(board_url)
    errors = []

    for url in urls:
        for attempt in range(1, BOARD_GOTO_RETRIES + 1):
            try:
                print(f"Opening board: {url} (attempt {attempt}/{BOARD_GOTO_RETRIES})")
                page.goto(url, wait_until="domcontentloaded", timeout=BOARD_GOTO_TIMEOUT_MS)
                return
            except Exception as nav_error:
                msg = str(nav_error)
                errors.append(f"{url} attempt {attempt}: {msg}")
                print(f"Navigation failed: {msg}")
                time.sleep(BOARD_RETRY_DELAY_SECONDS)

    raise RuntimeError(
        "Could not open Pinterest board after retries. "
        f"Tried URLs: {', '.join(urls)}. Last error: {errors[-1] if errors else 'unknown'}"
    )


# Collected in one round-trip: the board holds hundreds of tiles and this runs
# several times per scroll step, so a per-element evaluate is far too chatty.
COLLECT_IMAGES_SCRIPT = """
() => Array.from(document.querySelectorAll('img[src]')).map((el) => {
    let pin = el.closest('a[href*="/pin/"]');
    if (!pin) {
        const card = el.closest('[data-test-id*="pin"], [data-grid-item], [role="listitem"], article');
        if (card) {
            pin = card.querySelector('a[href*="/pin/"]');
        }
    }
    const rect = el.getBoundingClientRect();
    return {
        pinHref: pin ? pin.href : '',
        src: el.currentSrc || el.src || el.getAttribute('src') || '',
        imageTop: rect.top + window.scrollY
    };
})
"""


def collect_image_urls(page, downloads, seen_image_urls, cutoff_y=None, iteration=0):
    try:
        images = page.evaluate(COLLECT_IMAGES_SCRIPT)
    except Exception as collect_error:
        print(f"Could not read board images this round: {collect_error}")
        return 0

    new_urls = 0
    for details in images:
        image_top = details.get("imageTop")
        if cutoff_y is not None and isinstance(image_top, (int, float)) and image_top >= cutoff_y:
            continue

        raw_pin_href = details.get("pinHref")
        raw_src = details.get("src")

        # Keep only actual pin-card images from the board feed.
        key = normalize_pin_href(raw_pin_href)
        if not key:
            continue
        if not raw_src or "pinimg.com" not in raw_src:
            continue
        if not TEST_MODE_ALL_BOARD_PINS and any(hint in raw_src for hint in THUMBNAIL_PATH_HINTS):
            continue

        candidates = build_image_url_candidates(raw_src)
        src = candidates[0] if candidates else None

        if key and src and key not in downloads and src not in seen_image_urls:
            downloads[key] = candidates
            seen_image_urls.add(src)
            new_urls += 1
            log_found_image(
                iteration=iteration,
                key=key,
                src=src,
                pin_href=details.get("pinHref"),
                image_top=image_top,
                cutoff_y=cutoff_y,
            )

    return new_urls


def suggestion_boundary_visible(page):
    viewport_height = page.viewport_size["height"] if page.viewport_size else page.evaluate("window.innerHeight")

    for text in SUGGESTION_BOUNDARY_TEXTS:
        locator = page.get_by_text(text, exact=True)
        if locator.count() == 0:
            continue

        try:
            box = locator.first.bounding_box()
        except Exception:
            continue

        if box and box["y"] <= viewport_height * 1.5:
            return True

    return False


def runtime_exceeded(start_time):
    return time.monotonic() - start_time > MAX_RUNTIME_SECONDS


def current_cutoff_y(page, cutoff_lock_y):
    if TEST_MODE_ALL_BOARD_PINS:
        return None

    # Once locked the cutoff must never move: the boundary headings drift down
    # as more content lazy-loads, and a live value would let recommendations in.
    if cutoff_lock_y is not None:
        return cutoff_lock_y

    return recommendation_cutoff_y(page)


def maybe_lock_cutoff(page, boundary_seen, cutoff_lock_y):
    if boundary_seen and cutoff_lock_y is not None:
        return boundary_seen, cutoff_lock_y

    if not suggestion_boundary_visible(page):
        return boundary_seen, cutoff_lock_y

    if cutoff_lock_y is None:
        cutoff_lock_y = recommendation_cutoff_y(page)
        print(f"Locked board cutoff at y={cutoff_lock_y}")

    return True, cutoff_lock_y


def click_first_visible(page, labels, timeout_ms=2000):
    for label in labels:
        locator = page.get_by_role("button", name=label)
        if locator.count() == 0:
            locator = page.get_by_role("link", name=label)
        if locator.count() == 0:
            locator = page.get_by_text(label, exact=True)

        if locator.count() == 0:
            continue

        try:
            locator.first.click(timeout=timeout_ms)
            return True
        except Exception:
            continue

    return False


def open_or_create_album(page, album_name):
    click_first_visible(page, ["Albums", "Album"])

    # Try opening an existing album first.
    if click_first_visible(page, [album_name], timeout_ms=1500):
        return True

    # Best-effort album creation flow; UI labels can vary by region.
    if not click_first_visible(page, ["New Album", "Create Album", "New", "+"], timeout_ms=1500):
        return False

    name_input = page.locator("input[type='text']")
    if name_input.count() == 0:
        return False

    try:
        name_input.first.fill(album_name)
    except Exception:
        return False

    if not click_first_visible(page, ["Create", "Done", "OK"], timeout_ms=2000):
        return False

    return True


def upload_files_to_icloud_page(page, file_paths):
    # No click-based triggers here; avoid opening OS file chooser windows.
    # We only set files directly on existing file inputs.
    deadline = time.monotonic() + ICLOUD_FILE_INPUT_WAIT_SECONDS
    while time.monotonic() < deadline:
        scopes = list(page.frames)
        for scope in scopes:
            file_input = scope.locator("input[type='file']")
            if file_input.count() == 0:
                continue
            for i in range(file_input.count()):
                try:
                    file_input.nth(i).set_input_files(file_paths)
                    return True
                except Exception:
                    continue

        time.sleep(1)

    return False


def upload_activity_detected(frame):
    return frame.evaluate(
        """
        () => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };

            const progressLike = document.querySelectorAll(
                "progress, [role='progressbar'], [class*='progress'], [class*='spinner'], [aria-busy='true']"
            );
            for (const el of progressLike) {
                if (isVisible(el)) return true;
            }

            const statusSelectors = [
                "[aria-live]",
                "[class*='status']",
                "[class*='upload']",
                "[data-testid*='upload']",
                "[id*='upload']",
            ];
            const uploadWords = /(uploading|upload|remaining|left|processing|preparing)/i;
            for (const selector of statusSelectors) {
                const nodes = document.querySelectorAll(selector);
                for (const node of nodes) {
                    if (!isVisible(node)) continue;
                    const text = (node.innerText || node.textContent || '').trim();
                    if (text && uploadWords.test(text)) return true;
                }
            }

            return false;
        }
        """
    )


def accept_icloud_duplicate_prompt(page):
    for frame in page.frames:
        try:
            clicked_label = frame.evaluate(
                """
                () => {
                    const bodyText = (document.body && document.body.innerText ? document.body.innerText : '').toLowerCase();
                    const duplicateHints = [
                        'duplicate',
                        'already exists',
                        'already in',
                        'duplicaat',
                        'bestaat al'
                    ];
                    if (!duplicateHints.some((hint) => bodyText.includes(hint))) {
                        return '';
                    }

                    const labels = [
                        'accept',
                        'ok',
                        'continue',
                        'keep both',
                        'replace',
                        'beide bewaren',
                        'accepteren',
                        'oké',
                        'doorgaan',
                        'bewaar beide',
                        'vervang'
                    ];
                    const blockedWords = [
                        'upload',
                        'add',
                        'import',
                        'file',
                        'bestandskiezer',
                        'bestand kiezen'
                    ];
                    const nodes = Array.from(document.querySelectorAll("button, [role='button'], ui-button"));
                    const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };

                    const buttonText = (el) => ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase().trim();

                    for (const label of labels) {
                        const node = nodes.find((el) => {
                            if (!isVisible(el)) return false;
                            const text = buttonText(el);
                            if (blockedWords.some((word) => text.includes(word))) return false;
                            return text === label || text.startsWith(label + ' ');
                        });
                        if (node) {
                            node.click();
                            return label;
                        }
                    }

                    // Safe fallback: pick a visible "primary" dialog button that is not upload/import related.
                    const dialog = document.querySelector("[role='dialog'], .modal, .dialog");
                    if (dialog) {
                        const dialogButtons = Array.from(dialog.querySelectorAll("button, [role='button'], ui-button"));
                        const primary = dialogButtons.find((el) => {
                            if (!isVisible(el)) return false;
                            const text = buttonText(el);
                            if (!text) return false;
                            if (blockedWords.some((word) => text.includes(word))) return false;
                            const cls = (el.getAttribute('class') || '').toLowerCase();
                            return cls.includes('primary') || cls.includes('default') || cls.includes('confirm');
                        });
                        if (primary) {
                            primary.click();
                            return 'dialog-primary';
                        }
                    }

                    return '';
                }
                """
            )
            if clicked_label:
                print(f"Accepted iCloud duplicate prompt using '{clicked_label}'.")
                return True
        except Exception:
            continue

    return False


def wait_for_icloud_upload_completion(page):
    start = time.monotonic()
    deadline = start + ICLOUD_UPLOAD_WAIT_TIMEOUT_SECONDS
    stable_polls = 0
    saw_activity = False

    while time.monotonic() < deadline:
        accept_icloud_duplicate_prompt(page)

        activity = False
        for frame in page.frames:
            try:
                if upload_activity_detected(frame):
                    activity = True
                    break
            except Exception:
                continue

        if activity:
            saw_activity = True
            stable_polls = 0
            print("iCloud upload still in progress...")
        else:
            stable_polls += 1
            # iCloud takes a moment to render its first progress indicator. Without
            # this grace window the very first polls look idle, the browser closes
            # and the transfer is killed before it ever started.
            grace_over = time.monotonic() - start >= ICLOUD_UPLOAD_START_GRACE_SECONDS
            if stable_polls >= ICLOUD_UPLOAD_STABLE_POLLS and (saw_activity or grace_over):
                if saw_activity:
                    print("iCloud upload appears finished.")
                else:
                    print("No active iCloud upload indicator detected; continuing.")
                return

        time.sleep(ICLOUD_UPLOAD_POLL_SECONDS)

    print("Timed out waiting for iCloud upload completion.")
    input("Confirm uploads are done, then press Enter to close iCloud browser...")


def upload_to_icloud_album(playwright, file_paths):
    if not file_paths:
        print("No files to upload to iCloud.")
        return

    print(f"Preparing iCloud upload for {len(file_paths)} files...")

    browser = playwright.chromium.launch(headless=False)
    context_kwargs = {}
    state_path = Path(ICLOUD_STORAGE_STATE_FILE)
    if state_path.exists():
        context_kwargs["storage_state"] = str(state_path)

    context = browser.new_context(**context_kwargs)
    page = context.new_page()

    try:
        page.goto(ICLOUD_ALBUM_URL or ICLOUD_PHOTOS_URL, wait_until="domcontentloaded")

        try:
            page.wait_for_url("**icloud.com/photos/**", timeout=15000)
        except Exception:
            pass

        print("If prompted, sign in to iCloud in the opened browser.")
        input("After iCloud Photos is fully loaded, press Enter to continue...")

        try:
            context.storage_state(path=str(state_path))
        except Exception:
            pass

        # Ensure we're on the album view the user shared; fallback only if navigation fails.
        if "/#/a," not in page.url:
            print("Not in an album view; looking the album up by name.")
            album_ready = open_or_create_album(page, ICLOUD_ALBUM_NAME)
            if not album_ready:
                print("Could not reliably open/create the iCloud album; uploading to library view instead.")

        if not upload_files_to_icloud_page(page, file_paths):
            print("Warning: Could not trigger iCloud file upload automatically.")
            print("This run avoids clicking Upload automatically to prevent OS file-dialog popups.")
            print("If needed, upload manually in iCloud Photos.")
            return

        accept_icloud_duplicate_prompt(page)

        print("Upload started in iCloud Photos. Waiting for upload completion...")
        wait_for_icloud_upload_completion(page)
    finally:
        context.close()
        browser.close()


with sync_playwright() as p:
    clear_downloads_dir("downloads")
    init_image_log()

    browser = None
    page = None
    launch_errors = []

    for engine_name in SCRAPER_BROWSER_ENGINES:
        try:
            browser_type = getattr(p, engine_name)
            browser = browser_type.launch(headless=False)
            page = browser.new_page()
            open_board_with_retries(page, BOARD_URL)
            print(f"Using {engine_name} for Pinterest scraping.")
            break
        except Exception as engine_error:
            launch_errors.append(f"{engine_name}: {engine_error}")
            print(f"{engine_name} failed for board navigation; trying next browser engine.")
            if browser:
                browser.close()
            browser = None
            page = None

    if not page:
        raise RuntimeError(
            "Could not start scraping in any browser engine. "
            f"Errors: {' | '.join(launch_errors)}"
        )

    downloads = {}
    seen_image_urls = set()
    downloaded_files = []

    try:
        expected_pin_count = extract_expected_pin_count(page)
        if expected_pin_count:
            print(f"Detected board pin target: {expected_pin_count}")

        stagnant_rounds = 0
        start_time = time.monotonic()
        boundary_seen = False
        cutoff_lock_y = None

        for iteration in range(MAX_SCROLLS):
            if runtime_exceeded(start_time):
                print("Stopping because the runtime safety limit was reached.")
                break

            cutoff_y = current_cutoff_y(page, cutoff_lock_y)
            new_urls = collect_image_urls(
                page,
                downloads,
                seen_image_urls,
                cutoff_y=cutoff_y,
                iteration=iteration,
            )

            boundary_seen, cutoff_lock_y = maybe_lock_cutoff(page, boundary_seen, cutoff_lock_y)

            remaining_scroll = SCROLL_STEP
            while remaining_scroll > 0:
                if runtime_exceeded(start_time):
                    break

                jump = min(SCROLL_INCREMENT, remaining_scroll)
                page.evaluate(f"window.scrollBy(0, {jump})")
                remaining_scroll -= jump
                time.sleep(SCROLL_SETTLE_SECONDS)

                try:
                    page.wait_for_load_state("networkidle", timeout=1500)
                except Exception:
                    pass

                boundary_seen, cutoff_lock_y = maybe_lock_cutoff(page, boundary_seen, cutoff_lock_y)

                cutoff_y = current_cutoff_y(page, cutoff_lock_y)

                new_urls += collect_image_urls(
                    page,
                    downloads,
                    seen_image_urls,
                    cutoff_y=cutoff_y,
                    iteration=iteration,
                )

            if runtime_exceeded(start_time):
                print("Stopping because the runtime safety limit was reached.")
                break

            # One extra settle phase helps capture late-rendered masonry tiles.
            for _ in range(SCROLL_SETTLE_CHECKS):
                time.sleep(SCROLL_SETTLE_SECONDS)
                cutoff_y = current_cutoff_y(page, cutoff_lock_y)
                new_urls += collect_image_urls(
                    page,
                    downloads,
                    seen_image_urls,
                    cutoff_y=cutoff_y,
                    iteration=iteration,
                )

            if expected_pin_count and len(downloads) >= expected_pin_count:
                print("Reached detected board pin count target.")
                break

            if new_urls == 0:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            print(f"Found {len(downloads)} images so far")

            if stagnant_rounds >= STALL_LIMIT:
                if (
                        TEST_MODE_ALL_BOARD_PINS
                        and expected_pin_count
                        and len(downloads) < expected_pin_count
                ):
                    print("Stalled before board target; continuing in all-board test mode.")
                    stagnant_rounds = 0
                    continue
                if boundary_seen:
                    print("Reached suggestions section and no new board pins were found.")
                break

        print(f"Final image count: {len(downloads)}")

        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})

        for pin_key, candidates in downloads.items():
            saved = False
            last_error = "no candidate URLs"
            for url in candidates:
                try:
                    # Numbered by files actually written, so a pin that fails
                    # every candidate leaves no gap in the img_<n> sequence.
                    output_path = download_image(session, url, len(downloaded_files))
                except Exception as download_error:
                    last_error = download_error
                    continue

                downloaded_files.append(str(output_path))
                saved = True
                break

            if not saved:
                print(f"Failed all candidates for {pin_key}: {last_error}")
    finally:
        browser.close()

    if ICLOUD_UPLOAD_ENABLED:
        upload_to_icloud_album(p, downloaded_files)
