# Price Tracking Enhancements

## Context

The dashboard (`dashboard/app.py`) shows a grid of truck listings but has no way to inspect a single listing's price history over time. The `price_history` table already captures every price change per listing. The goal is to add a drill-down detail view: click into a listing from the card grid, see its price history chart and metadata, then navigate back.

**Rules:**
- Only chart prices that are numeric — "Call for Price" and null prices are kept as text but not plotted
- Delisted listings show whatever history they accumulated plus their `last_seen` date
- No navigation between listings needed yet — back button is sufficient for now

---

## File to modify

- `dashboard/app.py` — only file that changes

---

## Implementation Steps

### 1. Session state init
Add after `load_data()` is called:
```python
if "selected" not in st.session_state:
    st.session_state["selected"] = None  # (listing_id, source) or None
```

### 2. New cached price history loader
Add alongside `load_data()`, reusing the existing `parse_price()` helper:
```python
@st.cache_data(ttl=120)
def load_price_history(listing_id: str, source: str) -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    df = pd.read_sql_query(
        "SELECT date, price FROM price_history WHERE listing_id = ? AND source = ? ORDER BY date",
        conn,
        params=(listing_id, source),
    )
    conn.close()
    df["price_num"] = df["price"].apply(parse_price)
    return df
```

### 3. "View history" button on each card
After each `st.markdown(...)` card block in the card grid loop, add a native Streamlit button:
```python
if st.button("View history", key=f"hist_{r['id']}_{r['source']}", use_container_width=True):
    st.session_state["selected"] = (r["id"], r["source"])
    st.rerun()
```
> HTML inside `st.markdown` cannot trigger Python callbacks, so the button must be a native Streamlit element rendered below the card HTML.

### 4. Detail view
Wrap the existing card grid section in `if st.session_state["selected"] is None:` and add an `else:` branch:

```python
else:
    listing_id, source = st.session_state["selected"]
    row = df[(df["id"] == listing_id) & (df["source"] == source)].iloc[0]
    ph = load_price_history(listing_id, source)

    # Back button
    if st.button("← Back to listings"):
        st.session_state["selected"] = None
        st.rerun()

    # Listing header
    st.subheader(row["name"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Current price", row["price"] or "—")
    c2.metric("Status", "Active" if row["active"] else f"Delisted · last seen {row['last_seen']}")
    c3.metric("First seen", row["first_seen"])

    # Additional details (location, mileage, engine, VIN)
    # rendered as st.write lines

    # Price history section
    st.subheader("Price history")
    numeric_ph = ph.dropna(subset=["price_num"])

    if len(ph) <= 1:
        st.info("Only one price entry recorded — check back after more scrape runs.")
    elif numeric_ph.empty:
        st.write("No numeric prices to chart.")
        st.dataframe(ph[["date", "price"]], hide_index=True)
    else:
        # Step chart — flat line until a change occurs
        chart = (
            alt.Chart(numeric_ph)
            .mark_line(interpolate="step-after", point=True)
            .encode(
                x=alt.X("date:T", title="Date"),
                y=alt.Y("price_num:Q", title="Price ($)", scale=alt.Scale(zero=False)),
                tooltip=["date:T", "price_num:Q"],
            )
            .properties(height=280)
        )
        st.altair_chart(chart, use_container_width=True)
        # Full table below chart (shows "Call for Price" rows too)
        st.dataframe(
            ph[["date", "price"]].rename(columns={"date": "Date", "price": "Price"}),
            hide_index=True,
        )

    if row["url"]:
        st.markdown(f"[View original listing →]({row['url']})")
```

### 5. What stays unchanged
- Sidebar filters
- Header metrics (5 columns)
- Price distribution chart
- Mileage distribution chart

These all render regardless of whether a listing is selected.

---

## Verification

1. `streamlit run dashboard/app.py` from the project root
2. Card grid should render with a "View history" button under each card
3. Click a button → detail view appears with listing metadata
4. With only one scrape run: info message shown instead of chart (expected)
5. After a second scrape run with a price change: step chart shows the drop/increase
6. "Call for Price" listing: no chart, text table shows the entry
7. Back button returns to the card grid with all sidebar filters preserved
8. Delisted listing: status shows last_seen date, history renders normally
