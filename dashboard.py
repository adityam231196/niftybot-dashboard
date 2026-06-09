"""
NiftyBot Research Dashboard
===========================
Live Streamlit dashboard reading the NiftyBot Google Sheet directly via a
Google service account. No file uploads needed.

Pages:
  1. Overview            - capital curves, today's PnL, V29 streak
  2. Daily Log           - per-day futures + options PnL per strategy
  3. Strategy Comparison - win rates, avg PnL, totals
  4. Research Questions  - auto-computed evidence for the active questions
  5. Signal Detail       - per-day drill-down of signals and filters
  6. Capital Tracker     - Rs.30K start, running balance, milestones

Deploy: Streamlit Cloud, with the service-account JSON stored under the
[gcp_service_account] secret. See README_DEPLOY.md for exact steps.
"""

import io
import json
from datetime import datetime, date, time as dtime

import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go

# ---------------------------------------------------------------- constants

SHEET_FILE_ID = "1Pw1oD79rf4NCPNKbNrkxiV5w9oR5DLCcBwzm_XihUPc"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

LOT_SIZE = 65
START_CAPITAL = 30_000          # per strategy, from May 1 2026
CAPITAL_START_DATE = date(2026, 5, 1)
PRICE_FLOOR, PRICE_CEIL = 150, 200
RSI_MIN = 60
MILESTONES = [50_000, 100_000, 250_000, 500_000, 1_000_000, 2_500_000,
              5_000_000, 10_000_000]

STRATS = ["TRIFORCE", "V28", "V29", "PURE3"]
STRAT_COLORS = {"TRIFORCE": "#2563EB", "V28": "#059669",
                "V29": "#D97706", "PURE3": "#7C3AED"}

st.set_page_config(page_title="NiftyBot Dashboard", page_icon="📈",
                   layout="wide")

# ---------------------------------------------------------------- loading


def _drive_service():
    """Build a Drive API client from the Streamlit secret."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = dict(st.secrets["gcp_service_account"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@st.cache_data(ttl=60, show_spinner="Reading NiftyBot sheet from Google Drive…")
def load_workbook_bytes() -> bytes:
    """Export the Google Sheet as xlsx via the Drive API."""
    from googleapiclient.http import MediaIoBaseDownload

    svc = _drive_service()
    req = svc.files().export_media(fileId=SHEET_FILE_ID, mimeType=XLSX_MIME)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


@st.cache_data(ttl=60)
def load_tabs() -> dict:
    """Return {tab_name: DataFrame} with normalised lowercase columns."""
    raw = load_workbook_bytes()
    sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, engine="openpyxl")
    out = {}
    for name, df in sheets.items():
        df = df.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]
        out[name.strip().lower()] = df
    return out


# ---------------------------------------------------------------- helpers


def col(df: pd.DataFrame, *candidates, default=None):
    """Return the first matching column name present in df, else default."""
    for c in candidates:
        if c in df.columns:
            return c
    return default


def to_dt(s):
    return pd.to_datetime(s, errors="coerce", dayfirst=False)


def norm_strat(s):
    s = str(s).strip().upper()
    if s.startswith("TRI"):
        return "TRIFORCE"
    if "PURE" in s:
        return "PURE3"
    return s


def safe_df(tabs: dict, name: str) -> pd.DataFrame:
    df = tabs.get(name)
    if df is None:
        for k in tabs:                       # fuzzy match on tab name
            if name.replace("_", "") in k.replace("_", ""):
                return tabs[k]
        return pd.DataFrame()
    return df


def inr(x):
    try:
        return f"₹{x:,.0f}"
    except (TypeError, ValueError):
        return "—"


# ---------------------------------------------------------------- shaping


@st.cache_data(ttl=60)
def build_datasets():
    """Produce the tidy frames every page works from."""
    tabs = load_tabs()

    # ---- options trades: T / V28 / V29 + Pure3 merged into one frame ----
    frames = []
    for tab, default_strat in [("options_trade_log", None),
                               ("pure3_trade_log", "PURE3")]:
        df = safe_df(tabs, tab)
        if df.empty:
            continue
        d = df.copy()
        sc = col(d, "strategy")
        d["strategy"] = (d[sc].map(norm_strat) if sc else default_strat)
        if default_strat:
            d["strategy"] = d["strategy"].fillna(default_strat)
        tcol = col(d, "signal_time", "entry_time", "timestamp")
        d["signal_dt"] = to_dt(d[tcol]) if tcol else pd.NaT
        d["trade_date"] = d["signal_dt"].dt.date
        for c in ["entry_option_price", "exit_option_price", "pnl_rupees",
                  "pnl_points"]:
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        for c in ["exit_reason", "entry_type", "expiry_track", "strike_type",
                  "selected_strike", "option_symbol", "is_reentry",
                  "direction"]:
            if c not in d.columns:
                d[c] = np.nan
        d["src_tab"] = tab
        frames.append(d)

    opt = (pd.concat(frames, ignore_index=True)
           if frames else pd.DataFrame())
    if not opt.empty:
        opt["expiry_track"] = opt["expiry_track"].astype(str).str.upper()
        opt["is_futures_only"] = opt["expiry_track"].eq("FUTURES_ONLY")
        # closed option trades only, real option legs only
        opt["closed"] = opt["pnl_rupees"].notna() & ~opt["is_futures_only"]
        opt["win"] = opt["pnl_rupees"] > 0

    # ---- futures trades: trade_log (T/V28/V29) + Pure3 pnl_points -------
    fut_frames = []
    tl = safe_df(tabs, "trade_log")
    if not tl.empty:
        d = tl.copy()
        sc = col(d, "strategy")
        d["strategy"] = d[sc].map(norm_strat) if sc else np.nan
        ec = col(d, "entry_date", "entry_time", "signal_time")
        xc = col(d, "exit_date", "exit_time")
        d["entry_dt"] = to_dt(d[ec]) if ec else pd.NaT
        d["exit_dt"] = to_dt(d[xc]) if xc else pd.NaT
        d["trade_date"] = d["entry_dt"].dt.date
        for c in ["entry_price", "exit_price"]:
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        sign = d.get("direction", pd.Series(index=d.index, dtype=object)) \
                .astype(str).str.upper().map(
                    lambda x: -1 if ("SHORT" in x or "SELL" in x or
                                     x.startswith("PE") or "DOWN" in x) else 1)
        if {"entry_price", "exit_price"} <= set(d.columns):
            d["fut_points"] = (d["exit_price"] - d["entry_price"]) * sign
        else:
            d["fut_points"] = np.nan
        d["fut_rupees"] = d["fut_points"] * LOT_SIZE
        d["exit_type"] = d.get("exit_type", np.nan)
        fut_frames.append(d[["strategy", "trade_date", "entry_dt", "exit_dt",
                             "fut_points", "fut_rupees", "exit_type"]])

    p3 = safe_df(tabs, "pure3_trade_log")
    if not p3.empty and "pnl_points" in p3.columns:
        d = p3.copy()
        tcol = col(d, "signal_time", "entry_time", "timestamp")
        d["entry_dt"] = to_dt(d[tcol]) if tcol else pd.NaT
        d["trade_date"] = d["entry_dt"].dt.date
        d["fut_points"] = pd.to_numeric(d["pnl_points"], errors="coerce")
        d["fut_rupees"] = d["fut_points"] * LOT_SIZE
        d["strategy"] = "PURE3"
        d["exit_dt"] = pd.NaT
        d["exit_type"] = d.get("exit_reason", np.nan)
        # one futures row per signal (avoid double count across option legs)
        d = d.drop_duplicates(subset=["entry_dt"])
        fut_frames.append(d[["strategy", "trade_date", "entry_dt", "exit_dt",
                             "fut_points", "fut_rupees", "exit_type"]])

    fut = (pd.concat(fut_frames, ignore_index=True)
           if fut_frames else pd.DataFrame())

    # ---- daily pivots ----------------------------------------------------
    if not opt.empty:
        daily_opt = (opt[opt["closed"]]
                     .groupby(["trade_date", "strategy"])["pnl_rupees"]
                     .sum().unstack(fill_value=0.0))
    else:
        daily_opt = pd.DataFrame()
    if not fut.empty:
        daily_fut = (fut.dropna(subset=["fut_rupees"])
                     .groupby(["trade_date", "strategy"])["fut_rupees"]
                     .sum().unstack(fill_value=0.0))
    else:
        daily_fut = pd.DataFrame()

    other = {k: safe_df(tabs, k) for k in
             ["entry_decision_log", "candle_log", "options_candle_log",
              "pure3_candle_log", "exit_log"]}
    return opt, fut, daily_opt, daily_fut, other


def capital_curve(daily: pd.DataFrame) -> pd.DataFrame:
    """Rs.30K per strategy + cumulative daily PnL."""
    if daily.empty:
        return pd.DataFrame()
    d = daily.sort_index().copy()
    d = d[d.index >= CAPITAL_START_DATE]
    cum = d.cumsum() + START_CAPITAL
    return cum


def v29_tally(daily_opt: pd.DataFrame):
    """Per day: V29 options PnL vs mean(T, V28). Returns df + streak."""
    need = {"V29"} & set(daily_opt.columns)
    base_cols = [c for c in ("TRIFORCE", "V28") if c in daily_opt.columns]
    if not need or not base_cols:
        return pd.DataFrame(), 0, "—"
    d = daily_opt.copy()
    d["tv28_avg"] = d[base_cols].mean(axis=1)
    d = d[(d["V29"] != 0) | (d["tv28_avg"] != 0)]
    d["verdict"] = np.where(d["V29"] > d["tv28_avg"], "BETTER",
                   np.where(d["V29"] < d["tv28_avg"], "WORSE", "TIE"))
    streak, last = 0, None
    for v in d["verdict"][::-1]:
        if v == "TIE":
            continue
        if last is None:
            last = v
        if v == last:
            streak += 1
        else:
            break
    return d, streak, (last or "—")


# ---------------------------------------------------- research computations


def rq_a3_entry_type(opt):
    """A2/A3 - IMMEDIATE vs BOUNCE entry record."""
    d = opt[opt["closed"] & opt["entry_type"].notna()].copy()
    if d.empty:
        return pd.DataFrame()
    d["entry_type"] = d["entry_type"].astype(str).str.upper()
    g = d.groupby("entry_type").agg(
        trades=("pnl_rupees", "size"),
        wins=("win", "sum"),
        total_pnl=("pnl_rupees", "sum"),
        avg_pnl=("pnl_rupees", "mean"))
    g["win_rate_%"] = (g["wins"] / g["trades"] * 100).round(1)
    return g.reset_index()


def rq_d2_pure3_exits(opt):
    """D2 - Pure3: SMI_ZONE vs 1OF3_BREAK vs EOD exits."""
    d = opt[(opt["strategy"] == "PURE3") & opt["closed"]].copy()
    if d.empty:
        return pd.DataFrame()
    d["exit_reason"] = d["exit_reason"].astype(str).str.upper()
    g = d.groupby("exit_reason").agg(
        trades=("pnl_rupees", "size"),
        wins=("win", "sum"),
        total_pnl=("pnl_rupees", "sum"),
        avg_pnl=("pnl_rupees", "mean"))
    g["win_rate_%"] = (g["wins"] / g["trades"] * 100).round(1)
    return g.reset_index()


def rq_d4_smi_vs_hold(opt, other):
    """D4 - For T/V28/V29 trades: hypothetical exit at Pure3's SMI_ZONE
    time on the same day, valued from options_candle_log."""
    ocl = other.get("options_candle_log", pd.DataFrame())
    p3 = opt[(opt["strategy"] == "PURE3") & opt["closed"]].copy()
    tv = opt[opt["strategy"].isin(["TRIFORCE", "V28", "V29"]) &
             opt["closed"]].copy()
    if ocl.empty or p3.empty or tv.empty:
        return pd.DataFrame()
    p3["exit_reason"] = p3["exit_reason"].astype(str).str.upper()
    smi = p3[p3["exit_reason"].str.contains("SMI", na=False)].copy()
    if smi.empty:
        return pd.DataFrame()
    # SMI exit timestamp ~ approximate with last logged candle of that signal
    c = ocl.copy()
    tc = col(c, "candle_time")
    sc = col(c, "signal_time")
    if not tc:
        return pd.DataFrame()
    c["candle_dt"] = to_dt(c[tc])
    c["candle_date"] = c["candle_dt"].dt.date
    smi_times = (c[c.get("strategy", "").astype(str).str.upper()
                   .str.contains("PURE", na=False)]
                 .groupby("candle_date")["candle_dt"].max()
                 if "strategy" in c.columns else pd.Series(dtype="datetime64[ns]"))
    rows = []
    for _, t in tv.iterrows():
        day = t["trade_date"]
        if day not in smi_times.index or pd.isna(t.get("entry_option_price")):
            continue
        cutoff = smi_times.loc[day]
        sym = t.get("option_symbol")
        sub = c[(c["candle_date"] == day) & (c["candle_dt"] <= cutoff)]
        if sym is not np.nan and "option_symbol" in c.columns:
            sub = sub[sub["option_symbol"] == sym]
        if sub.empty or "option_close" not in sub.columns:
            continue
        px = pd.to_numeric(
            sub.sort_values("candle_dt")["option_close"], errors="coerce"
        ).dropna()
        if px.empty:
            continue
        hyp = (px.iloc[-1] - t["entry_option_price"]) * LOT_SIZE
        rows.append({"date": day, "strategy": t["strategy"],
                     "symbol": sym,
                     "actual_pnl": t["pnl_rupees"],
                     "hyp_smi_exit_pnl": round(hyp),
                     "smi_better_by": round(hyp - t["pnl_rupees"])})
    return pd.DataFrame(rows)


def rq_e3_divergence(daily_opt, daily_fut):
    """E3 - days where futures and options PnL disagree in sign."""
    if daily_opt.empty or daily_fut.empty:
        return pd.DataFrame(), 0, 0
    rows = []
    for strat in [s for s in STRATS
                  if s in daily_opt.columns and s in daily_fut.columns]:
        m = pd.DataFrame({"options": daily_opt[strat],
                          "futures": daily_fut[strat]}).dropna()
        m = m[(m["options"] != 0) | (m["futures"] != 0)]
        for day, r in m.iterrows():
            diverged = np.sign(r["options"]) != np.sign(r["futures"]) and \
                       r["options"] != 0 and r["futures"] != 0
            rows.append({"date": day, "strategy": strat,
                         "futures_pnl": round(r["futures"]),
                         "options_pnl": round(r["options"]),
                         "diverged": "YES" if diverged else ""})
    df = pd.DataFrame(rows)
    if df.empty:
        return df, 0, 0
    return df, int((df["diverged"] == "YES").sum()), len(df)


def rq_b2_ceiling(other):
    """B2 - strikes first seen at Rs.200-250: hypothetical entry->last PnL."""
    c = other.get("options_candle_log", pd.DataFrame())
    if c.empty or "option_close" not in c.columns:
        return pd.DataFrame()
    c = c.copy()
    tc = col(c, "candle_time")
    c["candle_dt"] = to_dt(c[tc]) if tc else pd.NaT
    c["option_close"] = pd.to_numeric(c["option_close"], errors="coerce")
    keys = [k for k in ["strategy", "signal_time", "option_symbol",
                        "expiry_track"] if k in c.columns]
    if not keys:
        return pd.DataFrame()
    rows = []
    for key, g in c.dropna(subset=["option_close"]).groupby(keys):
        g = g.sort_values("candle_dt")
        first, last = g["option_close"].iloc[0], g["option_close"].iloc[-1]
        if PRICE_CEIL < first <= 250:
            rec = dict(zip(keys, key if isinstance(key, tuple) else (key,)))
            rec.update({"first_price": round(first, 2),
                        "last_price": round(last, 2),
                        "hyp_pnl_rupees": round((last - first) * LOT_SIZE)})
            rows.append(rec)
    return pd.DataFrame(rows)


def rq_a1_rsi_blocked(opt, other):
    """A1 - tracked strikes priced 150-200 but RSI<60 at first eligible
    candle, with no executed trade for that signal: hypothetical PnL."""
    c = other.get("options_candle_log", pd.DataFrame())
    if c.empty or not {"option_close", "option_rsi"} <= set(c.columns):
        return pd.DataFrame()
    c = c.copy()
    tc = col(c, "candle_time")
    c["candle_dt"] = to_dt(c[tc]) if tc else pd.NaT
    for x in ["option_close", "option_rsi"]:
        c[x] = pd.to_numeric(c[x], errors="coerce")
    executed = set()
    if not opt.empty:
        executed = set(zip(opt["strategy"],
                           opt["signal_dt"].astype(str),
                           opt["option_symbol"].astype(str)))
    keys = [k for k in ["strategy", "signal_time", "option_symbol"]
            if k in c.columns]
    rows = []
    for key, g in c.dropna(subset=["option_close"]).groupby(keys):
        g = g.sort_values("candle_dt")
        elig = g[(g["option_close"].between(PRICE_FLOOR, PRICE_CEIL)) &
                 (g["option_rsi"] < RSI_MIN)]
        if elig.empty:
            continue
        kd = dict(zip(keys, key if isinstance(key, tuple) else (key,)))
        sig = (norm_strat(kd.get("strategy", "")),
               str(to_dt(kd.get("signal_time"))),
               str(kd.get("option_symbol")))
        if sig in executed:
            continue
        entry = elig["option_close"].iloc[0]
        last = g["option_close"].iloc[-1]
        kd.update({"blocked_entry_price": round(entry, 2),
                   "last_tracked_price": round(last, 2),
                   "hyp_pnl_rupees": round((last - entry) * LOT_SIZE),
                   "rsi_at_block": round(elig["option_rsi"].iloc[0], 1)})
        rows.append(kd)
    return pd.DataFrame(rows)


def rq_f3_futures_only(opt, daily_fut):
    """F3 - when the price filter blocked all strikes (FUTURES_ONLY),
    did the futures direction win or lose that day?"""
    if opt.empty or daily_fut.empty:
        return pd.DataFrame()
    fo = opt[opt["is_futures_only"]].copy()
    if fo.empty:
        return pd.DataFrame()
    rows = []
    for _, r in fo.iterrows():
        day, strat = r["trade_date"], r["strategy"]
        futpnl = (daily_fut.loc[day, strat]
                  if day in daily_fut.index and strat in daily_fut.columns
                  else np.nan)
        rows.append({"date": day, "strategy": strat,
                     "futures_pnl_that_day": (round(futpnl)
                                              if pd.notna(futpnl) else None),
                     "direction_right": ("YES" if pd.notna(futpnl) and
                                         futpnl > 0 else
                                         "NO" if pd.notna(futpnl) and
                                         futpnl < 0 else "?")})
    return pd.DataFrame(rows).drop_duplicates()


# ---------------------------------------------------------------- UI pages


def page_overview(opt, daily_opt, daily_fut):
    st.title("NiftyBot — Overview")
    if daily_opt.empty:
        st.info("No closed options trades found yet.")
        return
    cum = capital_curve(daily_opt)
    latest = cum.iloc[-1] if not cum.empty else pd.Series(dtype=float)

    cols = st.columns(len(STRATS))
    for i, s in enumerate(STRATS):
        val = latest.get(s, START_CAPITAL)
        delta = val - START_CAPITAL
        cols[i].metric(f"{s} (options)", inr(val), inr(delta))

    # capital curve
    fig = go.Figure()
    for s in [s for s in STRATS if s in cum.columns]:
        fig.add_trace(go.Scatter(x=cum.index, y=cum[s], name=s,
                                 line=dict(color=STRAT_COLORS[s], width=2)))
    fig.add_hline(y=START_CAPITAL, line_dash="dot", line_color="grey",
                  annotation_text="₹30K start")
    fig.update_layout(title="Options-only capital curve (₹30K start, May 1)",
                      height=420, hovermode="x unified",
                      yaxis_title="Capital (₹)")
    st.plotly_chart(fig, use_container_width=True)

    # today / latest day PnL
    last_day = daily_opt.index.max()
    st.subheader(f"Latest trading day: {last_day}")
    c1, c2 = st.columns(2)
    with c1:
        st.caption("Options PnL")
        st.dataframe(daily_opt.loc[[last_day]].T.rename(
            columns={last_day: "₹"}).style.format("{:,.0f}"))
    with c2:
        st.caption("Futures PnL")
        if not daily_fut.empty and last_day in daily_fut.index:
            st.dataframe(daily_fut.loc[[last_day]].T.rename(
                columns={last_day: "₹"}).style.format("{:,.0f}"))
        else:
            st.write("—")

    # V29 streak
    tally, streak, side = v29_tally(daily_opt)
    if not tally.empty:
        better = int((tally["verdict"] == "BETTER").sum())
        worse = int((tally["verdict"] == "WORSE").sum())
        st.subheader("V29 early-entry tracker")
        a, b, c = st.columns(3)
        a.metric("Days BETTER than T/V28", better)
        b.metric("Days WORSE", worse)
        c.metric("Current streak", f"{streak} × {side}")


def page_daily_log(daily_opt, daily_fut, other):
    st.title("Daily Log")
    if daily_opt.empty and daily_fut.empty:
        st.info("No trade data yet.")
        return
    opt_t = daily_opt.add_suffix(" · OPT") if not daily_opt.empty \
        else pd.DataFrame()
    fut_t = daily_fut.add_suffix(" · FUT") if not daily_fut.empty \
        else pd.DataFrame()
    merged = opt_t.join(fut_t, how="outer").sort_index(ascending=False)
    merged["TOTAL ₹"] = merged.sum(axis=1)
    st.dataframe(merged.style.format("{:,.0f}", na_rep="—")
                 .map(lambda v: "color:#059669" if isinstance(v, (int, float))
                      and v > 0 else ("color:#DC2626" if isinstance(
                          v, (int, float)) and v < 0 else "")),
                 use_container_width=True, height=520)

    edl = other.get("entry_decision_log", pd.DataFrame())
    if not edl.empty:
        tc = col(edl, "timestamp", "signal_time")
        if tc:
            e = edl.copy()
            e["d"] = to_dt(e[tc]).dt.date
            rc = col(e, "result")
            if rc:
                st.subheader("Signals per day (entry_decision_log)")
                pivot = (e.groupby(["d", rc]).size().unstack(fill_value=0)
                         .sort_index(ascending=False))
                st.dataframe(pivot, use_container_width=True)


def page_strategy_comparison(opt, fut):
    st.title("Strategy Comparison")
    if opt.empty:
        st.info("No options trades yet.")
        return
    d = opt[opt["closed"]]
    g = d.groupby("strategy").agg(
        trades=("pnl_rupees", "size"),
        wins=("win", "sum"),
        total_pnl=("pnl_rupees", "sum"),
        avg_pnl=("pnl_rupees", "mean"),
        best=("pnl_rupees", "max"),
        worst=("pnl_rupees", "min"))
    g["win_rate_%"] = (g["wins"] / g["trades"] * 100).round(1)
    st.subheader("Options")
    st.dataframe(g.reindex([s for s in STRATS if s in g.index])
                 .style.format("{:,.0f}", subset=["total_pnl", "avg_pnl",
                                                  "best", "worst"]),
                 use_container_width=True)
    fig = px.bar(g.reset_index(), x="strategy", y="total_pnl",
                 color="strategy", color_discrete_map=STRAT_COLORS,
                 title="Total options PnL by strategy")
    st.plotly_chart(fig, use_container_width=True)

    if not fut.empty:
        st.subheader("Futures")
        f = fut.dropna(subset=["fut_rupees"])
        gf = f.groupby("strategy").agg(
            trades=("fut_rupees", "size"),
            wins=("fut_rupees", lambda s: int((s > 0).sum())),
            total_pnl=("fut_rupees", "sum"),
            avg_pnl=("fut_rupees", "mean"))
        gf["win_rate_%"] = (gf["wins"] / gf["trades"] * 100).round(1)
        st.dataframe(gf.style.format("{:,.0f}",
                     subset=["total_pnl", "avg_pnl"]),
                     use_container_width=True)

    st.subheader("Exit reasons (options)")
    er = d.copy()
    er["exit_reason"] = er["exit_reason"].astype(str).str.upper()
    pivot = er.pivot_table(index="exit_reason", columns="strategy",
                           values="pnl_rupees", aggfunc=["count", "sum"])
    st.dataframe(pivot.style.format("{:,.0f}", na_rep="—"),
                 use_container_width=True)


def page_research(opt, daily_opt, daily_fut, other):
    st.title("Research Questions — Auto-computed Evidence")
    st.caption("Each section shows the raw evidence table plus data-point "
               "count. Hypothetical PnLs assume entry at the first eligible "
               "candle close and exit at the last tracked candle — estimates, "
               "not exact fills.")

    with st.expander("A1 — Does the RSI ≥ 60 filter help or hurt?",
                     expanded=True):
        a1 = rq_a1_rsi_blocked(opt, other)
        if a1.empty:
            st.write("No RSI-blocked candidates detected yet.")
        else:
            blocked_pnl = a1["hyp_pnl_rupees"].sum()
            winners = int((a1["hyp_pnl_rupees"] > 0).sum())
            st.metric("Hypothetical PnL the filter blocked",
                      inr(blocked_pnl),
                      f"{winners}/{len(a1)} would-be winners")
            st.dataframe(a1, use_container_width=True)
            st.caption(f"Data points: {len(a1)} · Filter is net "
                       f"{'HURTING' if blocked_pnl > 0 else 'PROTECTING'} "
                       "so far (blocked PnL > 0 means it blocked profit).")

    with st.expander("A2/A3 — IMMEDIATE vs BOUNCE entry"):
        a3 = rq_a3_entry_type(opt)
        if a3.empty:
            st.write("No entry_type data yet.")
        else:
            st.dataframe(a3.style.format(
                {"total_pnl": "{:,.0f}", "avg_pnl": "{:,.0f}"}),
                use_container_width=True)

    with st.expander("B2 — Should the ceiling be ₹250 instead of ₹200?"):
        b2 = rq_b2_ceiling(other)
        if b2.empty:
            st.write("No strikes first-seen in the ₹200–250 band yet.")
        else:
            st.metric("Hypothetical PnL of ₹200–250 entries",
                      inr(b2["hyp_pnl_rupees"].sum()),
                      f"{len(b2)} candidates")
            st.dataframe(b2, use_container_width=True)

    with st.expander("D2 — Pure3: SMI_ZONE vs 1OF3_BREAK exits"):
        d2 = rq_d2_pure3_exits(opt)
        if d2.empty:
            st.write("No Pure3 closed trades yet.")
        else:
            st.dataframe(d2.style.format(
                {"total_pnl": "{:,.0f}", "avg_pnl": "{:,.0f}"}),
                use_container_width=True)

    with st.expander("D4 — Exit all strategies at SMI_ZONE time?",
                     expanded=True):
        d4 = rq_d4_smi_vs_hold(opt, other)
        if d4.empty:
            st.write("Need overlapping Pure3 SMI exits + T/V28/V29 candle "
                     "data on the same day. None matched yet.")
        else:
            net = d4["smi_better_by"].sum()
            st.metric("Net ₹ if SMI_ZONE exit had been used",
                      inr(net), f"{len(d4)} trades compared")
            st.dataframe(d4.style.format(
                {"actual_pnl": "{:,.0f}", "hyp_smi_exit_pnl": "{:,.0f}",
                 "smi_better_by": "{:,.0f}"}), use_container_width=True)

    with st.expander("E3 — Futures vs options PnL divergence", expanded=True):
        e3, n_div, n_tot = rq_e3_divergence(daily_opt, daily_fut)
        if e3.empty:
            st.write("Need both futures and options daily PnL.")
        else:
            st.metric("Divergent strategy-days",
                      f"{n_div} / {n_tot}",
                      f"{n_div / n_tot * 100:.0f}% divergence rate"
                      if n_tot else None)
            st.dataframe(e3.sort_values("date", ascending=False),
                         use_container_width=True)

    with st.expander("F3 — When price filter blocks ALL strikes, "
                     "is direction usually wrong?"):
        f3 = rq_f3_futures_only(opt, daily_fut)
        if f3.empty:
            st.write("No FUTURES_ONLY signals found yet.")
        else:
            right = int((f3["direction_right"] == "YES").sum())
            st.metric("Direction right on blocked days",
                      f"{right} / {len(f3)}")
            st.dataframe(f3, use_container_width=True)

    with st.expander("V29 vs T/V28 — daily better/worse tally"):
        tally, streak, side = v29_tally(daily_opt)
        if tally.empty:
            st.write("Need V29 + T/V28 daily options PnL.")
        else:
            st.dataframe(tally[["V29", "tv28_avg", "verdict"]]
                         .sort_index(ascending=False)
                         .style.format("{:,.0f}", subset=["V29", "tv28_avg"]),
                         use_container_width=True)
            st.caption(f"Current streak: {streak} consecutive {side} days")


def page_signal_detail(opt, other):
    st.title("Signal Detail")
    edl = other.get("entry_decision_log", pd.DataFrame())
    dates = set()
    if not opt.empty:
        dates |= set(opt["trade_date"].dropna())
    tc = col(edl, "timestamp", "signal_time") if not edl.empty else None
    if tc:
        edl = edl.copy()
        edl["d"] = to_dt(edl[tc]).dt.date
        dates |= set(edl["d"].dropna())
    if not dates:
        st.info("No signal data yet.")
        return
    day = st.date_input("Pick a trading day", value=max(dates),
                        min_value=min(dates), max_value=max(dates))

    if tc:
        e = edl[edl["d"] == day]
        st.subheader(f"Signals fired on {day}")
        if e.empty:
            st.write("No signals logged.")
        else:
            show = [c for c in e.columns if c not in ("d",)]
            st.dataframe(e[show], use_container_width=True)
            fcols = [c for c in e.columns if c.startswith("filter_")]
            if fcols:
                st.caption("Filter pass rates this day")
                st.dataframe(e[fcols].apply(
                    lambda s: s.astype(str).str.upper()
                    .isin(["TRUE", "PASS", "1", "YES"]).mean() * 100
                ).round(0).to_frame("pass %"))

    if not opt.empty:
        t = opt[opt["trade_date"] == day]
        st.subheader("Options trades")
        if t.empty:
            st.write("No options trades this day.")
        else:
            show = [c for c in ["strategy", "signal_dt", "option_symbol",
                                "selected_strike", "strike_type",
                                "expiry_track", "entry_type",
                                "entry_option_price", "exit_option_price",
                                "exit_reason", "pnl_rupees", "is_reentry"]
                    if c in t.columns]
            st.dataframe(t[show], use_container_width=True)

    ocl = other.get("options_candle_log", pd.DataFrame())
    if not ocl.empty:
        c = ocl.copy()
        tcc = col(c, "candle_time")
        if tcc:
            c["d"] = to_dt(c[tcc]).dt.date
            cd = c[c["d"] == day]
            if not cd.empty and "option_symbol" in cd.columns:
                st.subheader("Option candle tracking (with-filter vs "
                             "no-filter view)")
                sym = st.selectbox("Strike",
                                   sorted(cd["option_symbol"].dropna()
                                          .unique()))
                s = cd[cd["option_symbol"] == sym].sort_values(tcc)
                if "option_close" in s.columns:
                    fig = px.line(s, x=tcc, y="option_close",
                                  title=f"{sym} — {day}")
                    fig.add_hrect(y0=PRICE_FLOOR, y1=PRICE_CEIL,
                                  fillcolor="green", opacity=0.08,
                                  annotation_text="entry band ₹150–200")
                    st.plotly_chart(fig, use_container_width=True)
                st.dataframe(s, use_container_width=True)


def page_capital(daily_opt, daily_fut):
    st.title("Capital Tracker")
    if daily_opt.empty:
        st.info("No PnL data yet.")
        return
    opt_cum = capital_curve(daily_opt)
    combined_daily = daily_opt.add(daily_fut, fill_value=0) \
        if not daily_fut.empty else daily_opt
    comb_cum = capital_curve(combined_daily)

    total_opt = opt_cum.iloc[-1].sum() if not opt_cum.empty else 0
    total_comb = comb_cum.iloc[-1].sum() if not comb_cum.empty else 0
    n_strats = len(opt_cum.columns) if not opt_cum.empty else 0
    base = START_CAPITAL * n_strats

    a, b, c = st.columns(3)
    a.metric("Options-only (all strategies)", inr(total_opt),
             inr(total_opt - base))
    b.metric("Combined futures + options", inr(total_comb),
             inr(total_comb - base))
    c.metric("Starting base", inr(base),
             f"{n_strats} × ₹30,000")

    fig = go.Figure()
    for s in [s for s in STRATS if s in comb_cum.columns]:
        fig.add_trace(go.Scatter(x=comb_cum.index, y=comb_cum[s], name=s,
                                 line=dict(color=STRAT_COLORS[s])))
    fig.update_layout(title="Combined capital curve (futures + options)",
                      height=420, yaxis_title="₹", hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Milestones (best single strategy, options-only)")
    best = opt_cum.max(axis=1).max() if not opt_cum.empty else START_CAPITAL
    rows = []
    for m in MILESTONES:
        rows.append({"milestone": inr(m),
                     "status": "✅ reached" if best >= m else
                     f"{best / m * 100:.1f}% there"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True,
                 hide_index=True)
    st.caption("Go/no-go review: November 2026 · Capital arrangement: "
               "March 2027+")


# ---------------------------------------------------------------- main


def main():
    st.sidebar.title("📈 NiftyBot")
    page = st.sidebar.radio("Page", [
        "Overview", "Daily Log", "Strategy Comparison",
        "Research Questions", "Signal Detail", "Capital Tracker"])
    if st.sidebar.button("🔄 Force refresh"):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Sheet re-read every 60s · "
                       f"{datetime.now():%d %b %Y %H:%M}")

    try:
        opt, fut, daily_opt, daily_fut, other = build_datasets()
    except KeyError:
        st.error("Google service-account secret not found. Add it under "
                 "`[gcp_service_account]` in Streamlit secrets — see "
                 "README_DEPLOY.md.")
        st.stop()
    except Exception as e:
        st.error(f"Could not read the Google Sheet: {e}")
        st.stop()

    if page == "Overview":
        page_overview(opt, daily_opt, daily_fut)
    elif page == "Daily Log":
        page_daily_log(daily_opt, daily_fut, other)
    elif page == "Strategy Comparison":
        page_strategy_comparison(opt, fut)
    elif page == "Research Questions":
        page_research(opt, daily_opt, daily_fut, other)
    elif page == "Signal Detail":
        page_signal_detail(opt, other)
    elif page == "Capital Tracker":
        page_capital(daily_opt, daily_fut)


if __name__ == "__main__":
    main()
