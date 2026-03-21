"""
TruckPaper listing scraper  —  truckpaper.com
==============================================
Extracts truck listings (with VIN, engine model, transmission) across all pages.

IMPORTANT — BOT PROTECTION:
  TruckPaper uses Distil Networks bot detection. This script uses a persistent
  browser profile (saved to ./browser_profile/) and opens a real Chrome window.
  - First run: Chrome opens visibly so the site can fingerprint a legitimate session.
  - Subsequent runs: the saved profile usually bypasses the check silently.
  - If you get blocked, delete ./browser_profile/ and re-run.

USAGE:
  python3 scraper.py                          # scrape, print JSON to stdout
  python3 scraper.py --output listings.json  # save to file

REQUIREMENTS:
  pip install playwright
  playwright install chrome
"""

import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, BrowserContext, Page

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.db import merge_and_save, export_listings

load_dotenv(Path(__file__).parent.parent / ".env")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = (
    "https://www.truckpaper.com/listings/search"
    "?Category=16013"
    "&ListingType=For%20Retail"
    "&Manufacturer=FREIGHTLINER%7CMACK"
    "&Year=2010%2A2020"
    "&Mileage=%2A600000"
    "&Condition=USED"
    "&Price=%2A37%2C000"
    "&Country=178"
    "&State=TEXAS"
)

PROFILE_DIR = Path(__file__).parent / "browser_profile"
VIN_CONCURRENCY = 1   # detail pages fetched sequentially to avoid bot detection
PAGE_DELAY = 5        # seconds to wait between page loads and detail page fetches
PAGE_RETRIES = 3      # max retries for a page load before giving up

# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _js_extractor() -> str:
    """Return the JS function string that extracts listings from the current DOM."""
    return """
    () => {
      function extractListings() {
        const cards = document.querySelectorAll('.list-listing-card-wrapper');
        const results = [];
        cards.forEach(card => {
          const item = {};
          // id
          const grid = card.querySelector('.listing-card-grid[data-listing-id]');
          if (grid) item.id = grid.getAttribute('data-listing-id');
          // name + url
          const titleLink = card.querySelector('.list-listing-title-link');
          if (titleLink) {
            item.name = titleLink.innerText.trim();
            const href = titleLink.getAttribute('href');
            item.url = href
              ? 'https://www.truckpaper.com' + href
              : null;
          }
          // first image (src is set by React hydration, so we wait for it before extracting)
          const img =
            card.querySelector("img.listing-main-image[fetchpriority='high']") ||
            card.querySelector('img.listing-main-image');
          if (img) item.image_url = img.getAttribute('src') || null;
          // price
          const priceEl = card.querySelector('.price');
          if (priceEl) {
            item.price = priceEl.innerText
              .trim()
              .replace(/^[A-Z]{2,4}\\s+/, '');   // strip "USD " prefix
          }
          // mileage + engine manufacturer from spec rows
          card.querySelectorAll('.list-spec .spec').forEach(spec => {
            const lbl = spec.querySelector('.spec-label')
              ?.innerText?.trim()?.replace(/:.*/, '') || '';
            const val = spec.querySelector('.spec-value')?.innerText?.trim() || '';
            if (lbl.includes('Mileage'))            item.mileage = val;
            if (lbl.includes('Engine Manufacturer')) item.engine_manufacturer = val;
          });
          // location
          const locEl = card.querySelector('.machine-location');
          if (locEl)
            item.location = locEl.innerText
              .trim()
              .replace(/^Location\\s*:\\s*/i, '');
          // seller
          const sellerLink = card.querySelector('.seller a');
          if (sellerLink) {
            item.seller = sellerLink.innerText.trim();
          } else {
            const sellerEl = card.querySelector('.seller');
            if (sellerEl)
              item.seller = sellerEl.innerText
                .trim()
                .replace(/^Seller\\s*:\\s*/i, '');
          }
          results.push(item);
        });
        return results;
      }
      return JSON.stringify(extractListings());
    }
    """


async def extract_page_listings(page: Page) -> list[dict]:
    """Wait for listing cards to appear then extract all on the current page."""
    try:
        await page.wait_for_selector(".list-listing-card-wrapper", timeout=30_000)
    except Exception as e:
        screenshot_path = Path(__file__).parent / "error_screenshot.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[scraper] Timed out waiting for listings. Screenshot saved to {screenshot_path}", file=sys.stderr)
        raise
    raw = await page.evaluate(_js_extractor())
    return json.loads(raw)


async def get_total_count(page: Page) -> int:
    """Read the '1 - 28 of 162 Listings' counter and return the total integer."""
    try:
        el = await page.query_selector(".list-listings-count")
        if el:
            text = await el.inner_text()
            m = re.search(r"of\s+([\d,]+)", text)
            if m:
                return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# VIN enrichment
# ---------------------------------------------------------------------------

async def fetch_detail_fields(page: Page, url: str) -> dict:
    """
    Navigate to a listing detail page and return a dict with any of:
      vin, engine_model, transmission_model
    Only keys that have a value are included. Returns {} on failure.
    """
    try:
        await page.goto(url, wait_until="load", timeout=30_000)
        await page.wait_for_selector(".detail__specs-label", timeout=10_000)
        raw = await page.evaluate("""
            () => {
              const WANT = {
                'VIN':                   'vin',
                'Engine Model':          'engine_model',
                'Transmission Type':     'transmission_model',
              };
              const result = {};
              document.querySelectorAll('.detail__specs-label').forEach(label => {
                const key = WANT[label.innerText.trim()];
                if (key) {
                  const val = label.nextElementSibling?.innerText?.trim();
                  if (val) result[key] = val;
                }
              });
              // Grab image: og:image meta first, then first real img on page
              const og = document.querySelector('meta[property="og:image"]');
              if (og && og.content) {
                result.image_url = og.content;
              } else {
                const img = Array.from(document.querySelectorAll('img')).find(
                  i => i.src && i.src.startsWith('http') && !i.src.includes('logo') && !i.src.includes('icon') && i.naturalWidth > 100
                );
                if (img) result.image_url = img.src;
              }
              return JSON.stringify(result);
            }
        """)
        return json.loads(raw)
    except Exception:
        return {}


async def enrich_with_detail_fields(context: BrowserContext, listings: list[dict]) -> None:
    """
    Fetch VIN, engine model, and transmission model from each listing's detail
    page, updating each dict in-place. Runs VIN_CONCURRENCY pages in parallel.
    """
    semaphore = asyncio.Semaphore(VIN_CONCURRENCY)
    total = len(listings)

    async def fetch_one(listing: dict, index: int) -> None:
        url = listing.get("url")
        if not url:
            return
        async with semaphore:
            await asyncio.sleep(PAGE_DELAY)
            page = await context.new_page()
            try:
                fields = await fetch_detail_fields(page, url)
                listing.update(fields)
                summary = ", ".join(f"{k}: {v}" for k, v in fields.items()) or "(none)"
                print(
                    f"[detail] {index + 1}/{total}  {listing.get('name', '')}  {summary}",
                    file=sys.stderr,
                )
            finally:
                await page.close()

    await asyncio.gather(*[fetch_one(l, i) for i, l in enumerate(listings)])


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def page_url(page_num: int) -> str:
    """Build the URL for a given page number (1-based)."""
    if page_num <= 1:
        return BASE_URL
    return f"{BASE_URL}&page={page_num}"


async def navigate_and_extract(page: Page, page_num: int) -> list[dict]:
    """Navigate to `page_num` and return its listings, retrying on timeout."""
    url = page_url(page_num)
    last_exc: Exception | None = None
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            return await extract_page_listings(page)
        except Exception as exc:
            last_exc = exc
            if attempt < PAGE_RETRIES:
                delay = PAGE_DELAY * attempt + random.uniform(2, 6)
                print(
                    f"[scraper] Page {page_num} attempt {attempt} failed ({type(exc).__name__}), "
                    f"retrying in {delay:.1f}s …",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
            else:
                print(
                    f"[scraper] Page {page_num} failed after {PAGE_RETRIES} attempts — skipping.",
                    file=sys.stderr,
                )
    if last_exc:
        raise last_exc


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

async def create_context(pw, headless: bool) -> BrowserContext:
    """
    Create a persistent browser context so session cookies are saved between
    runs, which helps avoid bot-detection challenges on subsequent scrapes.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    proxy_host = os.getenv("PROXY_HOST")
    proxy_port = os.getenv("PROXY_PORT")
    proxy_user = os.getenv("PROXY_USER")
    proxy_pass = os.getenv("PROXY_PASS")
    proxy = None
    if proxy_host and proxy_port:
        proxy = {
            "server": f"http://{proxy_host}:{proxy_port}",
            "username": proxy_user,
            "password": proxy_pass,
        }
        print(f"[scraper] Using proxy: {proxy_host}:{proxy_port}", file=sys.stderr)
    else:
        print("[scraper] No proxy configured — connecting directly", file=sys.stderr)

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        channel="chrome",          # use real installed Chrome if available
        proxy=proxy,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    # Spoof navigator.webdriver = undefined to reduce detection signals
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return context


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def scrape_all() -> dict:
    """
    Scrape all pages and return:
      {
        "metadata": { source_url, total_reported, total_scraped, pages },
        "listings":  [ { id, name, url, image_url, price, mileage,
                         engine_manufacturer, location, seller,
                         vin, engine_model, transmission_model }, ... ]
      }
    """
    async with async_playwright() as pw:
        context = await create_context(pw, headless=False)
        page = await context.new_page()

        # Page 1 — also grab the reported total
        print("[scraper] Loading page 1 ...", file=sys.stderr)
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90_000)

        total_count = await get_total_count(page)
        print(f"[scraper] Site reports {total_count} listings", file=sys.stderr)

        listings = await extract_page_listings(page)
        print(f"[scraper] Page 1: {len(listings)} listings", file=sys.stderr)

        # Determine page count
        per_page = len(listings) or 28
        total_pages = (
            (total_count + per_page - 1) // per_page
            if total_count > 0
            else 6
        )

        # Pages 2 … N
        for n in range(2, total_pages + 1):
            await asyncio.sleep(PAGE_DELAY + random.uniform(0, 3))
            print(f"[scraper] Loading page {n}/{total_pages} ...", file=sys.stderr)
            try:
                page_listings = await navigate_and_extract(page, n)
            except Exception as exc:
                print(f"[scraper] Skipping page {n} after repeated failures: {exc}", file=sys.stderr)
                continue
            listings.extend(page_listings)
            print(f"[scraper] Page {n}: {len(page_listings)} listings", file=sys.stderr)

        # Fetch VIN, engine model, and transmission from each detail page
        print(f"[scraper] Fetching detail fields for {len(listings)} listings ...", file=sys.stderr)
        await enrich_with_detail_fields(context, listings)
        found = sum(1 for l in listings if l.get("vin"))
        print(f"[scraper] VINs found: {found}/{len(listings)}", file=sys.stderr)

        await context.close()

    # Normalize: ensure every listing has all fields (None if missing)
    FIELDS = [
        "id", "source", "name", "url", "image_url", "price", "mileage",
        "engine_manufacturer", "location", "seller",
        "vin", "engine_model", "transmission_model",
        "search_term", "search_city", "make", "model",
    ]
    for listing in listings:
        listing.setdefault("source", "truck_paper")
        listing.setdefault("search_term", None)
        listing.setdefault("search_city", None)
        for field in FIELDS:
            listing.setdefault(field, None)
        ordered = {field: listing[field] for field in FIELDS}
        listing.clear()
        listing.update(ordered)

    return {
        "meta": {
            "source_url": BASE_URL,
            "total_reported": total_count,
            "total_scraped": len(listings),
            "pages": total_pages,
        },
        "listings": listings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    args = sys.argv[1:]
    today = datetime.now().strftime("%Y-%m-%d")

    result = await scrape_all()
    stats = merge_and_save(result["listings"], "truck_paper", today)

    print(
        f"\nDone — {stats['total']} total | {stats['new']} new | "
        f"{stats['reseen']} reseen | {stats['price_changes']} price changes",
        file=sys.stderr,
    )

    if "--output" in args:
        idx = args.index("--output")
        if idx + 1 < len(args):
            output_file = args[idx + 1]
            exported = export_listings("truck_paper")
            Path(output_file).write_text(
                json.dumps({"meta": {**result["meta"], **stats}, "listings": exported}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"[scraper] Exported {len(exported)} listings to {output_file}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
