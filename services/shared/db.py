"""
Shared SQLite persistence layer for truck listing scrapers.

DB location: <project_root>/data/truck_listings.db

Tables
------
listings      — one row per unique listing (id + source), updated in place
price_history — append-only log, new row only when price changes
scrape_runs   — one row per scrape run for auditing
"""

import re
import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# Make / model extraction
# ---------------------------------------------------------------------------

# (pattern, canonical_name) — checked in order, first match wins
_FREIGHTLINER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"CASC|CAS\d"), "Cascadia"),   # covers misspellings + "Cas126"
    (re.compile(r"CORONADO"), "Coronado"),
    (re.compile(r"COLUMBIA"), "Columbia"),
    (re.compile(r"CLASSIC"), "Classic"),
    (re.compile(r"CENTURY"), "Century"),
    (re.compile(r"\bFLD\b"), "FLD"),
    (re.compile(r"\bM2\b"), "M2"),
]

_MACK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"PINNACLE|CXU|CHU"), "Pinnacle"),  # CXU/CHU are Pinnacle model codes
    (re.compile(r"ANTHEM|\bAN\d"), "Anthem"),        # AN6xx/AN7xx are Anthem codes
    (re.compile(r"GRANITE"), "Granite"),
    (re.compile(r"VISI"), "Vision"),                 # covers Vision + Visión (accented)
]


def extract_make_model(name: str | None) -> tuple[str | None, str | None]:
    """Return (make, model) extracted from a listing name, or (None, None)."""
    if not name:
        return None, None
    upper = name.upper()
    if "FREIGHTLINER" in upper:
        make = "Freightliner"
        patterns = _FREIGHTLINER_PATTERNS
    elif "MACK" in upper:
        make = "Mack"
        patterns = _MACK_PATTERNS
    else:
        return None, None
    model = next((label for pat, label in patterns if pat.search(upper)), None)
    return make, model

DB_PATH = Path(__file__).parent.parent.parent / "data" / "truck_listings.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id                  TEXT NOT NULL,
                source              TEXT NOT NULL,
                name                TEXT,
                url                 TEXT,
                image_url           TEXT,
                price               TEXT,
                location            TEXT,
                mileage             TEXT,
                engine_manufacturer TEXT,
                engine_model        TEXT,
                transmission_model  TEXT,
                seller              TEXT,
                vin                 TEXT,
                search_term         TEXT,
                search_city         TEXT,
                make                TEXT,
                model               TEXT,
                first_seen          TEXT NOT NULL,
                last_seen           TEXT NOT NULL,
                PRIMARY KEY (id, source)
            );

            CREATE TABLE IF NOT EXISTS price_history (
                row_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id  TEXT NOT NULL,
                source      TEXT NOT NULL,
                date        TEXT NOT NULL,
                price       TEXT,
                FOREIGN KEY (listing_id, source) REFERENCES listings(id, source)
            );

            CREATE TABLE IF NOT EXISTS scrape_runs (
                run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source        TEXT NOT NULL,
                run_date      TEXT NOT NULL,
                total         INTEGER,
                new           INTEGER,
                reseen        INTEGER,
                price_changes INTEGER
            );
        """)
        # Migrate existing DBs that predate the make/model columns
        for col in ("make", "model"):
            try:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        # Backfill make/model for rows where it's null
        rows = conn.execute(
            "SELECT id, source, name FROM listings WHERE make IS NULL AND name IS NOT NULL"
        ).fetchall()
        for row in rows:
            make, model = extract_make_model(row["name"])
            conn.execute(
                "UPDATE listings SET make = ?, model = ? WHERE id = ? AND source = ?",
                (make, model, row["id"], row["source"]),
            )


def merge_and_save(fresh: list[dict], source: str, today: str) -> dict:
    """
    Merge a fresh scrape into the DB.

    - New ID      → INSERT, add initial price_history entry
    - Existing ID → UPDATE last_seen + mutable fields,
                    append to price_history only if price changed
    - Not in fresh → untouched (last_seen stays as-is)

    Returns stats dict: total, new, reseen, price_changes
    """
    init_db()
    new_count = 0
    reseen_count = 0
    price_change_count = 0

    with _connect() as conn:
        for item in fresh:
            lid = item["id"]
            price_today = item.get("price")

            row = conn.execute(
                "SELECT price FROM listings WHERE id = ? AND source = ?",
                (lid, source),
            ).fetchone()

            if row is None:
                make, model = extract_make_model(item.get("name"))
                conn.execute(
                    """
                    INSERT INTO listings (
                        id, source, name, url, image_url, price,
                        location, mileage, engine_manufacturer, engine_model,
                        transmission_model, seller, vin,
                        search_term, search_city, make, model, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lid, source,
                        item.get("name"), item.get("url"), item.get("image_url"),
                        price_today, item.get("location"), item.get("mileage"),
                        item.get("engine_manufacturer"), item.get("engine_model"),
                        item.get("transmission_model"), item.get("seller"), item.get("vin"),
                        item.get("search_term"), item.get("search_city"),
                        make, model, today, today,
                    ),
                )
                if price_today:
                    conn.execute(
                        "INSERT INTO price_history (listing_id, source, date, price) VALUES (?, ?, ?, ?)",
                        (lid, source, today, price_today),
                    )
                new_count += 1

            else:
                existing_price = row["price"]
                make, model = extract_make_model(item.get("name"))
                conn.execute(
                    """
                    UPDATE listings SET
                        last_seen           = ?,
                        name                = COALESCE(?, name),
                        image_url           = COALESCE(?, image_url),
                        location            = COALESCE(?, location),
                        price               = COALESCE(?, price),
                        mileage             = COALESCE(?, mileage),
                        engine_manufacturer = COALESCE(?, engine_manufacturer),
                        engine_model        = COALESCE(?, engine_model),
                        transmission_model  = COALESCE(?, transmission_model),
                        seller              = COALESCE(?, seller),
                        vin                 = COALESCE(?, vin),
                        search_term         = COALESCE(?, search_term),
                        search_city         = COALESCE(?, search_city),
                        make                = COALESCE(make, ?),
                        model               = COALESCE(model, ?)
                    WHERE id = ? AND source = ?
                    """,
                    (
                        today,
                        item.get("name"), item.get("image_url"), item.get("location"),
                        price_today, item.get("mileage"), item.get("engine_manufacturer"),
                        item.get("engine_model"), item.get("transmission_model"),
                        item.get("seller"), item.get("vin"),
                        item.get("search_term"), item.get("search_city"),
                        make, model,
                        lid, source,
                    ),
                )
                if price_today and price_today != existing_price:
                    conn.execute(
                        "INSERT INTO price_history (listing_id, source, date, price) VALUES (?, ?, ?, ?)",
                        (lid, source, today, price_today),
                    )
                    price_change_count += 1
                else:
                    reseen_count += 1

        total = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE source = ?", (source,)
        ).fetchone()[0]

        conn.execute(
            """
            INSERT INTO scrape_runs (source, run_date, total, new, reseen, price_changes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source, today, total, new_count, reseen_count, price_change_count),
        )

    return {
        "total": total,
        "new": new_count,
        "reseen": reseen_count,
        "price_changes": price_change_count,
    }


def export_listings(source: str) -> list[dict]:
    """Return all listings for a source as dicts, sorted by first_seen desc."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE source = ? ORDER BY first_seen DESC",
            (source,),
        ).fetchall()
        return [dict(row) for row in rows]
