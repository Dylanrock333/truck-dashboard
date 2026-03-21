"""
Quick DB summary — run anytime to see what's in the database.

Usage:
  python3 scripts/check_db.py
  python3 scripts/check_db.py --listings        # show all active listings
  python3 scripts/check_db.py --source truck_paper
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "truck_listings.db"


def connect():
    if not DB_PATH.exists():
        print("No database found yet. Run a scraper first.")
        sys.exit(0)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def main():
    args = sys.argv[1:]
    source_filter = None
    if "--source" in args:
        idx = args.index("--source")
        if idx + 1 < len(args):
            source_filter = args[idx + 1]

    conn = connect()

    # --- Scrape runs ---
    print("\n=== SCRAPE RUNS ===")
    runs = conn.execute(
        "SELECT * FROM scrape_runs ORDER BY run_date DESC LIMIT 20"
    ).fetchall()
    if runs:
        print(f"{'Source':<24} {'Date':<12} {'Total':>6} {'New':>6} {'Reseen':>7} {'Price Δ':>8}")
        print("-" * 70)
        for r in runs:
            print(f"{r['source']:<24} {r['run_date']:<12} {r['total']:>6} {r['new']:>6} {r['reseen']:>7} {r['price_changes']:>8}")
    else:
        print("  No runs yet.")

    # --- Listings summary per source ---
    print("\n=== LISTINGS SUMMARY ===")
    rows = conn.execute("""
        SELECT source,
               COUNT(*) as total,
               COUNT(CASE WHEN price IS NOT NULL THEN 1 END) as with_price,
               COUNT(CASE WHEN vin IS NOT NULL THEN 1 END) as with_vin,
               COUNT(CASE WHEN mileage IS NOT NULL THEN 1 END) as with_mileage,
               MIN(first_seen) as earliest,
               MAX(last_seen) as latest_seen
        FROM listings
        GROUP BY source
    """).fetchall()
    for r in rows:
        print(f"\n  {r['source']}")
        print(f"    total:       {r['total']}")
        print(f"    with price:  {r['with_price']}")
        print(f"    with VIN:    {r['with_vin']}")
        print(f"    with mileage:{r['with_mileage']}")
        print(f"    first seen:  {r['earliest']}")
        print(f"    last seen:   {r['latest_seen']}")

    # --- Price history ---
    ph_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    listings_with_changes = conn.execute(
        "SELECT COUNT(DISTINCT listing_id) FROM price_history GROUP BY listing_id HAVING COUNT(*) > 1"
    ).fetchall()
    print(f"\n=== PRICE HISTORY ===")
    print(f"  total price_history rows:  {ph_count}")
    print(f"  listings with >1 price:    {len(listings_with_changes)}")

    # --- Sample listings (most recent) ---
    if "--listings" in args:
        print("\n=== LISTINGS ===")
        query = "SELECT * FROM listings"
        params = []
        if source_filter:
            query += " WHERE source = ?"
            params.append(source_filter)
        query += " ORDER BY first_seen DESC LIMIT 20"
        listings = conn.execute(query, params).fetchall()
        for l in listings:
            price = l['price'] or 'no price'
            location = l['location'] or 'unknown'
            vin = l['vin'] or '-'
            print(f"  [{l['source'][:2].upper()}] {l['name']} | {price} | {location} | VIN: {vin} | seen: {l['first_seen']} → {l['last_seen']}")

    conn.close()


if __name__ == "__main__":
    main()
