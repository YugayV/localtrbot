import os
import threading
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import multi_pairs_bot as botmod


st.set_page_config(page_title="LocalTRBot", layout="wide")


@st.cache_resource
def _start_background():
    print("[DASH] Streamlit dashboard mode: no bot logic running here", flush=True)
    return {"started_at": time.time()}


def _get_positions_df():
    try:
        botmod.account.load_state()
    except Exception:
        pass
    return pd.DataFrame(list(botmod.account.positions or []))


def _get_trades_df():
    try:
        botmod.account.load_state()
    except Exception:
        pass
    return pd.DataFrame(list(reversed((botmod.account.trades or [])[-100:])))


@st.cache_data(ttl=30, show_spinner=False)
def _get_pair_history(pair: str, tf: str):
    df, tf_norm = botmod.get_history(pair, tf=tf)
    df = df.tail(2000)
    return df, tf_norm


@st.cache_data(ttl=10, show_spinner=False)
def _intraday_signal_and_plan(pair: str, tf_norm: str):
    ticker = botmod.PAIRS[pair]

    sig = 0
    reasons = []
    ind = None
    confirm = None
    plan = {"direction": 0}

    if tf_norm == "15m":
        sig, reasons, ind15, ind1h = botmod.get_intraday_signal(pair, ticker, enforce_hours=False)
        ind = ind15
        if ind1h is not None:
            confirm = {"tf": "1h", "rsi": ind1h.get("rsi"), "trend": ind1h.get("trend"), "price": ind1h.get("price")}

        if ind is not None and hasattr(botmod, "compute_intraday_plan"):
            plan = botmod.compute_intraday_plan(pair, ticker, sig, entry_price=float(ind.get("price")))
        elif ind is not None and sig in [1, -1]:
            sl_dist, tp_dist = botmod.get_sl_tp_distance(pair)
            px = float(ind.get("price"))
            if sig == 1:
                plan = {"direction": 1, "entry": px, "sl": px - sl_dist, "tp": px + tp_dist, "rr": (tp_dist / sl_dist) if sl_dist else None}
            else:
                plan = {"direction": -1, "entry": px, "sl": px + sl_dist, "tp": px - tp_dist, "rr": (tp_dist / sl_dist) if sl_dist else None}

        return sig, reasons, ind, confirm, plan

    tail = botmod.get_data(ticker, period="60d", interval="1h")
    if tail is None or tail.empty:
        return 0, ["No data"], {"price": 0.0, "rsi": 0.0, "trend": 0}, None, {"direction": 0}

    ind = botmod.get_indicators(tail, pair)
    sig, reasons = botmod.check_signal(ind, enforce_hours=False)

    if sig in [1, -1]:
        sl_dist, tp_dist = botmod.get_sl_tp_distance(pair)
        px = float(ind.get("price"))
        if sig == 1:
            plan = {"direction": 1, "entry": px, "sl": px - sl_dist, "tp": px + tp_dist, "rr": (tp_dist / sl_dist) if sl_dist else None}
        else:
            plan = {"direction": -1, "entry": px, "sl": px + sl_dist, "tp": px - tp_dist, "rr": (tp_dist / sl_dist) if sl_dist else None}

    return sig, reasons, ind, None, plan


bg = _start_background()

def _fmt_ts(ts):
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return str(ts)

st.title("LocalTRBot — Streamlit Dashboard")

if st.button("Refresh data", width="stretch"):
    st.cache_data.clear()
    st.rerun()

st.caption(
    "Dashboard mode: bot logic runs in separate worker process | "
    f"last_open={_fmt_ts(rt.get('auto_trade_last_open_ts'))} {rt.get('auto_trade_last_open_pair') or ''}"
)

if rt.get("auto_trade_last_error"):
    st.error(f"Auto-trade error: {rt['auto_trade_last_error']}")
if rt.get("bot_poll_last_error"):
    st.error(f"Telegram polling error: {rt['bot_poll_last_error']}")

if botmod.WEBHOOK_BASE_URL and botmod.TELEGRAM_WEBHOOK_SECRET:
    st.warning("WEBHOOK_* переменные заданы, но Streamlit не принимает /telegram/<secret>. Либо убери WEBHOOK env и используй polling, либо запускай встроенный HTTP-сервер вместо Streamlit.")

col_left, col_right = st.columns([2, 1], gap="large")

with col_right:
    st.subheader("Control Panel")
    cfg = botmod.get_public_config()

    st.subheader("Trading")

    paused_reason = (rt.get("trading_paused_reason") if isinstance(rt, dict) else None) or ""
    enabled_now = bool((cfg or {}).get("auto_trade_enabled", True))

    day_start = (rt.get("day_start_balance") if isinstance(rt, dict) else None)
    try:
        day_start_f = float(day_start) if day_start is not None else None
    except Exception:
        day_start_f = None
    daily_pnl = (float(botmod.account.balance) - day_start_f) if day_start_f else None

    st.caption(f"Trading: {'ON' if enabled_now else 'OFF'}")
    st.caption(f"Paused reason: {paused_reason or '—'}")
    if daily_pnl is not None and day_start_f:
        st.caption(f"Today PnL: {daily_pnl:+.2f} ({(daily_pnl / day_start_f * 100.0):+.2f}%)")

    last_cand = (rt.get("auto_trade_last_candidates") if isinstance(rt, dict) else None)
    last_sample = (rt.get("auto_trade_last_sample") if isinstance(rt, dict) else None)
    if last_cand is not None:
        st.caption(f"Last scan: candidates={last_cand}")
    if last_sample:
        st.caption(f"Sample: {last_sample}")

    if st.button("START trading" if not enabled_now else "STOP trading", width="stretch"):
        if enabled_now:
            botmod.set_auto_trade_enabled(False, reason="MANUAL PAUSE")
            st.success("Trading stopped")
        else:
            botmod.set_auto_trade_enabled(True, reason=None)
            st.success("Trading started")
        st.rerun()

    if st.button("Close all open positions", width="stretch"):
        closed = botmod.close_all_positions(reason="MANUAL CLOSE")
        st.success(f"Closed: {len(closed)}")
        st.rerun()

    st.subheader("Settings")
    trades_per_pair = st.number_input("Trades per pair", min_value=0, max_value=20, value=int(cfg["trades_per_pair"]), step=1)
    max_total_positions = st.number_input("Max total positions", min_value=0, max_value=50, value=int(cfg.get("max_total_positions", 10)), step=1)
    sl_atr_multiplier = st.number_input("Stop loss (ATR x)", min_value=0.1, max_value=20.0, value=float(cfg.get("sl_atr_multiplier", 2.0)), step=0.1)
    tp_atr_multiplier = st.number_input("Take profit (ATR x)", min_value=0.1, max_value=50.0, value=float(cfg.get("tp_atr_multiplier", 6.0)), step=0.1)
    leverage = st.number_input("Leverage", min_value=0.1, max_value=1000.0, value=float(cfg["leverage"]), step=1.0)
    check_interval = st.number_input("Check interval (sec)", min_value=5, value=int(cfg["check_interval"]), step=5)

    goya_score_enabled = st.selectbox("GoyaScore enabled", ["true", "false"], index=0 if bool(cfg.get("goya_score_enabled", True)) else 1)
    goya_min_score = st.number_input("Goya min score (0..100)", min_value=0, max_value=100, value=int(cfg.get("goya_min_score", 35)), step=1)

    if st.button("Save settings", width="stretch"):
        botmod.apply_config_patch(
            {
                "trades_per_pair": trades_per_pair,
                "max_total_positions": max_total_positions,
                "sl_atr_multiplier": sl_atr_multiplier,
                "tp_atr_multiplier": tp_atr_multiplier,
                "leverage": leverage,
                "check_interval": check_interval,
                "goya_score_enabled": goya_score_enabled,
                "goya_min_score": goya_min_score,
            }
        )
        st.success("Saved")

with col_left:
    pairs = list(botmod.PAIRS.keys())
    pair = st.selectbox("Pair", pairs, index=0)
    tf = st.selectbox("TF", ["15m", "1h"], index=0)

    try:
        with st.spinner("Loading market data..."):
            df, tf_norm = _get_pair_history(pair, tf)
            sig, reasons, ind, confirm, plan = _intraday_signal_and_plan(pair, tf_norm)
    except Exception as e:
        st.error(f"Data load error: {e}")
        st.stop()

    sig_txt = "NO SIGNAL"
    if sig == 1:
        sig_txt = "BUY"
    elif sig == -1:
        sig_txt = "SELL"

    st.markdown(f"**{pair} • TF {tf_norm} • {sig_txt}**")

    goya_line = None
    if reasons:
        try:
            goya_line = next((r for r in reasons if isinstance(r, str) and r.startswith("GoyaScore:")), None)
        except Exception:
            goya_line = None
    if goya_line:
        st.caption(goya_line)

    if plan and plan.get("direction") in [1, -1]:
        rr = plan.get("rr")
        rr_txt = f"{float(rr):.2f}" if isinstance(rr, (int, float)) else "—"
        st.info(f"Plan: {'LONG' if plan['direction']==1 else 'SHORT'} Entry {plan['entry']:.5f} • SL {plan['sl']:.5f} • TP {plan['tp']:.5f} • RR {rr_txt}")
    else:
        st.info("Plan: —")

    fig = go.Figure()
    if not df.empty:
        fig.add_trace(
            go.Candlestick(
                x=df.index,
                open=df["Open"],
                high=df["High"],
                low=df["Low"],
                close=df["Close"],
                name="Price",
            )
        )


    fig.update_layout(height=650, margin=dict(l=10, r=10, t=30, b=10), xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Open Positions")
        st.dataframe(_get_positions_df(), width="stretch", hide_index=True)
    with c2:
        st.subheader("Last Trades")
        st.dataframe(_get_trades_df(), width="stretch", hide_index=True)