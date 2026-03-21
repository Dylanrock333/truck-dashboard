"""
Facebook Marketplace scraper  —  Mack & Freightliner trucks across Texas
=========================================================================
Uses FB's internal GraphQL API for structured JSON responses — no HTML parsing.
Requires a valid FB session (one-time manual login via --login).

USAGE:
  python3 fb_marketplace_scraper.py --login   # open browser, log in, save cookies
  python3 fb_marketplace_scraper.py           # fetch listings, print JSON to stdout

REQUIREMENTS:
  pip install playwright
  playwright install chromium
"""

import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.db import merge_and_save, export_listings

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

_SCRAPE_START: float = 0.0


def log(msg: str, level: str = "INFO") -> None:
    """Timestamped log to stderr. level: INFO | WARN | ERROR | STAT"""
    elapsed = time.monotonic() - _SCRAPE_START if _SCRAPE_START else 0
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "  ", "WARN": "! ", "ERROR": "X ", "STAT": "* "}.get(level, "  ")
    m, s = divmod(int(elapsed), 60)
    print(f"[fb {ts} +{m:02d}:{s:02d}] {prefix}{msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Search queries  —  edit this list to add/remove searches
# ---------------------------------------------------------------------------

SEARCH_TERMS = [
    # Mack models
    "Mack Pinnacle",
    "Mack Anthem",
    "Mack CXU613",
    "Mack CHU613",
    # Freightliner models
    "Freightliner Cascadia",
    "Freightliner Columbia",
    "Freightliner 122SD",
]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 35-mile radius searches centered on major Texas cities
SEARCH_LOCATIONS = [
    {"name": "Houston",      "lat": 29.7604, "lng": -95.3698,  "location_id": "houston"},
    {"name": "Austin",       "lat": 30.2672, "lng": -97.7431,  "location_id": "austin"},
    {"name": "San Antonio",  "lat": 29.4241, "lng": -98.4936,  "location_id": "sanantonio"},
    {"name": "Fort Worth",   "lat": 32.7555, "lng": -97.3308,  "location_id": "fortworth"},
    {"name": "Laredo",       "lat": 27.5306, "lng": -99.4803,  "location_id": "laredo"},
    {"name": "El Paso",      "lat": 31.7619, "lng": -106.4850, "location_id": "elpaso"},
    {"name": "Odessa",       "lat": 31.8457, "lng": -102.3676, "location_id": "odessa"},
    {"name": "Lubbock",      "lat": 33.5779, "lng": -101.8552, "location_id": "lubbock"},
]

RADIUS_METERS = 56327   # 35 miles
MAX_PRICE     = 37000
MIN_PRICE     = 8000
MAX_PAGES     = 8       # max pages to fetch per search-term × city (24 listings/page)

DOC_ID        = "26543821838555115"   # CometMarketplaceSearchContentContainerQuery
COOKIE_FILE   = Path(__file__).parent / "fb_cookies.json"

# ---------------------------------------------------------------------------
# Title / quality filters
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r'\b(20\d{2})\b')
_MODEL_KEYWORDS = [
    "mack", "pinnacle", "anthem", "cxu613", "chu613",
    "freightliner", "cascadia", "columbia", "122sd",
]
YEAR_MIN = 2010
YEAR_MAX = 2022


def _filter_reason(listing: dict) -> str | None:
    """
    Return the rejection reason string, or None if the listing passes all filters.
    Filters applied (in order):
      1. Title must contain a year 2010-2020
      2. Title must contain a known truck model keyword
      3. Price must be present (non-None)
    """
    title = (listing.get("name") or "").lower()
    price = listing.get("price")

    years = [int(y) for y in _YEAR_RE.findall(title)]
    if not years:
        return "no year in title"
    if not any(YEAR_MIN <= y <= YEAR_MAX for y in years):
        return f"year out of range ({', '.join(str(y) for y in years)})"
    if not any(kw in title for kw in _MODEL_KEYWORDS):
        return "no model keyword in title"
    if not price:
        return "no price"
    return None  # passes


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

async def login_and_save_cookies() -> None:
    """Open a visible browser, let the user log into Facebook, then save cookies."""
    log("Opening browser — log into Facebook, then press Enter here.")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto("https://www.facebook.com/login", wait_until="load")

        log("Waiting for you to log in … press Enter when done.")
        await asyncio.get_event_loop().run_in_executor(None, input)

        cookies = await context.cookies()
        COOKIE_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        log(f"Saved {len(cookies)} cookies to {COOKIE_FILE}")
        await browser.close()


# ---------------------------------------------------------------------------
# Token extraction helpers
# ---------------------------------------------------------------------------

def _extract_token(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


async def fetch_csrf_tokens(page) -> dict:
    """
    Load facebook.com/marketplace/ and extract CSRF tokens embedded in the page HTML.
    Also reads the user ID from the c_user cookie.
    Returns dict with keys: fb_dtsg, lsd, jazoest, user_id  (any may be None if not found).
    """
    await page.goto("https://www.facebook.com/marketplace/", wait_until="load", timeout=60_000)
    html = await page.content()

    fb_dtsg  = _extract_token(r'"DTSGInitialData"[^}]*"token":"([^"]+)"', html)
    lsd      = _extract_token(r'"LSD"[^}]*"token":"([^"]+)"', html)
    jazoest  = _extract_token(r'jazoest=(\d+)', html)

    # Fallback patterns
    if not fb_dtsg:
        fb_dtsg = _extract_token(r'name="fb_dtsg" value="([^"]+)"', html)
    if not lsd:
        lsd = _extract_token(r'name="lsd" value="([^"]+)"', html)

    # User ID from c_user cookie (needed for av/__user fields)
    cookies = await page.context.cookies()
    user_id = next((c["value"] for c in cookies if c["name"] == "c_user"), "0")

    return {"fb_dtsg": fb_dtsg, "lsd": lsd, "jazoest": jazoest, "user_id": user_id}


# ---------------------------------------------------------------------------
# GraphQL search
# ---------------------------------------------------------------------------

DEBUG = "--debug" in sys.argv

FRIENDLY_NAME = "CometMarketplaceSearchContentContainerQuery"


async def graphql_search(
    page,
    tokens: dict,
    search_text: str,
    lat: float,
    lng: float,
    location_id: str,
    cursor: str | None = None,
) -> dict:
    """
    POST a single GraphQL request and return the parsed JSON response.
    FB prepends 'for (;;);' to all API responses — we strip it first.
    """
    radius_km = round(RADIUS_METERS / 1000)
    variables = {
        "buyLocation": {"latitude": lat, "longitude": lng},
        "contextual_data": None,
        "count": 24,
        "cursor": cursor,
        "params": {
            "bqf": {
                "callsite": "COMMERCE_MKTPLACE_WWW",
                "query": search_text,
            },
            "browse_request_params": {
                "commerce_enable_local_pickup": True,
                "commerce_enable_shipping": True,
                "commerce_search_and_rp_available": True,
                "commerce_search_and_rp_category_id": [],
                "commerce_search_and_rp_condition": None,
                "commerce_search_and_rp_ctime_days": None,
                "filter_location_latitude": lat,
                "filter_location_longitude": lng,
                "filter_price_lower_bound": MIN_PRICE * 100,   # FB uses cents
                "filter_price_upper_bound": MAX_PRICE * 100,
                "filter_radius_km": radius_km,
            },
            "custom_request_params": {
                "browse_context": None,
                "contextual_filters": [],
                "referral_code": None,
                "referral_ui_component": None,
                "saved_search_strid": None,
                "search_vertical": "C2C",
                "seo_url": None,
                "serp_landing_settings": {"virtual_category_id": ""},
                "surface": "SEARCH",
                "virtual_contextual_filters": [],
            },
        },
        "savedSearchID": None,
        "savedSearchQuery": search_text,
        "scale": 2,
        "searchPopularSearchesParams": {
            "location_id": location_id,
            "query": search_text,
        },
        "shouldIncludePopularSearches": False,
        "topicPageParams": {"location_id": location_id, "url": None},
    }

    user_id = tokens.get("user_id", "0")
    post_data = {
        "av": user_id,
        "__user": user_id,
        "__a": "1",
        "dpr": "2",
        "__ccg": "EXCELLENT",
        "fb_dtsg": tokens.get("fb_dtsg", ""),
        "jazoest": tokens.get("jazoest", ""),
        "lsd": tokens.get("lsd", ""),
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": FRIENDLY_NAME,
        "variables": json.dumps(variables),
        "server_timestamps": "true",
        "doc_id": DOC_ID,
    }

    result = await page.evaluate(
        """
        async ([url, data]) => {
            const params = new URLSearchParams(data);
            const resp = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Accept': '*/*',
                    'X-FB-Friendly-Name': data.fb_api_req_friendly_name,
                    'X-FB-LSD': data.lsd || '',
                },
                body: params.toString(),
                credentials: 'include',
            });
            return await resp.text();
        }
        """,
        ["https://www.facebook.com/api/graphql/", post_data],
    )

    text = result.lstrip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]

    try:
        # Use raw_decode to grab only the first JSON object — FB sometimes
        # concatenates a second blob (ad unit) which breaks json.loads
        parsed, _ = json.JSONDecoder().raw_decode(text)
        if DEBUG:
            log("RAW RESPONSE (first 3000 chars):")
            print(json.dumps(parsed, indent=2)[:3000], file=sys.stderr)
        return parsed
    except json.JSONDecodeError as exc:
        log(f"Failed to parse response for '{search_text}': {exc}", "ERROR")
        log(f"Response preview: {text[:400]}", "ERROR")
        return {}


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_listings_from_response(data: dict, search_term: str) -> tuple[list[dict], str | None]:
    """
    Walk the GraphQL response tree and return (listings, next_cursor).
    """
    listings: list[dict] = []
    next_cursor: str | None = None

    try:
        root = data.get("data", {}).get("marketplace_search", {})
        if not root:
            root = (
                data.get("data", {})
                .get("viewer", {})
                .get("marketplace_feed_stories", {})
            )

        edges = root.get("feed_units", {}).get("edges") or root.get("edges") or []
        page_info = (
            root.get("feed_units", {}).get("page_info")
            or root.get("page_info")
            or {}
        )

        next_cursor = page_info.get("end_cursor") if page_info.get("has_next_page") else None

        for edge in edges:
            node = edge.get("node", {})
            listing_node = (
                node.get("listing")
                or node.get("marketplace_listing_renderable_target")
                or node
            )
            if not listing_node:
                continue

            lid = listing_node.get("id") or listing_node.get("listing_id")
            if not lid:
                continue

            title = (
                listing_node.get("marketplace_listing_title")
                or listing_node.get("name")
                or ""
            )

            price_info   = listing_node.get("listing_price") or {}
            price_amount = price_info.get("amount")
            price_text   = price_info.get("formatted_amount") or (
                f"${int(price_amount):,}" if price_amount else None
            )

            location_info = listing_node.get("location") or {}
            reverse_geo   = location_info.get("reverse_geocode") or {}
            location_str  = (
                reverse_geo.get("city_page", {}).get("display_name")
                or f"{reverse_geo.get('city', '')}, {reverse_geo.get('state', '')}".strip(", ")
                or None
            )

            primary_photo = listing_node.get("primary_listing_photo") or {}
            image = (
                primary_photo.get("image", {}).get("uri")
                or primary_photo.get("uri")
                or None
            )

            listings.append({
                "id":                  str(lid),
                "source":              "facebook_marketplace",
                "name":                title,
                "url":                 f"https://www.facebook.com/marketplace/item/{lid}/",
                "image_url":           image,
                "price":               price_text,
                "mileage":             None,
                "engine_manufacturer": None,
                "location":            location_str,
                "seller":              None,
                "vin":                 None,
                "engine_model":        None,
                "transmission_model":  None,
                "search_term":         search_term,
                "search_city":         None,  # set later in search_term_all_pages
            })

    except Exception as exc:
        log(f"Error parsing response: {exc}", "ERROR")

    return listings, next_cursor


# ---------------------------------------------------------------------------
# Paginated search per term × location
# ---------------------------------------------------------------------------

async def search_term_all_pages(
    page,
    tokens: dict,
    search_term: str,
    lat: float,
    lng: float,
    location_id: str,
    city_name: str,
    seen_ids: set[str],
    reject_counts: dict[str, int],
) -> list[dict]:
    """
    Fetch pages for one search term + city.
    Applies filters inline; stops early if a full page yields zero matching results.
    Returns only new listings that pass all filters.
    """
    kept: list[dict] = []
    cursor: str | None = None
    page_num = 0
    t0 = time.monotonic()

    while page_num < MAX_PAGES:
        page_num += 1

        data = await graphql_search(page, tokens, search_term, lat, lng, location_id, cursor)
        if not data:
            log(f"  Empty response — stopping '{search_term}' @ {city_name}", "WARN")
            break

        batch, cursor = _extract_listings_from_response(data, search_term)
        page_kept = 0
        page_dupes = 0
        page_filtered = 0

        for item in batch:
            if item["id"] in seen_ids:
                page_dupes += 1
                continue
            seen_ids.add(item["id"])

            reason = _filter_reason(item)
            if reason:
                reject_counts[reason] = reject_counts.get(reason, 0) + 1
                page_filtered += 1
                continue

            item["search_city"] = city_name
            kept.append(item)
            page_kept += 1

        elapsed = time.monotonic() - t0
        log(
            f"  '{search_term}' @ {city_name} p{page_num}: "
            f"{len(batch)} raw | {page_kept} kept | {page_dupes} dupes | {page_filtered} filtered "
            f"({elapsed:.1f}s)"
        )

        # Early stop: whole page was filtered/dupes — no point paginating further
        if page_kept == 0 and len(batch) > 0:
            log(f"  Zero matches on page {page_num} — stopping early for '{search_term}' @ {city_name}")
            break

        if not cursor or not batch:
            break

        await asyncio.sleep(1.5)

    return kept


# ---------------------------------------------------------------------------
# Main scrape flow
# ---------------------------------------------------------------------------

async def scrape() -> list[dict]:
    global _SCRAPE_START
    _SCRAPE_START = time.monotonic()

    if not COOKIE_FILE.exists():
        log(
            f"Cookie file not found: {COOKIE_FILE}\n"
            "Run with --login first:\n"
            "  python3 fb_marketplace_scraper.py --login",
            "ERROR",
        )
        sys.exit(1)

    cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))

    log(f"Starting scrape — {len(SEARCH_LOCATIONS)} cities × {len(SEARCH_TERMS)} terms | "
        f"radius={RADIUS_METERS // 1000} km ({RADIUS_METERS // 1609} mi) | "
        f"price ${MIN_PRICE:,}–${MAX_PRICE:,} | "
        f"year {YEAR_MIN}–{YEAR_MAX} | max {MAX_PAGES} pages/search")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await context.add_cookies(cookies)
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()

        log("Fetching CSRF tokens from marketplace homepage …")
        tokens = await fetch_csrf_tokens(page)

        missing = [k for k, v in tokens.items() if not v]
        if missing:
            log(f"Could not extract tokens: {missing} — session may have expired", "WARN")
            if "fb_dtsg" in missing and "lsd" in missing:
                sys.exit(1)

        log(
            f"Tokens: fb_dtsg={'OK' if tokens.get('fb_dtsg') else 'MISSING'}  "
            f"lsd={'OK' if tokens.get('lsd') else 'MISSING'}  "
            f"jazoest={'OK' if tokens.get('jazoest') else 'MISSING'}  "
            f"user_id={tokens.get('user_id', '?')}"
        )

        all_listings: list[dict] = []
        seen_ids: set[str] = set()
        reject_counts: dict[str, int] = {}

        for loc in SEARCH_LOCATIONS:
            loc_start = time.monotonic()
            loc_before = len(all_listings)
            log(f"=== {loc['name']} (35-mile radius) ===", "STAT")

            for term in SEARCH_TERMS:
                term_listings = await search_term_all_pages(
                    page, tokens, term,
                    loc["lat"], loc["lng"], loc["location_id"],
                    loc["name"],
                    seen_ids,
                    reject_counts,
                )
                all_listings.extend(term_listings)
                await asyncio.sleep(1.0)

            loc_elapsed = time.monotonic() - loc_start
            loc_found = len(all_listings) - loc_before
            log(
                f"=== {loc['name']} done: +{loc_found} listings | "
                f"total so far: {len(all_listings)} | {loc_elapsed:.0f}s ===",
                "STAT",
            )

        await browser.close()

    total_elapsed = time.monotonic() - _SCRAPE_START
    m, s = divmod(int(total_elapsed), 60)

    log(f"--- FILTER REJECTION BREAKDOWN ---", "STAT")
    for reason, count in sorted(reject_counts.items(), key=lambda x: -x[1]):
        log(f"  {count:>5}  {reason}", "STAT")

    log(
        f"--- DONE: {len(all_listings)} listings | "
        f"unique IDs seen: {len(seen_ids)} | "
        f"total runtime: {m}m {s}s ---",
        "STAT",
    )
    return all_listings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def find_docid() -> None:
    """
    Open a visible browser, load cookies, navigate to Marketplace, intercept
    a real GraphQL search request, and print the current doc_id.
    """
    if not COOKIE_FILE.exists():
        log("Run --login first.", "ERROR")
        sys.exit(1)

    cookies = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    found: dict = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        await context.add_cookies(cookies)

        page = await context.new_page()

        async def handle_request(request):
            if "/api/graphql" in request.url and request.method == "POST":
                try:
                    body = request.post_data or ""
                    fname_m = re.search(r"fb_api_req_friendly_name=([^&]+)", body)
                    friendly_name = fname_m.group(1) if fname_m else ""
                    doc_m = re.search(r"doc_id=(\d+)", body)
                    doc_id = doc_m.group(1) if doc_m else ""
                    if friendly_name in (
                        "CometMarketplaceSearchContentContainerQuery",
                        "CometMarketplaceSearchRootQuery",
                    ):
                        log(f"=== {friendly_name} (doc_id={doc_id}) ===")
                        from urllib.parse import unquote_plus
                        for part in body.split("&"):
                            print(f"  {unquote_plus(part)}", file=sys.stderr)
                        if not found.get("doc_id"):
                            found["doc_id"] = doc_id
                            found["friendly_name"] = friendly_name
                except Exception as e:
                    log(f"intercept error: {e}", "ERROR")

        page.on("request", handle_request)

        log("Opening Marketplace — type anything in the search box, then come back here.")
        await page.goto("https://www.facebook.com/marketplace/", wait_until="load")

        for _ in range(60):
            if found.get("doc_id"):
                break
            await asyncio.sleep(1)

        await browser.close()

    if found.get("doc_id"):
        log(f"Found doc_id: {found['doc_id']}")
        log(f"friendly_name: {found['friendly_name']}")
        log(f'Update DOC_ID in the script:  DOC_ID = "{found["doc_id"]}"')
    else:
        log("No graphql search request captured. Did you type in the search box?", "WARN")


async def main() -> None:
    args = sys.argv[1:]

    if "--login" in args:
        await login_and_save_cookies()
        return

    if "--find-docid" in args:
        await find_docid()
        return

    today = datetime.now().strftime("%Y-%m-%d")
    args = sys.argv[1:]

    fresh = await scrape()
    stats = merge_and_save(fresh, "facebook_marketplace", today)

    log(
        f"Done — {stats['total']} total | {stats['new']} new | "
        f"{stats['reseen']} reseen | {stats['price_changes']} price changes",
        "STAT",
    )

    if "--output" in args:
        idx = args.index("--output")
        if idx + 1 < len(args):
            output_file = args[idx + 1]
            exported = export_listings("facebook_marketplace")
            Path(output_file).write_text(
                json.dumps({"meta": {"last_run": today, **stats}, "listings": exported}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log(f"Exported {len(exported)} listings to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
