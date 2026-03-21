# Truck Dashboard

Scrapes Mack and Freightliner truck listings from Facebook Marketplace and TruckPaper daily, stores them in SQLite, and tracks price history over time.

---

## What It Does

- Scrapes listings across Texas (FB: 8 cities, TruckPaper: statewide)
- Stores every listing in a SQLite database
- On each daily run: new listings are inserted, existing ones are updated, price changes are logged
- Listings that stop appearing are left as-is — use `last_seen` to detect delisted trucks

---

## Project Structure

```
truck-dashboard/
├── data/
│   └── truck_listings.db       # SQLite database (all listings + price history)
├── scripts/
│   └── check_db.py             # Summary tool — see below
├── services/
│   ├── shared/
│   │   └── db.py               # Shared database layer (merge logic, schema)
│   ├── fb_market/
│   │   ├── fb_marketplace_scraper.py
│   │   └── fb_cookies.json     # FB session (saved after --login)
│   └── truck_paper/
│       └── truck_paper_scraper.py
└── .env                        # Proxy config (optional)
```

---

## Setup

```bash
pip install playwright python-dotenv
playwright install chrome
```

---

## Running the Scrapers

**TruckPaper** — no login needed:
```bash
python3 services/truck_paper/truck_paper_scraper.py
```

**Facebook Marketplace** — first time only, run login to save cookies:
```bash
python3 services/fb_market/fb_marketplace_scraper.py --login
```
Then for all future runs:
```bash
python3 services/fb_market/fb_marketplace_scraper.py
```

**Export a JSON snapshot** (either scraper):
```bash
python3 services/truck_paper/truck_paper_scraper.py --output dump.json
python3 services/fb_market/fb_marketplace_scraper.py --output dump.json
```
This exports all stored listings for that source from the DB into a JSON file.

---

## check_db.py

A read-only summary script — run it anytime to verify a scrape worked or inspect the database.

```bash
python3 scripts/check_db.py
```

**Output includes:**
- **Scrape runs log** — every run with date, total listings, how many were new, reseen, and had price changes
- **Listings summary** — per source: total count, how many have a price, VIN, mileage
- **Price history** — total rows logged and how many listings have had more than one price

```bash
# Also show the 20 most recent listings
python3 scripts/check_db.py --listings

# Filter to one source
python3 scripts/check_db.py --listings --source truck_paper
python3 scripts/check_db.py --listings --source facebook_marketplace
```

---

## Database Schema

**`listings`** — one row per unique listing, updated in place each run

| Column | Description |
|---|---|
| `id` | Source-specific listing ID |
| `source` | `facebook_marketplace` or `truck_paper` |
| `name` | Full listing title |
| `price` | Most recent price |
| `location` | City, State |
| `mileage` | TruckPaper only |
| `engine_manufacturer` | TruckPaper only |
| `engine_model` | TruckPaper only |
| `transmission_model` | TruckPaper only |
| `seller` | TruckPaper only |
| `vin` | TruckPaper only |
| `search_term` | FB only — which search query found it |
| `search_city` | FB only — which city radius found it |
| `first_seen` | Date first scraped (`YYYY-MM-DD`) |
| `last_seen` | Date last seen in a scrape (`YYYY-MM-DD`) |

**`price_history`** — append-only, one row per price change

| Column | Description |
|---|---|
| `listing_id` + `source` | Foreign key to listings |
| `date` | Date of the price (`YYYY-MM-DD`) |
| `price` | Price on that date |

**`scrape_runs`** — one row per scrape run

| Column | Description |
|---|---|
| `source` | Which scraper ran |
| `run_date` | Date of the run |
| `total` | Total listings tracked for this source |
| `new` | Listings inserted this run |
| `reseen` | Listings seen again with no price change |
| `price_changes` | Listings where price differed from last run |

---

## Useful Queries

```sql
-- Active listings (seen on most recent TruckPaper run)
SELECT * FROM listings
WHERE source = 'truck_paper'
  AND last_seen = (SELECT MAX(run_date) FROM scrape_runs WHERE source = 'truck_paper');

-- Likely delisted (not seen in latest run)
SELECT * FROM listings
WHERE source = 'truck_paper'
  AND last_seen < (SELECT MAX(run_date) FROM scrape_runs WHERE source = 'truck_paper');

-- Price history for a specific listing
SELECT date, price FROM price_history
WHERE listing_id = '...' AND source = 'truck_paper'
ORDER BY date;

-- Listings that have had price drops
SELECT l.name, l.location, ph.date, ph.price
FROM price_history ph
JOIN listings l ON l.id = ph.listing_id AND l.source = ph.source
ORDER BY l.id, ph.date;
```

---

## Filters

| Filter | FB Marketplace | TruckPaper |
|---|---|---|
| Makes | Mack, Freightliner | Mack, Freightliner |
| Years | 2010–2022 | 2010–2020 |
| Max price | $37,000 | $37,000 |
| Max mileage | — | 600,000 mi |
| Geography | 8 Texas cities, 35-mile radius each | Texas statewide |
| Condition | Used | Used |

---

## Troubleshooting

**FB session expired** — re-run login:
```bash
python3 services/fb_market/fb_marketplace_scraper.py --login
```

**FB GraphQL doc_id changed** — FB occasionally rotates this ID:
```bash
python3 services/fb_market/fb_marketplace_scraper.py --find-docid
```
Then update `DOC_ID` in `fb_marketplace_scraper.py`.

**TruckPaper bot detection** — delete the browser profile and re-run:
```bash
rm -rf services/truck_paper/browser_profile/
python3 services/truck_paper/truck_paper_scraper.py
```
