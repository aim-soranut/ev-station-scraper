"""
EV station pricing & demand dashboard.

Reads the nationwide time series collected by monitor/main.py from Supabase
(via the anon key + read-only RLS) and visualizes price vs. occupancy.
All aggregation runs in Postgres via the dash_* RPC functions in
supabase/schema.sql, so the browser never pulls raw snapshot rows.

Config (Streamlit secrets or env vars):
  SUPABASE_URL
  SUPABASE_ANON_KEY
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import pydeck as pdk
import streamlit as st
from supabase import create_client

# Provinces this project focuses on (Nonthaburi + Pathum Thani).
DEFAULT_PROVINCE_CODES = ["12", "13"]

st.set_page_config(page_title="EV Station Pricing & Demand", layout="wide")


# ── Supabase client ─────────────────────────────────────────────────────────

def _secret(name):
    val = None
    try:
        if name in st.secrets:
            val = st.secrets[name]
    except Exception:
        pass
    if val is None:
        val = os.environ.get(name)
    # Trim stray whitespace/invisible chars (e.g. a U+2028 from pasting a URL).
    return val.strip() if isinstance(val, str) else val


@st.cache_resource
def get_client():
    url = _secret("SUPABASE_URL")
    key = _secret("SUPABASE_ANON_KEY")
    if not url or not key:
        st.error("Set SUPABASE_URL and SUPABASE_ANON_KEY in Streamlit secrets.")
        st.stop()
    return create_client(url, key)


def rpc(fn, params):
    return pd.DataFrame(get_client().rpc(fn, params).execute().data)


@st.cache_data(ttl=300)
def load_provinces():
    return rpc("dash_provinces", {})


@st.cache_data(ttl=300)
def load_latest(codes):
    return rpc("dash_latest_status", {"p_codes": codes})


@st.cache_data(ttl=300)
def load_occupancy(codes, start, end):
    return rpc("dash_occupancy_timeseries",
               {"p_codes": codes, "p_start": start, "p_end": end})


@st.cache_data(ttl=300)
def load_prices(codes, start, end):
    return rpc("dash_price_timeseries",
               {"p_codes": codes, "p_start": start, "p_end": end})


@st.cache_data(ttl=300)
def load_scatter(codes, start, end):
    return rpc("dash_station_price_occupancy",
               {"p_codes": codes, "p_start": start, "p_end": end})


@st.cache_data(ttl=300)
def load_history(station_id):
    return rpc("dash_station_history", {"p_station_id": int(station_id)})


# ── Sidebar filters ─────────────────────────────────────────────────────────

st.sidebar.title("Filters")

provinces = load_provinces()
if provinces.empty:
    st.warning("No data yet. Is the poller running and writing to Supabase?")
    st.stop()

label_by_code = {
    r.province_code: f"{r.province_name} ({r.station_count})"
    for r in provinces.itertuples()
}
all_codes = list(label_by_code)
default_codes = [c for c in DEFAULT_PROVINCE_CODES if c in label_by_code] or all_codes

selected_codes = st.sidebar.multiselect(
    "Provinces",
    options=all_codes,
    default=default_codes,
    format_func=lambda c: label_by_code.get(c, c),
)
codes_param = selected_codes or None  # None => all Thailand

days = st.sidebar.slider("History window (days)", 1, 30, 7)
end = datetime.now(timezone.utc)
start = end - timedelta(days=days)
start_iso, end_iso = start.isoformat(), end.isoformat()

conn_type = st.sidebar.radio("Connector type", ["Both", "AC", "DC"], horizontal=True)

latest = load_latest(codes_param)
if not latest.empty and "source" in latest:
    brands = sorted(latest["source"].dropna().unique().tolist())
    chosen_brands = st.sidebar.multiselect("Charging network (brand)", brands, default=[])
else:
    chosen_brands = []


def apply_brand(df):
    if chosen_brands and "source" in df:
        return df[df["source"].isin(chosen_brands)]
    return df


# Suffix for export filenames, e.g. 2026-06-12_2026-06-19.
FNAME_SUFFIX = f"{start.date()}_{end.date()}"


def csv_download(df, label, filename, key):
    """Render a CSV download button for the (already filtered) dataframe."""
    if df is None or df.empty:
        return
    st.download_button(
        label, df.to_csv(index=False).encode("utf-8"),
        file_name=filename, mime="text/csv", key=key,
    )


# ── Header KPIs ──────────────────────────────────────────────────────────────

st.title("EV Station Pricing & Demand — Thailand")

latest_f = apply_brand(latest)
c1, c2, c3, c4, c5 = st.columns(5)
if latest_f.empty:
    st.info("No snapshots for the current selection yet.")
else:
    occ = latest_f["n_occupied"].sum() / max(latest_f["n_connectors"].sum(), 1)
    c1.metric("Stations", f"{len(latest_f):,}")
    c2.metric("Occupancy now", f"{occ:.0%}")
    c3.metric("Median AC ฿/kWh", f"{latest_f['ac_min_price'].median():.2f}"
              if latest_f['ac_min_price'].notna().any() else "—")
    c4.metric("Median DC ฿/kWh", f"{latest_f['dc_min_price'].median():.2f}"
              if latest_f['dc_min_price'].notna().any() else "—")
    last_poll = pd.to_datetime(latest_f["polled_at"]).max()
    c5.metric("Last poll (UTC)", last_poll.strftime("%m-%d %H:%M") if pd.notna(last_poll) else "—")

tab_map, tab_occ, tab_price, tab_scatter, tab_station = st.tabs(
    ["🗺 Map", "📈 Occupancy", "💸 Price", "🎯 Price vs Demand", "🔎 Station"]
)

# ── Map ──────────────────────────────────────────────────────────────────────

with tab_map:
    st.caption("Current status. Color = occupancy rate (green idle → red busy), size = connector count.")
    df = latest_f.dropna(subset=["latitude", "longitude"]).copy()
    if df.empty:
        st.info("No located stations in selection.")
    else:
        df["occ"] = df["n_occupied"] / df["n_connectors"].clip(lower=1)
        df["r"] = (df["occ"] * 255).fillna(0).astype(int)
        df["g"] = (200 - df["occ"] * 200).fillna(200).astype(int)
        layer = pdk.Layer(
            "ScatterplotLayer", df,
            get_position=["longitude", "latitude"],
            get_fill_color=["r", "g", 60, 160],
            get_radius="n_connectors * 120",
            radius_min_pixels=3, pickable=True,
        )
        view = pdk.ViewState(
            latitude=df["latitude"].mean(), longitude=df["longitude"].mean(), zoom=8,
        )
        st.pydeck_chart(pdk.Deck(
            layers=[layer], initial_view_state=view,
            tooltip={"text": "{name}\n{ocpp_status}  occ={n_occupied}/{n_connectors}"},
        ))
        csv_download(latest_f, "⬇ Download current status (CSV)",
                     "latest_status.csv", "dl_latest")

# ── Occupancy over time ───────────────────────────────────────────────────────

with tab_occ:
    df = load_occupancy(codes_param, start_iso, end_iso)
    if df.empty:
        st.info("No occupancy data in this window.")
    else:
        df["ts"] = pd.to_datetime(df["ts"])
        fig = px.line(df, x="ts", y="occupancy_rate", color="province_name",
                      labels={"occupancy_rate": "Occupancy rate", "ts": "Time (UTC)"})
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)
        csv_download(df, "⬇ Download occupancy series (CSV)",
                     f"occupancy_{FNAME_SUFFIX}.csv", "dl_occ")

# ── Price over time ───────────────────────────────────────────────────────────

with tab_price:
    df = load_prices(codes_param, start_iso, end_iso)
    if df.empty:
        st.info("No price data in this window.")
    else:
        df["ts"] = pd.to_datetime(df["ts"])
        value_cols = {"AC": ["avg_ac_price"], "DC": ["avg_dc_price"],
                      "Both": ["avg_ac_price", "avg_dc_price"]}[conn_type]
        melted = df.melt(id_vars=["ts", "province_name"], value_vars=value_cols,
                         var_name="kind", value_name="price").dropna(subset=["price"])
        melted["series"] = melted["province_name"] + " " + melted["kind"].str.replace(
            "avg_", "").str.replace("_price", "").str.upper()
        fig = px.line(melted, x="ts", y="price", color="series",
                      labels={"price": "฿/kWh", "ts": "Time (UTC)"})
        st.plotly_chart(fig, use_container_width=True)
        csv_download(df, "⬇ Download price series (CSV)",
                     f"prices_{FNAME_SUFFIX}.csv", "dl_price")

# ── Price vs demand scatter ───────────────────────────────────────────────────

with tab_scatter:
    st.caption("Each point is a station: average price vs. average occupancy over the window. "
               "The core question — do cheaper stations run busier?")
    df = apply_brand(load_scatter(codes_param, start_iso, end_iso))
    df = df.dropna(subset=["avg_price", "avg_occupancy"]) if not df.empty else df
    if df.empty:
        st.info("Not enough data for a scatter yet.")
    else:
        fig = px.scatter(
            df, x="avg_price", y="avg_occupancy", color="province_name",
            size="samples", hover_name="name", trendline="ols",
            labels={"avg_price": "Avg price ฿/kWh", "avg_occupancy": "Avg occupancy"},
        )
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)
        csv_download(df, "⬇ Download price-vs-occupancy summary (CSV)",
                     f"price_vs_occupancy_{FNAME_SUFFIX}.csv", "dl_scatter")

# ── Per-station detail ────────────────────────────────────────────────────────

with tab_station:
    if latest_f.empty:
        st.info("No stations in selection.")
    else:
        opts = latest_f.sort_values("name")[["id", "name"]]
        choice = st.selectbox("Station", opts["id"],
                              format_func=lambda i: opts.set_index("id").loc[i, "name"])
        hist = load_history(choice)
        if hist.empty:
            st.info("No history for this station.")
        else:
            hist["ts"] = pd.to_datetime(hist["polled_at"])
            hist["occupancy"] = hist["n_occupied"] / hist["n_connectors"].clip(lower=1)
            # Coerce to float so an all-null AC (or DC) column doesn't end up a
            # different dtype than its sibling (Plotly wide-form rejects that).
            for c in ["ac_min_price", "dc_min_price"]:
                hist[c] = pd.to_numeric(hist[c], errors="coerce")
            col1, col2 = st.columns(2)
            col1.plotly_chart(
                px.line(hist, x="ts", y="occupancy", title="Occupancy")
                .update_yaxes(tickformat=".0%"), use_container_width=True)
            price_long = hist.melt(
                id_vars="ts", value_vars=["ac_min_price", "dc_min_price"],
                var_name="kind", value_name="price").dropna(subset=["price"])
            if price_long.empty:
                col2.info("No price history for this station.")
            else:
                price_long["kind"] = price_long["kind"].str.replace(
                    "_min_price", "", regex=False).str.upper()
                col2.plotly_chart(
                    px.line(price_long, x="ts", y="price", color="kind",
                            title="Price ฿/kWh"),
                    use_container_width=True)
            csv_download(hist, "⬇ Download this station's history (CSV)",
                         f"station_{choice}_history.csv", "dl_station")
