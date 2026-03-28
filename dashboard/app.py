import os
import re
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path(os.environ.get("TRUCK_DB_PATH", Path.home() / "data" / "truck_listings.db"))

st.set_page_config(
    page_title="Truck Dashboard",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── helpers ────────────────────────────────────────────────────────────────────

def parse_price(val: str | None) -> float | None:
    if not val:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_mileage(val: str | None) -> float | None:
    if not val:
        return None
    cleaned = re.sub(r"[^\d]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_year(name: str | None) -> int | None:
    if not name:
        return None
    m = re.search(r"\b(20\d{2}|19\d{2})\b", name)
    return int(m.group(1)) if m else None


@st.cache_data(ttl=120)
def load_last_runs() -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT source, MAX(run_date) as last_run FROM scrape_runs GROUP BY source"
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


@st.cache_data(ttl=120)
def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Latest run date per source to determine active status
    latest_runs = dict(
        conn.execute(
            "SELECT source, MAX(run_date) FROM scrape_runs GROUP BY source"
        ).fetchall()
    )

    df = pd.read_sql_query(
        "SELECT * FROM listings ORDER BY first_seen DESC, name",
        conn,
    )
    conn.close()

    df["price_num"] = df["price"].apply(parse_price)
    df["mileage_num"] = df["mileage"].apply(parse_mileage)
    df["year"] = df["name"].apply(extract_year)

    def is_active(row):
        latest = latest_runs.get(row["source"])
        return row["last_seen"] == latest if latest else False

    df["active"] = df.apply(is_active, axis=1)

    return df


# ── load ───────────────────────────────────────────────────────────────────────

if not DB_PATH.exists():
    st.error("No database found. Run a scraper first.")
    st.stop()

df = load_data()
last_runs = load_last_runs()

# ── sidebar filters ────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🚛 Filters")

    # Source
    sources = sorted(df["source"].unique())
    source_labels = {
        "facebook_marketplace": "Facebook Marketplace",
        "truck_paper": "TruckPaper",
    }
    selected_sources = st.multiselect(
        "Source",
        options=sources,
        default=sources,
        format_func=lambda s: source_labels.get(s, s),
    )

    st.divider()

    # Make
    makes = sorted(df["make"].dropna().unique())
    selected_makes = st.multiselect("Make", options=makes, default=makes)

    # Model (only show models relevant to selected makes)
    models_for_makes = sorted(
        df[df["make"].isin(selected_makes)]["model"].dropna().unique()
    ) if selected_makes else []
    if models_for_makes:
        selected_models = st.multiselect("Model", options=models_for_makes, default=models_for_makes)
    else:
        selected_models = []

    include_unknown_model = st.checkbox("Include unknown model", value=True)

    st.divider()

    # Status
    status_opt = st.radio("Status", ["Active only", "All listings"], index=0)
    active_only = status_opt == "Active only"

    st.divider()

    # Year
    years = df["year"].dropna().astype(int)
    year_min, year_max = int(years.min()), int(years.max())
    year_range = st.slider("Year", year_min, year_max, (year_min, year_max))

    st.divider()

    # Price
    prices = df["price_num"].dropna()
    price_min, price_max = int(prices.min()), int(prices.max())
    price_range = st.slider(
        "Price ($)",
        price_min,
        price_max,
        (price_min, price_max),
        step=500,
        format="$%d",
    )

    st.divider()

    # Mileage (only meaningful for TruckPaper)
    tp_miles = df[df["source"] == "truck_paper"]["mileage_num"].dropna()
    if not tp_miles.empty:
        mile_min, mile_max = int(tp_miles.min()), int(tp_miles.max())
        mile_range = st.slider(
            "Mileage (TruckPaper only)",
            mile_min,
            mile_max,
            (mile_min, mile_max),
            step=10_000,
            format="%d mi",
        )
    else:
        mile_range = None

    st.divider()
    if st.button("Clear cache / refresh", use_container_width=True):
        load_data.clear()
        load_last_runs.clear()
        st.rerun()

# ── apply filters ──────────────────────────────────────────────────────────────

# Make/model mask — listings with no make always pass (FB listings without MACK/FREIGHTLINER in name)
if selected_makes:
    make_mask = df["make"].isin(selected_makes) | df["make"].isna()
else:
    make_mask = pd.Series(True, index=df.index)

if selected_models:
    model_mask = df["model"].isin(selected_models)
    if include_unknown_model:
        model_mask |= df["model"].isna()
    # Only restrict model for rows that actually have a known make
    model_mask |= df["make"].isna()
else:
    model_mask = pd.Series(True, index=df.index)

mask = (
    df["source"].isin(selected_sources)
    & make_mask
    & model_mask
    & df["year"].between(year_range[0], year_range[1], inclusive="both").fillna(False)
    & (
        df["price_num"].between(price_range[0], price_range[1], inclusive="both")
        | df["price_num"].isna()
    )
)

if active_only:
    mask &= df["active"]

if mile_range is not None:
    tp_mask = df["source"] == "truck_paper"
    mile_mask = (
        df["mileage_num"].between(mile_range[0], mile_range[1], inclusive="both")
        | df["mileage_num"].isna()
        | ~tp_mask
    )
    mask &= mile_mask

filtered = df[mask].copy()

# ── header metrics ─────────────────────────────────────────────────────────────

st.title("Truck Listings Dashboard")

# Data freshness
_source_labels = {"facebook_marketplace": "Facebook", "truck_paper": "TruckPaper"}
_freshness_parts = [
    f"**{_source_labels.get(src, src)}:** {date}"
    for src, date in sorted(last_runs.items())
]
st.caption("Last scraped — " + "  |  ".join(_freshness_parts) if _freshness_parts else "Last scraped — no runs yet")

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total shown", len(filtered))
col2.metric("Active", int(filtered["active"].sum()))
col3.metric(
    "Avg price",
    f"${filtered['price_num'].mean():,.0f}" if not filtered["price_num"].isna().all() else "—",
)
col4.metric(
    "Lowest price",
    f"${filtered['price_num'].min():,.0f}" if not filtered["price_num"].isna().all() else "—",
)
col5.metric(
    "Avg mileage (TP)",
    f"{filtered[filtered['source']=='truck_paper']['mileage_num'].mean():,.0f} mi"
    if not filtered[filtered["source"] == "truck_paper"]["mileage_num"].isna().all()
    else "—",
)

st.divider()

# ── card grid ──────────────────────────────────────────────────────────────────

st.markdown("""
<style>
.card {
    border: 1px solid rgba(128,128,128,0.2);
    border-radius: 10px;
    overflow: hidden;
    background: rgba(255,255,255,0.03);
    display: flex;
    flex-direction: column;
    height: 100%;
}
.card-img {
    position: relative;
    width: 100%;
    height: 170px;
    background: rgba(128,128,128,0.12);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    flex-shrink: 0;
}
.card-img img {
    width: 100%;
    height: 170px;
    object-fit: cover;
    display: block;
}
.card-placeholder {
    font-size: 48px;
    opacity: 0.3;
}
.badge-row {
    position: absolute;
    top: 8px;
    left: 8px;
    display: flex;
    gap: 4px;
}
.badge {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
    letter-spacing: 0.04em;
}
.badge-fb  { background: #1877F2; color: #fff; }
.badge-tp  { background: #E65100; color: #fff; }
.badge-active   { background: #2e7d32; color: #fff; }
.badge-delisted { background: #555; color: #ccc; }
.card-body {
    padding: 11px 13px 13px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
}
.card-title {
    font-size: 13px;
    font-weight: 600;
    line-height: 1.35;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}
.card-price {
    font-size: 19px;
    font-weight: 700;
    color: #4CAF50;
    margin-top: 2px;
}
.card-price-na {
    font-size: 13px;
    color: #888;
    margin-top: 2px;
}
.card-detail {
    font-size: 12px;
    color: #aaa;
    margin-top: 1px;
}
.card-link {
    display: inline-block;
    margin-top: auto;
    padding-top: 10px;
    font-size: 12px;
    color: #4C9BE8;
    text-decoration: none;
    font-weight: 600;
}
.card-link:hover { text-decoration: underline; }
</style>
""", unsafe_allow_html=True)

# Sort + show controls
ctrl_l, ctrl_r = st.columns([2, 1])
with ctrl_l:
    sort_by = st.selectbox(
        "Sort by",
        ["Price: low → high", "Price: high → low", "Year: newest first", "Mileage: low → high"],
        label_visibility="collapsed",
    )
with ctrl_r:
    show_n = st.selectbox("Show", [40, 80, 160, 999999], index=0,
                          format_func=lambda n: "All" if n == 999999 else str(n),
                          label_visibility="collapsed")

sort_map = {
    "Price: low → high":    ("price_num", True),
    "Price: high → low":    ("price_num", False),
    "Year: newest first":   ("year", False),
    "Mileage: low → high":  ("mileage_num", True),
}
sort_col, sort_asc = sort_map[sort_by]
page_df = filtered.sort_values(sort_col, ascending=sort_asc, na_position="last").head(show_n)

# Render 4-column grid
COLS = 4
rows = [page_df.iloc[i : i + COLS] for i in range(0, len(page_df), COLS)]

for row_df in rows:
    cols = st.columns(COLS, gap="small")
    for col, (_, r) in zip(cols, row_df.iterrows()):
        with col:
            src_badge = "badge-fb" if r.source == "facebook_marketplace" else "badge-tp"
            src_label = "FB" if r.source == "facebook_marketplace" else "TP"
            status_badge = "badge-active" if r.active else "badge-delisted"
            status_label = "Active" if r.active else "Delisted"

            if r.image_url:
                img_html = f'<img src="{r.image_url}" alt="">'
            else:
                img_html = '<div class="card-placeholder">🚛</div>'

            price_html = (
                f'<div class="card-price">{r.price}</div>'
                if r.price
                else '<div class="card-price-na">Price not listed</div>'
            )

            details = []
            if r.year:
                details.append(f"📅 {int(r.year)}")
            if r.mileage:
                details.append(f"🛣 {r.mileage}")
            if r.engine_manufacturer or r.engine_model:
                eng = " ".join(filter(None, [r.engine_manufacturer, r.engine_model]))
                details.append(f"⚙️ {eng}")
            detail_html = "".join(
                f'<div class="card-detail">{d}</div>' for d in details
            )

            link_html = (
                f'<a class="card-link" href="{r.url}" target="_blank">View listing →</a>'
                if r.url else ""
            )

            st.markdown(f"""
<div class="card">
  <div class="card-img">
    {img_html}
    <div class="badge-row">
      <span class="badge {src_badge}">{src_label}</span>
      <span class="badge {status_badge}">{status_label}</span>
    </div>
  </div>
  <div class="card-body">
    <div class="card-title">{r["name"]}</div>
    {price_html}
    {detail_html}
    {link_html}
  </div>
</div>
""", unsafe_allow_html=True)

# ── price distribution chart ───────────────────────────────────────────────────

st.divider()
st.subheader("Price distribution")

chart_data = filtered[["price_num", "source"]].dropna(subset=["price_num"])
chart_data = chart_data.rename(columns={"price_num": "Price ($)", "source": "Source"})
chart_data["Source"] = chart_data["Source"].map(
    {"facebook_marketplace": "Facebook", "truck_paper": "TruckPaper"}
)

if not chart_data.empty:
    import altair as alt

    hist = (
        alt.Chart(chart_data)
        .mark_bar(opacity=0.8)
        .encode(
            x=alt.X("Price ($):Q", bin=alt.Bin(maxbins=30), title="Price ($)"),
            y=alt.Y("count()", title="Listings"),
            color=alt.Color("Source:N", scale=alt.Scale(range=["#4C9BE8", "#F4A340"])),
            tooltip=["Source:N", "count()"],
        )
        .properties(height=280)
    )
    st.altair_chart(hist, use_container_width=True)

# ── mileage distribution (TruckPaper only) ────────────────────────────────────

tp_filtered = filtered[filtered["source"] == "truck_paper"].dropna(subset=["mileage_num"])
if not tp_filtered.empty:
    st.subheader("Mileage distribution (TruckPaper)")

    mile_data = tp_filtered[["mileage_num"]].rename(columns={"mileage_num": "Mileage (mi)"})

    mile_hist = (
        alt.Chart(mile_data)
        .mark_bar(color="#F4A340", opacity=0.85)
        .encode(
            x=alt.X("Mileage (mi):Q", bin=alt.Bin(maxbins=25), title="Mileage"),
            y=alt.Y("count()", title="Listings"),
            tooltip=["count()"],
        )
        .properties(height=240)
    )
    st.altair_chart(mile_hist, use_container_width=True)
