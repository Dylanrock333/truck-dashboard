# Truck Dashboard

Streamlit dashboard for browsing and tracking Mack/Freightliner truck listings scraped from Facebook Marketplace and TruckPaper.

---

## What It Does

- Browse active truck listings with filters (source, make, price, mileage, year, location)
- Track price changes — see which listings dropped or raised their price
- View scrape history — charts and stats on how many listings are collected each run
- Shows last scrape date per source so you know how fresh the data is

---

## Project Structure

```
truck-dashboard/
├── dashboard/
│   └── app.py              # Streamlit app (single file)
├── services/
│   └── shared/
│       └── db.py           # Shared DB layer (also used by truck-scraper)
├── requirements.txt
└── .env                    # Set TRUCK_DB_PATH here
```

The database (`truck_listings.db`) lives outside this repo and is shared with the scraper backend. Set its path in `.env`:

```
TRUCK_DB_PATH=/path/to/truck_listings.db
```

---

## Setup

```bash
pip install -r requirements.txt
```

---

## Running

```bash
streamlit run dashboard/app.py --server.port 8282 --server.address 0.0.0.0
```

Access at `http://localhost:8282` (or via Tailscale at `http://<tailscale-ip>:8282`).

---

## Related

- **[truck-scraper](https://gitlab.com/Dylanrock333/truck-scraper)** — scraper backend that populates the database
