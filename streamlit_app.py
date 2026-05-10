import os
import threading
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import multi_pairs_bot as botmod


st.set_page_config(page_title="LocalTRBot", layout="wide")

st.markdown(
    """
<style>
:root { --bg:#0b1220; --card:#0f1a2b; --muted:#9fb0c6; --text:#e8eef8; --line:#1e2a40; }
.block-container { padding-top: 1.2rem; }
[data-testid="stMetric"] { background: var(--card); border: 1px solid var(--line); padding: 12px 14px; border-radius: 14px; }
.kpi-row { display:flex; gap:8px; flex-wrap:wrap; margin: 6px 0 2px 0; }
.pill { display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius: 999px; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); color: var(--text); font-size: 12.5px; }
.dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.dot-green{ background:#00D18F; }
.dot-red{ background:#FF4D4D; }
.dot-amber{ background:#FFB020; }
.dot-blue{ background:#4DA3FF; }
.dot-gray{ background:#7A8CA6; }
.hdr { background: linear-gradient(90deg, rgba(77,163,255,0.16), rgba(0,209,143,0.10)); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 14px 16px; }
.hdr h1 { margin:0; font-size: 22px; }
.hdr p { margin: 4px 0 0 0; color: var(--muted); font-size: 13px; }
</style>
""",
    unsafe_allow_html=True,
)


def _dot_class(kind: str) -> str:
    k = (kind or "").lower().strip()
    if k in ["green", "up", "buy", "on", "pos"]:
        return "dot dot-green"
    if k in ["red", "down", "sell", "off", "neg"]:
        return "dot dot-red"
    if k in ["amber", "warn"]:
        return "dot dot-amber"
    if k in ["blue", "info"]:
        return "dot dot-blue"
    return "dot dot-gray"


def _pill(label: str, value: str, kind: str = "gray") -> str:
    return f"<span class='pill'><span class='{_dot_class(kind)}'></span><span><b>{label}</b>: {value}</span></span>"


def _render_pills(items):
    html = "<div class='kpi-row'>" + "".join(items) + "</div>"
    st.markdown(html, unsafe_allow_html=True)


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
try:
    botmod.account.load_state()
except Exception:
    pass
rt = getattr(getattr(botmod, "account", None), "runtime", {}) or {}

def _fmt_ts(ts):
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return str(ts)

cfg = botmod.get_public_config()

st.markdown(
    """
<div class="hdr">
  <h1>LocalTRBot — Dashboard</h1>
  <p>Worker‑режим: торговля и Telegram работают отдельно • last_open: {last_open} • worker_ts: {worker_ts}</p>
</div>
""".format(
        last_open=(
            f"{_fmt_ts(rt.get('auto_trade_last_open_ts'))} {rt.get('auto_trade_last_open_pair') or ''}".strip()
            or "—"
        ),
        worker_ts=_fmt_ts(rt.get("ts")) if isinstance(rt, dict) else "—",
    ),
    unsafe_allow_html=True,
)

c_hdr1, c_hdr2, c_hdr3 = st.columns([1.2, 1.2, 1], gap="small")
with c_hdr1:
    if st.button("Обновить данные", width="stretch"):
        st.cache_data.clear()
        st.rerun()
with c_hdr2:
    st.caption(f"Последний скан: candidates={rt.get('auto_trade_last_candidates') if isinstance(rt, dict) else '—'}")
with c_hdr3:
    st.caption(f"Sample: {rt.get('auto_trade_last_sample') if isinstance(rt, dict) else '—'}")

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

    with st.expander("Настройки", expanded=True):
        cset1, cset2 = st.columns(2)
        with cset1:
            trades_per_pair = st.number_input("Trades per pair", min_value=0, max_value=20, value=int(cfg["trades_per_pair"]), step=1)
            max_total_positions = st.number_input("Max total positions", min_value=0, max_value=50, value=int(cfg.get("max_total_positions", 10)), step=1)
            goya_score_enabled = st.selectbox("VitalityScore", ["true", "false"], index=0 if bool(cfg.get("goya_score_enabled", True)) else 1)
            goya_min_score = st.number_input("Vitality min score", min_value=0, max_value=100, value=int(cfg.get("goya_min_score", 35)), step=1)
            deepseek_enabled = st.selectbox("DeepSeek (AI фильтр)", ["false", "true"], index=1 if bool(cfg.get("deepseek_enabled", False)) else 0)
            show_ai_indicators = st.selectbox("Показывать AI индикаторы", ["false", "true"], index=1 if bool(st.session_state.get("show_ai_indicators", False)) else 0)
            st.session_state["show_ai_indicators"] = (show_ai_indicators == "true")
        with cset2:
            sl_atr_multiplier = st.number_input("SL (ATR x)", min_value=0.1, max_value=20.0, value=float(cfg.get("sl_atr_multiplier", 2.0)), step=0.1)
            tp_atr_multiplier = st.number_input("TP (ATR x)", min_value=0.1, max_value=50.0, value=float(cfg.get("tp_atr_multiplier", 6.0)), step=0.1)
            leverage = st.number_input("Leverage", min_value=0.1, max_value=1000.0, value=float(cfg["leverage"]), step=1.0)
            check_interval = st.number_input("Check interval (sec)", min_value=5, value=int(cfg["check_interval"]), step=5)
            backtest_commission_bps = st.number_input("Backtest fee (bps)", min_value=0.0, max_value=200.0, value=float(cfg.get("backtest_commission_bps", 0.0) or 0.0), step=0.1)

        if st.button("Сохранить", width="stretch"):
            botmod.apply_config_patch(
                {
                    "trades_per_pair": trades_per_pair,
                    "max_total_positions": max_total_positions,
                    "sl_atr_multiplier": sl_atr_multiplier,
                    "tp_atr_multiplier": tp_atr_multiplier,
                    "leverage": leverage,
                    "check_interval": check_interval,
                    "backtest_commission_bps": backtest_commission_bps,
                    "goya_score_enabled": goya_score_enabled,
                    "goya_min_score": goya_min_score,
                    "deepseek_enabled": deepseek_enabled,
                }
            )
            st.success("Сохранено")

with col_left:
    pairs = list(botmod.PAIRS.keys())
    sel1, sel2 = st.columns([1.3, 0.7], gap="small")
    with sel1:
        pair = st.selectbox("Пара", pairs, index=0)
    with sel2:
        tf = st.selectbox("Таймфрейм", ["15m", "1h"], index=0)

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

    price = float((ind or {}).get("price") or 0.0) if isinstance(ind, dict) else 0.0
    change = float((ind or {}).get("change") or 0.0) if isinstance(ind, dict) else 0.0
    rsi = float((ind or {}).get("rsi") or 0.0) if isinstance(ind, dict) else 0.0
    trend = int((ind or {}).get("trend") or 0) if isinstance(ind, dict) else 0
    vitality_sc = None
    try:
        vitality_sc = int((ind or {}).get("goya_score"))
    except Exception:
        vitality_sc = None

    m1, m2, m3, m4 = st.columns([1.1, 1.1, 1.1, 1.1], gap="small")
    with m1:
        st.metric("Цена", f"{price:.5f}" if pair not in (getattr(botmod, "CRYPTO_PAIRS", set()) or set()) else f"{price:.2f}")
    with m2:
        st.metric("Изменение", f"{change:+.2f}%")
    with m3:
        st.metric("RSI", f"{rsi:.0f}")
    with m4:
        st.metric("VitalityScore", (f"{int(vitality_sc):+d}" if isinstance(vitality_sc, int) else "—"))

    st.markdown(f"**{pair} • {tf_norm} • {sig_txt}**")

    pills = []
    pills.append(_pill("Сигнал", sig_txt, "green" if sig == 1 else "red" if sig == -1 else "gray"))
    pills.append(_pill("Тренд", "UP" if trend == 1 else "DOWN" if trend == -1 else "—", "green" if trend == 1 else "red" if trend == -1 else "gray"))
    if isinstance(vitality_sc, int):
        pills.append(_pill("Vitality", f"{int(vitality_sc):+d}", "green" if vitality_sc > 0 else "red" if vitality_sc < 0 else "gray"))
    pills.append(_pill("Trading", "ON" if bool((cfg or {}).get("auto_trade_enabled", True)) else "OFF", "green" if bool((cfg or {}).get("auto_trade_enabled", True)) else "red"))
    if rt.get("auto_trade_last_candidates") is not None:
        pills.append(_pill("Candidates", str(rt.get("auto_trade_last_candidates")), "blue"))
    _render_pills(pills)

    if bool(st.session_state.get("show_ai_indicators", False)):
        ind_atr = float((ind or {}).get("atr") or 0.0) if isinstance(ind, dict) else 0.0
        ds_line = None
        model_line = None
        try:
            ds_line = next((r for r in (reasons or []) if isinstance(r, str) and r.startswith("DeepSeekScore:")), None)
        except Exception:
            ds_line = None
        try:
            model_line = next((r for r in (reasons or []) if isinstance(r, str) and r.startswith("Model p(up)=")), None)
        except Exception:
            model_line = None

        extra = []
        extra.append(_pill("ATR", f"{ind_atr:.5f}" if pair not in (getattr(botmod, "CRYPTO_PAIRS", set()) or set()) else f"{ind_atr:.2f}", "blue"))
        if model_line:
            extra.append(_pill("Model", model_line.replace("Model ", ""), "amber"))
        if ds_line:
            extra.append(_pill("DeepSeek", ds_line.replace("DeepSeekScore:", "").strip(), "amber"))
        _render_pills(extra)

    if plan and plan.get("direction") in [1, -1]:
        rr = plan.get("rr")
        rr_txt = f"{float(rr):.2f}" if isinstance(rr, (int, float)) else "—"
        st.info(f"Plan: {'LONG' if plan['direction']==1 else 'SHORT'} Entry {plan['entry']:.5f} • SL {plan['sl']:.5f} • TP {plan['tp']:.5f} • RR {rr_txt}")
    else:
        st.info("Plan: —")

    with st.expander("Бэктест и оптимизация", expanded=False):
        st.caption("Идея из статьи: быстро проверить стратегию на истории, затем перебрать варианты и сравнить метрики (Sortino/MaxDD/PF).")

        strat = st.selectbox("Стратегия", ["MACD+RSI (моментум)", "Bollinger (mean-reversion)"], index=0)

        fee_bps = float((cfg or {}).get("backtest_commission_bps", 0.0) or 0.0)
        st.caption(f"Комиссия (bps): {fee_bps}")

        sl = float((cfg or {}).get("sl_atr_multiplier", 2.0))
        tp = float((cfg or {}).get("tp_atr_multiplier", 6.0))
        tr_on = bool((cfg or {}).get("trailing_stop", True))
        tr = float((cfg or {}).get("trailing_stop_atr_multiplier", 1.5)) if tr_on else None

        if strat.startswith("MACD"):
            bt_rsi_min = st.slider("RSI порог (вход)", min_value=40, max_value=60, value=50, step=1)
            c1, c2, c3 = st.columns(3)
            with c1:
                macd_fast = st.number_input("MACD fast", min_value=2, max_value=50, value=12, step=1)
            with c2:
                macd_slow = st.number_input("MACD slow", min_value=3, max_value=80, value=26, step=1)
            with c3:
                macd_sig = st.number_input("MACD signal", min_value=2, max_value=30, value=9, step=1)
        else:
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                bb_period = st.number_input("BB period", min_value=10, max_value=60, value=20, step=1)
            with c2:
                bb_mult = st.number_input("BB mult", min_value=0.8, max_value=4.0, value=2.0, step=0.1)
            with c3:
                rsi_low = st.number_input("RSI low", min_value=10.0, max_value=50.0, value=40.0, step=1.0)
            with c4:
                rsi_high = st.number_input("RSI high", min_value=50.0, max_value=90.0, value=60.0, step=1.0)

        mode = st.selectbox("Режим", ["Быстрый бэктест", "Walk-forward (anti overfit)"], index=0)
        wf_train = 400
        wf_test = 120
        wf_step = 120
        if mode.startswith("Walk"):
            c_wf1, c_wf2, c_wf3 = st.columns(3)
            with c_wf1:
                wf_train = st.number_input("Train bars", min_value=100, max_value=3000, value=400, step=50)
            with c_wf2:
                wf_test = st.number_input("Test bars", min_value=50, max_value=2000, value=120, step=25)
            with c_wf3:
                wf_step = st.number_input("Step bars", min_value=25, max_value=2000, value=120, step=25)

        b1, b2, b3 = st.columns(3)
        with b1:
            run_bt = st.button("Запустить бэктест", width="stretch")
        with b2:
            run_opt = st.button("Оптимизировать (топ-5)", width="stretch")
        with b3:
            run_wf = st.button("Walk-forward", width="stretch")

        if run_bt:
            try:
                if strat.startswith("MACD"):
                    bt = botmod.backtest_macd_rsi(
                        df,
                        rsi_min=float(bt_rsi_min),
                        macd_fast=int(macd_fast),
                        macd_slow=int(macd_slow),
                        macd_signal=int(macd_sig),
                        sl_atr_mult=sl,
                        tp_atr_mult=tp,
                        trailing_atr_mult=tr,
                        commission_bps=float(fee_bps),
                    )
                    params = {"strategy": "MACD_RSI", "rsi_min": float(bt_rsi_min), "macd": f"{int(macd_fast)},{int(macd_slow)},{int(macd_sig)}", "sl_atr": float(sl), "tp_atr": float(tp), "trail_atr": tr}
                else:
                    bt = botmod.backtest_bbands_meanrev(
                        df,
                        bb_period=int(bb_period),
                        bb_mult=float(bb_mult),
                        rsi_low=float(rsi_low),
                        rsi_high=float(rsi_high),
                        sl_atr_mult=sl,
                        tp_atr_mult=max(3.0, float(tp) * 0.7),
                        trailing_atr_mult=tr,
                        commission_bps=float(fee_bps),
                    )
                    params = {"strategy": "BB_MEANREV", "bb_period": int(bb_period), "bb_mult": float(bb_mult), "rsi_low": float(rsi_low), "rsi_high": float(rsi_high), "sl_atr": float(sl), "tp_atr": max(3.0, float(tp) * 0.7), "trail_atr": tr}

                mt = botmod.backtest_metrics(bt)
                if not mt.get("ok"):
                    st.error(f"Backtest error: {mt.get('error')}")
                else:
                    st.dataframe(pd.DataFrame([mt]), width="stretch", hide_index=True)

                    eq = pd.DataFrame(bt.get("equity") or [])
                    if not eq.empty and "t" in eq.columns:
                        eq["t"] = pd.to_datetime(eq["t"])
                        fig2 = go.Figure()
                        fig2.add_trace(go.Scatter(x=eq["t"], y=eq["equity"], mode="lines", name="Equity"))
                        fig2.update_layout(template="plotly_dark", height=260, margin=dict(l=10, r=10, t=10, b=10))
                        st.plotly_chart(fig2, width="stretch")

                    trd = pd.DataFrame([t for t in (bt.get("trades") or []) if t.get("status") != "OPEN"])
                    if not trd.empty:
                        st.dataframe(trd.tail(50), width="stretch", hide_index=True)

                    if hasattr(botmod, "variant_report"):
                        rep = botmod.variant_report(bt, params=params, strategy=str(params.get("strategy")))
                        with st.expander("Отчёт по варианту", expanded=False):
                            st.json(rep)
            except Exception as e:
                st.error(f"Backtest error: {e}")

        if run_opt:
            try:
                if strat.startswith("MACD"):
                    top = botmod.optimize_macd_rsi(df, commission_bps=float(fee_bps), top_n=5)
                else:
                    top = botmod.optimize_bbands_meanrev(df, commission_bps=float(fee_bps), top_n=5)

                if not top:
                    st.warning("Нет результатов")
                else:
                    st.dataframe(pd.DataFrame(top), width="stretch", hide_index=True)
            except Exception as e:
                st.error(f"Optimize error: {e}")

        if run_wf:
            try:
                if strat.startswith("MACD"):
                    rows = botmod.walk_forward_optimize_macd_rsi(df, commission_bps=float(fee_bps), train_bars=int(wf_train), test_bars=int(wf_test), step_bars=int(wf_step))
                else:
                    rows = botmod.walk_forward_optimize_bbands_meanrev(df, commission_bps=float(fee_bps), train_bars=int(wf_train), test_bars=int(wf_test), step_bars=int(wf_step))

                if not rows:
                    st.warning("Недостаточно данных для walk-forward")
                else:
                    flat = []
                    for r in rows:
                        o = r.get("oos") or {}
                        flat.append(
                            {
                                "strategy": r.get("strategy"),
                                "train_start": r.get("train_start"),
                                "train_end": r.get("train_end"),
                                "test_start": r.get("test_start"),
                                "test_end": r.get("test_end"),
                                "oos_return_pct": o.get("total_return_pct"),
                                "oos_max_dd_pct": o.get("max_dd_pct"),
                                "oos_trades": o.get("trades"),
                                "oos_win_rate_pct": o.get("win_rate_pct"),
                                "oos_pf": o.get("profit_factor"),
                                "oos_sharpe": o.get("sharpe"),
                                "oos_sortino": o.get("sortino"),
                                "params": r.get("params"),
                            }
                        )

                    df_wf = pd.DataFrame(flat)
                    st.dataframe(df_wf, width="stretch", hide_index=True)

                    try:
                        agg = {
                            "windows": int(len(df_wf)),
                            "mean_oos_return_pct": float(df_wf["oos_return_pct"].astype(float).mean()),
                            "mean_oos_max_dd_pct": float(df_wf["oos_max_dd_pct"].astype(float).mean()),
                            "mean_oos_sortino": float(df_wf["oos_sortino"].astype(float).mean()),
                        }
                        st.dataframe(pd.DataFrame([agg]), width="stretch", hide_index=True)
                    except Exception:
                        pass
            except Exception as e:
                st.error(f"Walk-forward error: {e}")

    fig = go.Figure()
    if not df.empty:
        up = df["Close"] >= df["Open"]
        vol = df["Volume"] if "Volume" in df.columns else None

        fig.add_trace(
            go.Candlestick(
                x=df.index,
                open=df["Open"],
                high=df["High"],
                low=df["Low"],
                close=df["Close"],
                name="Цена",
                increasing_line_color="#00D18F",
                decreasing_line_color="#FF4D4D",
                increasing_fillcolor="rgba(0,209,143,0.35)",
                decreasing_fillcolor="rgba(255,77,77,0.35)",
            )
        )

        if vol is not None:
            colors = ["rgba(0,209,143,0.35)" if bool(u) else "rgba(255,77,77,0.35)" for u in up.tolist()]
            fig.add_trace(
                go.Bar(
                    x=df.index,
                    y=vol,
                    name="Объём",
                    marker_color=colors,
                    opacity=0.7,
                    yaxis="y2",
                )
            )

    fig.update_layout(
        template="plotly_dark",
        height=820,
        margin=dict(l=10, r=10, t=28, b=10),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.06)"),
        xaxis=dict(showgrid=False),
        yaxis2=dict(overlaying="y", side="right", showgrid=False, rangemode="tozero", title=""),
    )
    st.plotly_chart(fig, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Open Positions")
        st.dataframe(_get_positions_df(), width="stretch", hide_index=True)
    with c2:
        st.subheader("Last Trades")
        st.dataframe(_get_trades_df(), width="stretch", hide_index=True)