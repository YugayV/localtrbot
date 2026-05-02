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
    def _run_auto():
        botmod.auto_trade()

    def _run_poll():
        try:
            botmod.bot.remove_webhook()
        except Exception:
            pass
        botmod.run_bot_polling()

    def _run_data_updater():
        while True:
            try:
                for p in list(botmod.PAIRS.keys()):
                    botmod.update_market_data(p, tf="15m", bars=1500, min_age_sec=180)
                    botmod.update_market_data(p, tf="1h", bars=2000, min_age_sec=300)
                    time.sleep(0.2)
            except Exception:
                pass
            time.sleep(30)

    t1 = threading.Thread(target=_run_auto, daemon=True)
    t1.start()

    t2 = threading.Thread(target=_run_poll, daemon=True)
    t2.start()

    t3 = threading.Thread(target=_run_data_updater, daemon=True)
    t3.start()

    return {"started_at": time.time(), "auto_thread": t1, "poll_thread": t2, "data_thread": t3}


def _get_positions_df():
    return pd.DataFrame(list(botmod.account.positions or []))


def _get_trades_df():
    return pd.DataFrame(list(reversed((botmod.account.trades or [])[-100:])))


@st.cache_data(ttl=30, show_spinner=False)
def _get_pair_history(pair: str, tf: str, zz: float):
    df, tf_norm = botmod.get_history(pair, tf=tf)
    df = df.tail(2000)
    candles = botmod._df_to_candles(df)
    swings = botmod._zigzag_swings(candles, botmod._history_threshold(pair, tf_norm) * float(zz))[-12:]
    impulse = botmod._elliott_impulse_check(swings)
    swings = botmod._apply_impulse_labels(swings, impulse)
    return df, tf_norm, swings, impulse


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

auto_alive = bool(bg.get("auto_thread")) and bg["auto_thread"].is_alive()
poll_alive = bool(bg.get("poll_thread")) and bg["poll_thread"].is_alive()
data_alive = bool(bg.get("data_thread")) and bg["data_thread"].is_alive()
rt = getattr(botmod, "RUNTIME", {}) or {}

st.caption(
    f"Background: auto={auto_alive} polling={poll_alive} data={data_alive} | "
    f"last_loop={_fmt_ts(rt.get('auto_trade_last_loop_ts'))} | "
    f"last_cycle={_fmt_ts(rt.get('auto_trade_last_cycle_ts'))} | "
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

    risk_per_trade = st.number_input("Risk per trade (%)", min_value=0.1, max_value=100.0, value=float(cfg["risk_per_trade"]), step=0.1)
    trades_per_pair = st.number_input("Trades per pair", min_value=0, max_value=20, value=int(cfg["trades_per_pair"]), step=1)
    sl_pips = st.number_input("SL (pips / $)", min_value=0.01, value=float(cfg["sl_pips"]), step=0.01)
    tp_pips = st.number_input("TP (pips / $)", min_value=0.01, value=float(cfg["tp_pips"]), step=0.01)
    leverage = st.number_input("Leverage", min_value=0.1, max_value=1000.0, value=float(cfg["leverage"]), step=1.0)
    check_interval = st.number_input("Check interval (sec)", min_value=5, value=int(cfg["check_interval"]), step=5)
    auto_trade_enabled = st.selectbox("Auto-trade enabled", ["true", "false"], index=0 if bool(cfg["auto_trade_enabled"]) else 1)

    if st.button("Save settings", width="stretch"):
        botmod.apply_config_patch(
            {
                "risk_per_trade": risk_per_trade,
                "trades_per_pair": trades_per_pair,
                "sl_pips": sl_pips,
                "tp_pips": tp_pips,
                "leverage": leverage,
                "check_interval": check_interval,
                "auto_trade_enabled": auto_trade_enabled,
            }
        )
        st.success("Saved")

    with st.expander("Data & Model", expanded=False):
        st.caption("Скачивает данные в ./data и сохраняет модель в ./models (в Railway хранилище может быть временным)")

        pair_dm = st.selectbox("Pair", list(botmod.PAIRS.keys()), key="dm_pair")
        tf_dm = st.selectbox("TF", ["15m", "1h", "1d", "1wk"], key="dm_tf")

        if st.button("Update data", width="stretch"):
            try:
                df_u = botmod.update_market_data(pair_dm, tf=tf_dm, bars=3000)
                if df_u is None or df_u.empty:
                    st.error("No data downloaded")
                else:
                    st.success(f"OK: {len(df_u)} rows")
            except Exception as e:
                st.error(f"Update error: {e}")

        if st.button("Train 15m model", width="stretch"):
            try:
                m = botmod.train_direction_model(pair_dm, tf="15m", bars=5000)
                if not m:
                    st.error("Training failed (not enough data)")
                else:
                    acc = float((m.get("metrics") or {}).get("acc") or 0)
                    n = int((m.get("metrics") or {}).get("n") or 0)
                    st.success(f"Trained: acc={acc:.2f} n={n}")
            except Exception as e:
                st.error(f"Train error: {e}")

    st.divider()
    st.subheader("Account")
    st.write(botmod.account.stats())

with col_left:
    pairs = list(botmod.PAIRS.keys())
    pair = st.selectbox("Pair", pairs, index=0)
    tf = st.selectbox("TF", ["15m", "1h"], index=0)
    zz = st.slider("ZigZag sensitivity (x)", min_value=0.5, max_value=3.0, value=1.0, step=0.1)
    only_valid = st.checkbox("Only valid impulse", value=False)

    try:
        with st.spinner("Loading market data..."):
            df, tf_norm, swings, impulse = _get_pair_history(pair, tf, zz)
            sig, reasons, ind, confirm, plan = _intraday_signal_and_plan(pair, tf_norm)
    except Exception as e:
        st.error(f"Data load error: {e}")
        st.stop()

    sig_txt = "NO SIGNAL"
    if sig == 1:
        sig_txt = "BUY"
    elif sig == -1:
        sig_txt = "SELL"

    imp_txt = "Impulse: —"
    if impulse:
        imp_txt = "Impulse OK" if impulse.get("ok") else "Impulse INVALID: " + "; ".join(impulse.get("errors") or ["rules"])

    st.markdown(f"**{pair} • TF {tf_norm} • {sig_txt}**")
    st.caption(imp_txt)

    if confirm:
        st.caption(f"Confirm(1h): RSI {float(confirm.get('rsi', 0) or 0):.1f} • Trend {confirm.get('trend')}")

    if reasons:
        with st.expander("Signal reasons"):
            st.write(reasons)

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

    if swings and not (only_valid and impulse and impulse.get("ok") is False):
        xs = []
        ys = []
        texts = []
        for p in swings:
            t = pd.to_datetime(int(p["time"]), unit="s", utc=True).tz_convert(None)
            xs.append(t)
            ys.append(float(p["price"]))
            texts.append(p.get("label") or "")

        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers+text", text=texts, textposition="top center", name="Waves"))

    fig.update_layout(height=650, margin=dict(l=10, r=10, t=30, b=10), xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Open Positions")
        st.dataframe(_get_positions_df(), width="stretch", hide_index=True)
    with c2:
        st.subheader("Last Trades")
        st.dataframe(_get_trades_df(), width="stretch", hide_index=True)