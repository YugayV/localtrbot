"""
EURUSD Trading Bot - MULTI-PAIRS + NOTIFICATIONS
==============================================
Multiple pairs: FX + Crypto
Notifications: Trade alerts + Hourly reports
"""

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
import logging
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import time
import threading
import json
import os
import contextlib
import io
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


load_env(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
TRADE_LOG_FILE = os.path.join(BASE_DIR, "trades_log.jsonl")
_LOG_LOCK = threading.Lock()

WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Missing BOT_TOKEN or ADMIN_ID in .env")

CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
config_lock = threading.Lock()

CONFIG_DEFAULTS = {
    "initial_balance": 1000.0,
    "trades_per_pair": 2,
    "max_total_positions": 10,
    "risk_per_trade": 10.0,
    "leverage": 10,
    "sl_atr_multiplier": 2.0,
    "tp_atr_multiplier": 6.0,
    "trailing_stop": True,
    "trailing_stop_atr_multiplier": 1.5,
    "check_interval": 60,
    "auto_trade_enabled": True,
    "daily_profit_target_pct": 0.0,
    "daily_loss_limit_pct": 0.0,
    "close_positions_on_stop": False,
    "goya_score_enabled": True,
    "goya_min_score": 35,
    "goya_rank_candidates": True,
    "deepseek_enabled": False,
    "deepseek_model": "deepseek-v4-flash",
    "deepseek_timeout_sec": 10,
    "deepseek_ttl_sec": 300,
    "deepseek_min_local_score": 45,
    "deepseek_min_confidence": 0.6,

    "backtest_commission_bps": 0.0,
}


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: data[k] for k in cfg.keys() if k in data})
        except:
            pass
    return cfg


def save_config(cfg):
    try:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


CONFIG = load_config()
_CONFIG_MTIME = os.path.getmtime(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else 0.0


def _reload_config_if_needed():
    global _CONFIG_MTIME
    try:
        if not os.path.exists(CONFIG_FILE):
            return False
        mt = float(os.path.getmtime(CONFIG_FILE))
        if mt <= float(_CONFIG_MTIME or 0.0):
            return False
    except Exception:
        return False

    try:
        cfg = load_config()
        with config_lock:
            CONFIG.update(cfg)
        _CONFIG_MTIME = mt
        RUNTIME["config_last_reload_ts"] = time.time()
        return True
    except Exception as e:
        RUNTIME["config_last_reload_error"] = str(e)
        return False


def _append_trade_event(evt):
    try:
        if not isinstance(evt, dict):
            return
        line = json.dumps(evt, ensure_ascii=False)
        with _LOG_LOCK:
            with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


RUNTIME = {
    "auto_trade_last_loop_ts": None,
    "auto_trade_last_cycle_ts": None,
    "auto_trade_last_error": None,
    "auto_trade_last_open_ts": None,
    "auto_trade_last_open_pair": None,
    "auto_trade_last_candidates": None,
    "auto_trade_last_sample": None,
    "bot_poll_last_error": None,
    "day_key": None,
    "day_start_balance": None,
    "trading_paused_reason": None,
    "config_last_reload_ts": None,
    "config_last_reload_error": None,
    "state_last_save_ts": None,
}

# OPTIMIZED PAIRS (by performance analysis)
# REMOVED: AUDUSD, USDCHF (0% win rate)
CRYPTO_PAIRS = {
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "BNBUSD",
    "ADAUSD",
    "DOGEUSD",
    "AVAXUSD",
    "LINKUSD",
    "DOTUSD",
    "LTCUSD",
    "MATICUSD",
}

PAIRS = {
    "BTCUSD": "BTC-USD",   # BEST: 64% WR, +$428
    "ETHUSD": "ETH-USD",   # 2nd best: 47% WR, +$271
    "SOLUSD": "SOL-USD",
    "XRPUSD": "XRP-USD",
    "BNBUSD": "BNB-USD",
    "ADAUSD": "ADA-USD",
    "DOGEUSD": "DOGE-USD",
    "AVAXUSD": "AVAX-USD",
    "LINKUSD": "LINK-USD",
    "DOTUSD": "DOT-USD",
    "LTCUSD": "LTC-USD",
    "MATICUSD": ["MATIC-USD", "POL-USD"],

    "USDJPY": "USDJPY=X",  # 40% WR, +$141
    "EURJPY": "EURJPY=X",  # 37% WR, +$96
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "EURGBP": "EURGBP=X",
}

DXY_TICKERS = ["DX-Y.NYB", "DX-Y", "DXY"]

# Timezone for Korea (UTC+9)
def get_seoul_time():
    return datetime.now(timezone(timedelta(hours=9)))

GOOD_HOURS = [5, 7, 8, 18, 19]

DATA_DIR = os.path.join(BASE_DIR, "data")
MODEL_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)

BYBIT_SYMBOLS = {
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
    "SOLUSD": "SOLUSDT",
    "XRPUSD": "XRPUSDT",
    "BNBUSD": "BNBUSDT",
    "ADAUSD": "ADAUSDT",
    "DOGEUSD": "DOGEUSDT",
    "AVAXUSD": "AVAXUSDT",
    "LINKUSD": "LINKUSDT",
    "DOTUSD": "DOTUSDT",
    "LTCUSD": "LTCUSDT",
    "MATICUSD": "POLUSDT",
}


def _bybit_kline(pair, interval_min, limit=1000, end_ms=None, category="linear"):
    sym = BYBIT_SYMBOLS.get(pair)
    if not sym:
        return None

    q = f"category={category}&symbol={sym}&interval={int(interval_min)}&limit={int(limit)}"
    if end_ms is not None:
        q += f"&end={int(end_ms)}"

    url = f"https://api.bybit.com/v5/market/kline?{q}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        payload = json.loads(raw)
        if str(payload.get("retCode")) != "0":
            return None
        lst = (payload.get("result") or {}).get("list") or []
        if not lst:
            return None

        rows = []
        for it in lst:
            try:
                ts = int(it[0])
                o = float(it[1])
                h = float(it[2])
                l = float(it[3])
                c = float(it[4])
                v = float(it[5])
            except Exception:
                continue
            rows.append((ts, o, h, l, c, v))

        if not rows:
            return None

        rows.sort(key=lambda x: x[0])
        df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]) 
        df["Datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("Datetime")
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        return None


def update_market_data(pair, tf="15m", bars=3000, min_age_sec=300, force=False, min_rows=0):
    tf_norm = tf.lower().strip()
    path = os.path.join(DATA_DIR, f"{pair}_{tf_norm}.csv")

    try:
        if (not force) and os.path.exists(path) and (time.time() - os.path.getmtime(path)) < int(min_age_sec):
            cached = pd.read_csv(path, parse_dates=["Datetime"])
            cached = cached.set_index("Datetime")
            if cached is not None and (not cached.empty):
                if int(min_rows) <= 0 or len(cached) >= int(min_rows):
                    return cached
    except Exception:
        pass

    df_new = None

    if pair in CRYPTO_PAIRS and tf_norm in ["15m", "1h"]:
        interval_min = 15 if tf_norm == "15m" else 60
        target = max(int(bars), int(min_rows) if int(min_rows) > 0 else 0)
        need = int(target)
        parts = []
        end_ms = None
        while need > 0 and len(parts) < 10:
            batch = min(1000, need)
            chunk = _bybit_kline(pair, interval_min, limit=batch, end_ms=end_ms)
            if chunk is None or chunk.empty:
                break
            parts.append(chunk)
            end_ms = int(chunk.index[0].value / 1_000_000) - 1
            need -= len(chunk)
        if parts:
            df_new = pd.concat(parts).sort_index()
            df_new = df_new[~df_new.index.duplicated(keep="last")]
    else:
        ticker = PAIRS.get(pair)
        if ticker is not None:
            df_new = get_history(pair, tf=tf_norm)[0]

    if df_new is None or df_new.empty:
        return None

    df_all = df_new
    if os.path.exists(path):
        try:
            old = pd.read_csv(path, parse_dates=["Datetime"])
            old = old.set_index("Datetime")
            df_all = pd.concat([old, df_new]).sort_index()
            df_all = df_all[~df_all.index.duplicated(keep="last")]
        except Exception:
            df_all = df_new

    try:
        out = df_all.copy()
        out = out.reset_index()
        out.to_csv(path, index=False)
    except Exception:
        pass

    return df_all


def train_direction_model(pair, tf="15m", bars=5000, lr=0.2, epochs=250, l2=1e-4):
    tf_norm = str(tf).lower().strip()
    df = update_market_data(pair, tf=tf_norm, bars=bars, min_age_sec=0, min_rows=240)
    if df is None or df.empty:
        raise ValueError(f"Not enough data: pair={pair} tf={tf_norm} rows=0")
    if len(df) < 200:
        raise ValueError(f"Not enough data: pair={pair} tf={tf_norm} rows={len(df)} (need >= 200)")

    close = df["Close"].astype(float)
    ret1 = close.pct_change()
    vol = ret1.rolling(20).std()
    sma20 = close.rolling(20).mean()

    deltas = close.diff()
    gains = deltas.clip(lower=0)
    losses = (-deltas).clip(lower=0)
    avg_gain = gains.rolling(14).mean()
    avg_loss = losses.rolling(14).mean().replace(0, 1e-9)
    rsi = 100 - (100 / (1 + (avg_gain / avg_loss)))

    x = pd.DataFrame(
        {
            "ret1": ret1,
            "vol20": vol,
            "sma_dist": (close - sma20) / sma20,
            "rsi14": rsi / 100.0,
        },
        index=df.index,
    ).dropna()

    y = (close.shift(-1).reindex(x.index) > close.reindex(x.index)).astype(int).values
    x = x.values.astype(float)

    n = len(x)
    if n < 200:
        return None

    split = int(n * 0.8)
    x_train, x_test = x[:split], x[split:]
    y_train, y_test = y[:split], y[split:]

    mu = x_train.mean(axis=0)
    sd = x_train.std(axis=0)
    sd[sd == 0] = 1.0

    x_train = (x_train - mu) / sd
    x_test = (x_test - mu) / sd

    x_train = np.concatenate([np.ones((len(x_train), 1)), x_train], axis=1)
    x_test = np.concatenate([np.ones((len(x_test), 1)), x_test], axis=1)

    w = np.zeros(x_train.shape[1], dtype=float)

    def _sig(z):
        z = np.clip(z, -30, 30)
        return 1.0 / (1.0 + np.exp(-z))

    for _ in range(int(epochs)):
        p = _sig(x_train @ w)
        grad = (x_train.T @ (p - y_train)) / len(y_train)
        grad[1:] += l2 * w[1:]
        w -= float(lr) * grad

    p_test = _sig(x_test @ w)
    pred = (p_test >= 0.5).astype(int)
    acc = float((pred == y_test).mean()) if len(y_test) else 0.0

    model = {
        "pair": pair,
        "tf": tf,
        "type": "logreg",
        "feature_names": ["bias", "ret1", "vol20", "sma_dist", "rsi14"],
        "mu": mu.tolist(),
        "sd": sd.tolist(),
        "w": w.tolist(),
        "metrics": {"acc": acc, "n": int(n)},
        "trained_at": int(time.time()),
    }

    try:
        with open(os.path.join(MODEL_DIR, f"{pair}_{tf_norm}_logreg.json"), "w", encoding="utf-8") as f:
            json.dump(model, f)
    except Exception:
        pass

    return model


def load_direction_model(pair, tf="15m"):
    tf_norm = tf.lower().strip()
    path = os.path.join(MODEL_DIR, f"{pair}_{tf_norm}_logreg.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def predict_direction_proba(df, model):
    try:
        close = df["Close"].astype(float)
        ret1 = float(close.pct_change().iloc[-1])
        vol20 = float(close.pct_change().rolling(20).std().iloc[-1])
        sma20 = float(close.rolling(20).mean().iloc[-1])
        sma_dist = float((close.iloc[-1] - sma20) / sma20) if sma20 else 0.0

        deltas = close.diff()
        gains = deltas.clip(lower=0)
        losses = (-deltas).clip(lower=0)
        avg_gain = float(gains.rolling(14).mean().iloc[-1])
        avg_loss = float(losses.rolling(14).mean().iloc[-1])
        if avg_loss <= 0:
            avg_loss = 1e-9
        rsi14 = (100 - (100 / (1 + (avg_gain / avg_loss)))) / 100.0

        mu = np.array(model.get("mu") or [], dtype=float)
        sd = np.array(model.get("sd") or [], dtype=float)
        w = np.array(model.get("w") or [], dtype=float)
        if len(mu) != 4 or len(sd) != 4 or len(w) != 5:
            return None

        x = np.array([ret1, vol20, sma_dist, rsi14], dtype=float)
        x = (x - mu) / sd
        x = np.concatenate([[1.0], x])
        z = float(np.clip(x @ w, -30, 30))
        return float(1.0 / (1.0 + np.exp(-z)))
    except Exception:
        return None



def fmt_price(pair, price):
    if pair in CRYPTO_PAIRS:
        p = float(price)
        if abs(p) >= 100:
            return f"{p:.2f}"
        if abs(p) >= 1:
            return f"{p:.4f}"
        return f"{p:.6f}"
    if pair.endswith("JPY"):
        return f"{price:.3f}"
    return f"{price:.5f}"


def get_sl_tp_distance(pair, atr=None):
    with config_lock:
        sl_atr = float(CONFIG.get("sl_atr_multiplier", 2.0))
        tp_atr = float(CONFIG.get("tp_atr_multiplier", 6.0))

    if atr is not None and float(atr) > 0:
        return sl_atr * float(atr), tp_atr * float(atr)

    # Fallback to fixed pips if ATR not available
    sl_pips = float(CONFIG.get("sl_pips", 100))
    tp_pips = float(CONFIG.get("tp_pips", 300))
    if pair in CRYPTO_PAIRS:
        return sl_pips, tp_pips

    pip = 0.01 if pair.endswith("JPY") else 0.0001
    return sl_pips * pip, tp_pips * pip

# PAIR PRIORITY & RISK MODIFIERS
PAIR_CONFIG = {
    "BTCUSD": {"priority": 1, "risk_mult": 1.5},  # Top performer
    "ETHUSD": {"priority": 2, "risk_mult": 1.2},
    "SOLUSD": {"priority": 3, "risk_mult": 1.0},
    "XRPUSD": {"priority": 4, "risk_mult": 1.0},
    "BNBUSD": {"priority": 5, "risk_mult": 1.0},
    "ADAUSD": {"priority": 6, "risk_mult": 1.0},
    "DOGEUSD": {"priority": 7, "risk_mult": 1.0},
    "AVAXUSD": {"priority": 8, "risk_mult": 1.0},
    "LINKUSD": {"priority": 9, "risk_mult": 1.0},
    "DOTUSD": {"priority": 10, "risk_mult": 1.0},
    "LTCUSD": {"priority": 11, "risk_mult": 1.0},
    "MATICUSD": {"priority": 12, "risk_mult": 1.0},

    "USDJPY": {"priority": 13, "risk_mult": 1.0},
    "EURJPY": {"priority": 14, "risk_mult": 1.0},
    "EURUSD": {"priority": 15, "risk_mult": 0.8},
    "GBPUSD": {"priority": 16, "risk_mult": 0.8},
    "EURGBP": {"priority": 17, "risk_mult": 0.8},
}

_DATA_CACHE = {}
_DATA_CACHE_LOCK = threading.Lock()


def get_data(ticker, period="5d", interval="15m"):
    now = time.time()
    tickers = list(ticker) if isinstance(ticker, (list, tuple, set)) else [ticker]

    ttl = 120
    if interval in ["1h", "60m"]:
        ttl = 300
    if interval in ["15m", "30m"]:
        ttl = 120

    fail_ttl = 600

    for tk in tickers:
        key = f"{tk}|{period}|{interval}"

        with _DATA_CACHE_LOCK:
            hit = _DATA_CACHE.get(key)
            if hit and hit.get("df") is not None and (now - hit.get("ts", 0)) < ttl:
                return hit.get("df")
            if hit and hit.get("df") is None and (now - hit.get("fail_ts", 0)) < fail_ttl:
                continue

        try:
            t = yf.Ticker(tk)
            _buf = io.StringIO()
            with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
                d = t.history(period=period, interval=interval)
            if d is None or d.empty or len(d) < 20:
                raise ValueError("no data")

            with _DATA_CACHE_LOCK:
                _DATA_CACHE[key] = {"ts": now, "df": d}

            return d
        except:
            with _DATA_CACHE_LOCK:
                prev = _DATA_CACHE.get(key)
                if prev and prev.get("df") is not None:
                    return prev.get("df")
                _DATA_CACHE[key] = {"fail_ts": now, "df": None}

    return None

def get_indicators(data, pair_name):
    closes = data['Close'].values
    highs = data['High'].values
    lows = data['Low'].values
    
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(14).mean().iloc[-1]
    avg_loss_raw = pd.Series(losses).rolling(14).mean().iloc[-1]
    avg_loss = avg_loss_raw if avg_loss_raw > 0 else 0.00001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    sma = pd.Series(closes).rolling(20).mean().iloc[-1]
    trend = 1 if closes[-1] > sma else -1
    change = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) > 1 else 0
    
    # Calculate ATR (14-period)
    tr = []
    for i in range(1, len(data)):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i-1]
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr.append(max(tr1, tr2, tr3))
    atr = np.mean(tr[-14:]) if len(tr) >= 14 else (np.mean(tr) if tr else (highs[-1] - lows[-1]))
    
    return {
        'price': closes[-1],
        'prev': closes[-2] if len(closes) > 1 else closes[-1],
        'change': change,
        'rsi': rsi,
        'trend': trend,
        'sma': sma,
        'atr': float(atr),
        'pair': pair_name,
    }

def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean().replace(0, 1e-9)
    return 100 - (100 / (1 + (gain / loss)))


def _dxy_confirmation_for_eurusd(ind15, interval="15m"):
    dxy = get_data(DXY_TICKERS, period="5d" if interval in ["15m", "30m"] else "60d", interval=interval)
    if dxy is None or dxy.empty or len(dxy) < 20:
        dxy = get_data(DXY_TICKERS, period="60d", interval="1h")
        interval = "1h"

    if dxy is None or dxy.empty or len(dxy) < 20:
        return None

    close = dxy["Close"].astype(float)
    dxy_px = float(close.iloc[-1])
    dxy_ret = float(close.pct_change().iloc[-1])
    dxy_mom3 = float(close.pct_change(3).iloc[-1]) if len(close) >= 4 else 0.0
    dxy_rsi = float(_rsi_series(close, 14).iloc[-1])

    eur_rsi = float(ind15.get("rsi") or 50)

    score = 0
    details = []

    if dxy_ret < -0.001 and eur_rsi < 40:
        score += 1
        details.append("DXY DOWN + EUR RSI low")
    if dxy_ret > 0.001 and eur_rsi > 60:
        score -= 1
        details.append("DXY UP + EUR RSI high")

    if dxy_mom3 < -0.002:
        score += 1
        details.append("DXY momentum bearish")
    if dxy_mom3 > 0.002:
        score -= 1
        details.append("DXY momentum bullish")

    macro_dir = 0
    if score >= 2:
        macro_dir = 1
    elif score <= -2:
        macro_dir = -1

    return {
        "interval": interval,
        "dxy": dxy_px,
        "ret": dxy_ret,
        "mom3": dxy_mom3,
        "rsi": dxy_rsi,
        "score": int(score),
        "dir": int(macro_dir),
        "details": details,
    }


def _clip(x, lo, hi):
    try:
        return max(float(lo), min(float(x), float(hi)))
    except Exception:
        return float(lo)


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast = int(fast)
    slow = int(slow)
    signal = int(signal)
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return line, sig, hist


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(int(period)).mean()


def _goya_score_local(df15: pd.DataFrame, ind15: dict, ind1h: dict | None, p_up: float | None):
    rsi15 = float(ind15.get("rsi") or 50.0)
    trend15 = int(ind15.get("trend") or 0)
    change = float(ind15.get("change") or 0.0)

    vol20 = None
    try:
        close = df15["Close"].astype(float)
        vol20 = float(close.pct_change().rolling(20).std().iloc[-1])
    except Exception:
        vol20 = None

    vol_scale = 1.0
    if vol20 is not None and vol20 >= 0:
        vol_scale = 1.0 / (1.0 + (vol20 * 40.0))

    c_rsi = _clip((rsi15 - 50.0) / 25.0, -1.0, 1.0)
    c_trend15 = 1.0 if trend15 == 1 else (-1.0 if trend15 == -1 else 0.0)
    c_mom = float(np.tanh(change / 0.03))

    c_trend1h = 0.0
    if ind1h is not None:
        t1h = int(ind1h.get("trend") or 0)
        c_trend1h = 1.0 if t1h == 1 else (-1.0 if t1h == -1 else 0.0)

    c_model = 0.0
    has_model = False
    if p_up is not None:
        try:
            c_model = _clip((float(p_up) - 0.5) * 2.0, -1.0, 1.0)
            has_model = True
        except Exception:
            c_model = 0.0
            has_model = False

    w_sum = 0.0
    s_sum = 0.0
    parts = []

    def add(name, w, val):
        nonlocal w_sum, s_sum
        w = float(w)
        val = float(val)
        parts.append({"name": name, "w": w, "v": val})
        w_sum += abs(w)
        s_sum += w * val

    add("mom", 1.0, c_mom)
    add("rsi", 1.0, c_rsi)
    add("trend15", 1.0, c_trend15)
    add("trend1h", 0.6, c_trend1h)
    if has_model:
        add("model", 1.2, c_model)

    raw = (s_sum / w_sum) if w_sum else 0.0
    score = int(round(_clip(raw * 100.0 * vol_scale, -100.0, 100.0)))
    return {"score": score, "vol20": float(vol20) if vol20 is not None else None, "parts": parts}


_DEEPSEEK_LOCK = threading.Lock()
_DEEPSEEK_CACHE = {}


def _deepseek_score(pair, tf_norm, ind15, ind1h, local_score):
    with config_lock:
        enabled = bool(CONFIG.get("deepseek_enabled", False))
        model = str(CONFIG.get("deepseek_model", "deepseek-v4-flash") or "deepseek-v4-flash")
        timeout = float(CONFIG.get("deepseek_timeout_sec", 10) or 10)
        ttl = int(CONFIG.get("deepseek_ttl_sec", 300) or 300)
        min_local = int(CONFIG.get("deepseek_min_local_score", 45) or 45)

    if not enabled or (not DEEPSEEK_API_KEY) or abs(int(local_score)) < int(min_local):
        return None

    now = time.time()
    key = f"{pair}|{tf_norm}"
    with _DEEPSEEK_LOCK:
        hit = _DEEPSEEK_CACHE.get(key)
        if hit and (now - float(hit.get("ts") or 0)) < ttl:
            return hit.get("data")

    with config_lock:
        min_conf = float(CONFIG.get("deepseek_min_confidence", 0.6) or 0.6)

    req_body = {
        "model": model,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 220,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "Верни ТОЛЬКО JSON: {\"score\":-100..100,\"dir\":-1|0|1,\"confidence\":0..1,\"reasons\":[строки]}.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "pair": pair,
                        "tf": tf_norm,
                        "local_score": int(local_score),
                        "rsi15": float(ind15.get("rsi") or 0.0),
                        "trend15": int(ind15.get("trend") or 0),
                        "change": float(ind15.get("change") or 0.0),
                        "atr": float(ind15.get("atr") or 0.0),
                        "rsi1h": float(ind1h.get("rsi") or 0.0) if ind1h else None,
                        "trend1h": int(ind1h.get("trend") or 0) if ind1h else None,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(req_body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        resp = json.loads(raw)
        txt = (((resp.get("choices") or [{}])[0]).get("message") or {}).get("content")
        data = json.loads(txt) if isinstance(txt, str) else None
        if not isinstance(data, dict):
            return None

        score = int(_clip(float(data.get("score") or 0), -100, 100))
        d = int(float(data.get("dir") or 0))
        if d not in (-1, 0, 1):
            d = 0
        conf = float(_clip(float(data.get("confidence") or 0.0), 0.0, 1.0))
        reasons = data.get("reasons")
        if not isinstance(reasons, list):
            reasons = []

        out = {"score": score, "dir": d, "confidence": conf, "min_conf": float(min_conf), "reasons": [str(x) for x in reasons][:6]}
        with _DEEPSEEK_LOCK:
            _DEEPSEEK_CACHE[key] = {"ts": now, "data": out}
        return out
    except Exception:
        return None


def _goya_score_local(pair, df15, ind15, ind1h, p_up=None):
    rsi15 = float(ind15.get("rsi") or 50.0)
    trend15 = int(ind15.get("trend") or 0)
    change = float(ind15.get("change") or 0.0)

    vol20 = None
    try:
        close = df15["Close"].astype(float)
        vol20 = float(close.pct_change().rolling(20).std().iloc[-1])
    except Exception:
        vol20 = None

    vol_scale = 1.0
    if vol20 is not None and vol20 >= 0:
        vol_scale = 1.0 / (1.0 + (vol20 * 40.0))

    c_rsi = _clip((rsi15 - 50.0) / 25.0, -1.0, 1.0)
    c_trend15 = 1.0 if trend15 == 1 else (-1.0 if trend15 == -1 else 0.0)
    c_mom = float(np.tanh(change / 0.03))

    c_trend1h = 0.0
    if ind1h is not None:
        t1h = int(ind1h.get("trend") or 0)
        c_trend1h = 1.0 if t1h == 1 else (-1.0 if t1h == -1 else 0.0)

    c_model = 0.0
    has_model = False
    if p_up is not None:
        try:
            c_model = _clip((float(p_up) - 0.5) * 2.0, -1.0, 1.0)
            has_model = True
        except Exception:
            c_model = 0.0
            has_model = False

    parts = []
    w_sum = 0.0
    s_sum = 0.0

    def add(name, w, val):
        nonlocal w_sum, s_sum
        w = float(w)
        val = float(val)
        parts.append({"name": name, "w": w, "v": val})
        w_sum += abs(w)
        s_sum += w * val

    add("mom", 1.0, c_mom)
    add("rsi", 1.0, c_rsi)
    add("trend15", 1.0, c_trend15)
    add("trend1h", 0.6, c_trend1h)
    if has_model:
        add("model", 1.2, c_model)

    raw = (s_sum / w_sum) if w_sum else 0.0
    score = int(round(_clip(raw * 100.0 * vol_scale, -100.0, 100.0)))

    return {
        "pair": pair,
        "score": score,
        "vol20": float(vol20) if vol20 is not None else None,
        "parts": parts,
    }


_DEEPSEEK_LOCK = threading.Lock()
_DEEPSEEK_CACHE = {}


def _deepseek_goya_score(pair, tf_norm, ind15, ind1h, local_score):
    with config_lock:
        enabled = bool(CONFIG.get("deepseek_enabled", False))
        model = str(CONFIG.get("deepseek_model", "deepseek-v4-flash") or "deepseek-v4-flash")
        timeout = float(CONFIG.get("deepseek_timeout_sec", 10) or 10)
        ttl = int(CONFIG.get("deepseek_ttl_sec", 300) or 300)
        min_local = int(CONFIG.get("deepseek_min_local_score", 45) or 45)

    if not enabled:
        return None
    if not DEEPSEEK_API_KEY:
        return None
    if abs(int(local_score)) < int(min_local):
        return None

    now = time.time()
    cache_key = f"{pair}|{tf_norm}"
    with _DEEPSEEK_LOCK:
        hit = _DEEPSEEK_CACHE.get(cache_key)
        if hit and (now - float(hit.get("ts") or 0)) < ttl:
            return hit.get("data")

    payload = {
        "model": model,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 220,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "Верни ТОЛЬКО JSON. Схема: {\"score\":-100..100,\"dir\":-1|0|1,\"confidence\":0..1,\"reasons\":[строки]}.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "pair": pair,
                        "tf": tf_norm,
                        "local_score": int(local_score),
                        "rsi15": float(ind15.get("rsi") or 0.0),
                        "trend15": int(ind15.get("trend") or 0),
                        "change": float(ind15.get("change") or 0.0),
                        "atr": float(ind15.get("atr") or 0.0),
                        "rsi1h": float(ind1h.get("rsi") or 0.0) if ind1h else None,
                        "trend1h": int(ind1h.get("trend") or 0) if ind1h else None,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }

    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        resp = json.loads(raw)
        txt = (((resp.get("choices") or [{}])[0]).get("message") or {}).get("content")
        data = json.loads(txt) if isinstance(txt, str) else None
        if not isinstance(data, dict):
            return None

        score = int(float(data.get("score") or 0))
        score = int(_clip(score, -100, 100))
        d = int(float(data.get("dir") or 0))
        if d not in (-1, 0, 1):
            d = 0
        conf = float(data.get("confidence") or 0.0)
        conf = float(_clip(conf, 0.0, 1.0))
        reasons = data.get("reasons")
        if not isinstance(reasons, list):
            reasons = []

        out = {"score": score, "dir": d, "confidence": conf, "reasons": [str(x) for x in reasons][:6]}
        with _DEEPSEEK_LOCK:
            _DEEPSEEK_CACHE[cache_key] = {"ts": now, "data": out}
        return out
    except Exception:
        return None


def backtest_macd_rsi(df: pd.DataFrame, rsi_min: float = 50.0, macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9, sl_atr_mult: float = 2.0, tp_atr_mult: float = 6.0, trailing_atr_mult: float | None = None, commission_bps: float = 0.0):
    df = df.copy()
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if df.empty or len(df) < 60:
        return {"equity": [], "trades": [], "error": "not enough data"}

    close = df["Close"].astype(float)
    macd_line, macd_sig, _ = _macd(close, fast=int(macd_fast), slow=int(macd_slow), signal=int(macd_signal))
    rsi = _rsi_series(close, 14)
    atr = _atr(df, 14)

    equity = 100.0
    eq = []
    trades = []

    in_pos = False
    entry = 0.0
    sl = 0.0
    tp = 0.0
    peak = 0.0

    for i in range(2, len(df)):
        ts = df.index[i]
        px = float(close.iloc[i])
        eq.append({"t": ts, "equity": float(equity)})

        if not in_pos:
            cross_up = float(macd_line.iloc[i - 1]) <= float(macd_sig.iloc[i - 1]) and float(macd_line.iloc[i]) > float(macd_sig.iloc[i])
            if cross_up and float(rsi.iloc[i]) > float(rsi_min) and float(atr.iloc[i] or 0) > 0:
                in_pos = True
                entry = px
                sl = entry - (float(sl_atr_mult) * float(atr.iloc[i]))
                tp = entry + (float(tp_atr_mult) * float(atr.iloc[i]))
                peak = entry
                trades.append({"direction": 1, "open_t": ts, "entry": entry, "sl": sl, "tp": tp, "close_t": None, "exit": None, "pnl": 0.0, "status": "OPEN", "exit_reason": None})
            continue

        if px > peak:
            peak = px
        if trailing_atr_mult is not None and float(atr.iloc[i] or 0) > 0:
            tsl = float(peak) - (float(trailing_atr_mult) * float(atr.iloc[i]))
            if tsl > sl:
                sl = tsl

        exit_now = False
        exit_reason = None

        if px <= sl:
            exit_now = True
            exit_reason = "SL"
        elif px >= tp:
            exit_now = True
            exit_reason = "TP"
        else:
            cross_dn = float(macd_line.iloc[i - 1]) >= float(macd_sig.iloc[i - 1]) and float(macd_line.iloc[i]) < float(macd_sig.iloc[i])
            if cross_dn:
                exit_now = True
                exit_reason = "MACD"

        if exit_now:
            risk = 10.0
            sl_dist = max(entry - sl, 1e-9)
            rr = (px - entry) / sl_dist
            pnl = risk * rr
            pnl = max(-risk, min(pnl, risk * (float(tp_atr_mult) / max(float(sl_atr_mult), 1e-9))))

            fees = float(commission_bps) / 10000.0
            if fees > 0:
                pnl -= abs(pnl) * fees

            equity += pnl

            t = trades[-1]
            t["close_t"] = ts
            t["exit"] = px
            t["pnl"] = float(pnl)
            t["status"] = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
            t["exit_reason"] = exit_reason

            in_pos = False
            entry = 0.0
            sl = 0.0
            tp = 0.0
            peak = 0.0

    if eq and eq[-1]["t"] != df.index[-1]:
        eq.append({"t": df.index[-1], "equity": float(equity)})

    return {"equity": eq, "trades": trades}


def backtest_metrics(result: dict):
    eq = result.get("equity") or []
    trades = result.get("trades") or []
    if not eq:
        return {"ok": False, "error": str(result.get("error") or "no equity")}

    equity = np.array([float(x.get("equity") or 0.0) for x in eq], dtype=float)
    if len(equity) < 2:
        return {"ok": False, "error": "too short"}

    rets = np.diff(equity) / np.maximum(equity[:-1], 1e-9)

    total_return_pct = float((equity[-1] / max(equity[0], 1e-9) - 1.0) * 100.0)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.maximum(peak, 1e-9)
    max_dd_pct = float(dd.max() * 100.0)

    closed = [t for t in trades if t.get("status") in ["WIN", "LOSS", "FLAT"]]
    wins = [t for t in closed if t.get("status") == "WIN"]
    losses = [t for t in closed if t.get("status") == "LOSS"]

    win_rate_pct = float(len(wins) / len(closed) * 100.0) if closed else 0.0

    gp = float(sum(float(t.get("pnl") or 0.0) for t in wins))
    gl = float(-sum(float(t.get("pnl") or 0.0) for t in losses))
    profit_factor = float(gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)

    mu = float(np.mean(rets)) if len(rets) else 0.0
    sd = float(np.std(rets)) if len(rets) else 0.0
    sharpe = float((mu / sd) * np.sqrt(252)) if sd > 0 else 0.0

    neg = rets[rets < 0]
    ddn = float(np.std(neg)) if len(neg) else 0.0
    sortino = float((mu / ddn) * np.sqrt(252)) if ddn > 0 else 0.0

    return {
        "ok": True,
        "total_return_pct": total_return_pct,
        "max_dd_pct": max_dd_pct,
        "trades": int(len(closed)),
        "win_rate_pct": win_rate_pct,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sortino": sortino,
    }


def optimize_macd_rsi(df: pd.DataFrame, commission_bps: float = 0.0, top_n: int = 5):
    rsi_mins = [45, 50, 55]
    macd_sets = [(12, 26, 9), (8, 21, 5), (5, 35, 5)]
    sl_mults = [1.5, 2.0, 2.5]
    tp_mults = [4.0, 6.0, 8.0]
    trailing_mults = [None, 2.0, 3.0]

    out = []
    for rsi_min in rsi_mins:
        for f, s, si in macd_sets:
            for sl in sl_mults:
                for tp in tp_mults:
                    for tr in trailing_mults:
                        bt = backtest_macd_rsi(
                            df,
                            rsi_min=float(rsi_min),
                            macd_fast=int(f),
                            macd_slow=int(s),
                            macd_signal=int(si),
                            sl_atr_mult=float(sl),
                            tp_atr_mult=float(tp),
                            trailing_atr_mult=(float(tr) if tr is not None else None),
                            commission_bps=float(commission_bps),
                        )
                        mt = backtest_metrics(bt)
                        if not mt.get("ok"):
                            continue
                        out.append({"strategy": "MACD_RSI", "rsi_min": int(rsi_min), "macd": f"{f},{s},{si}", "sl_atr": float(sl), "tp_atr": float(tp), "trail_atr": tr, **mt})

    out.sort(key=lambda x: (float(x.get("sortino") or 0.0), float(x.get("total_return_pct") or 0.0)), reverse=True)
    return out[: int(top_n)]


def _bbands(close: pd.Series, period: int = 20, mult: float = 2.0):
    p = int(period)
    m = float(mult)
    ma = close.rolling(p).mean()
    sd = close.rolling(p).std()
    up = ma + (m * sd)
    lo = ma - (m * sd)
    return ma, up, lo


def backtest_bbands_meanrev(df: pd.DataFrame, bb_period: int = 20, bb_mult: float = 2.0, rsi_low: float = 40.0, rsi_high: float = 60.0, sl_atr_mult: float = 2.0, tp_atr_mult: float = 4.0, trailing_atr_mult: float | None = None, commission_bps: float = 0.0):
    df = df.copy()
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if df.empty or len(df) < 80:
        return {"equity": [], "trades": [], "error": "not enough data"}

    close = df["Close"].astype(float)
    rsi = _rsi_series(close, 14)
    atr = _atr(df, 14)
    mid, up, lo = _bbands(close, period=int(bb_period), mult=float(bb_mult))

    equity = 100.0
    eq = []
    trades = []

    in_pos = False
    direction = 0
    entry = 0.0
    sl = 0.0
    tp = 0.0
    peak = 0.0

    for i in range(2, len(df)):
        ts = df.index[i]
        px = float(close.iloc[i])
        eq.append({"t": ts, "equity": float(equity)})

        a = float(atr.iloc[i] or 0.0)
        if a <= 0:
            continue

        if not in_pos:
            if float(close.iloc[i]) < float(lo.iloc[i]) and float(rsi.iloc[i]) < float(rsi_low):
                in_pos = True
                direction = 1
                entry = px
                sl = entry - (float(sl_atr_mult) * a)
                tp = entry + (float(tp_atr_mult) * a)
                peak = entry
                trades.append({"direction": 1, "open_t": ts, "entry": entry, "sl": sl, "tp": tp, "close_t": None, "exit": None, "pnl": 0.0, "status": "OPEN", "exit_reason": None})
                continue
            if float(close.iloc[i]) > float(up.iloc[i]) and float(rsi.iloc[i]) > float(rsi_high):
                in_pos = True
                direction = -1
                entry = px
                sl = entry + (float(sl_atr_mult) * a)
                tp = entry - (float(tp_atr_mult) * a)
                peak = entry
                trades.append({"direction": -1, "open_t": ts, "entry": entry, "sl": sl, "tp": tp, "close_t": None, "exit": None, "pnl": 0.0, "status": "OPEN", "exit_reason": None})
                continue
            continue

        if direction == 1:
            if px > peak:
                peak = px
            if trailing_atr_mult is not None:
                tsl = float(peak) - (float(trailing_atr_mult) * a)
                if tsl > sl:
                    sl = tsl
        else:
            if px < peak:
                peak = px
            if trailing_atr_mult is not None:
                tsl = float(peak) + (float(trailing_atr_mult) * a)
                if tsl < sl:
                    sl = tsl

        exit_now = False
        exit_reason = None

        if direction == 1:
            if px <= sl:
                exit_now = True
                exit_reason = "SL"
            elif px >= tp:
                exit_now = True
                exit_reason = "TP"
            elif float(px) >= float(mid.iloc[i]):
                exit_now = True
                exit_reason = "MID"
        else:
            if px >= sl:
                exit_now = True
                exit_reason = "SL"
            elif px <= tp:
                exit_now = True
                exit_reason = "TP"
            elif float(px) <= float(mid.iloc[i]):
                exit_now = True
                exit_reason = "MID"

        if exit_now:
            risk = 10.0
            if direction == 1:
                sl_dist = max(entry - sl, 1e-9)
                rr = (px - entry) / sl_dist
            else:
                sl_dist = max(sl - entry, 1e-9)
                rr = (entry - px) / sl_dist

            pnl = risk * rr
            pnl = max(-risk, min(pnl, risk * (float(tp_atr_mult) / max(float(sl_atr_mult), 1e-9))))

            fees = float(commission_bps) / 10000.0
            if fees > 0:
                pnl -= abs(pnl) * fees

            equity += pnl

            t = trades[-1]
            t["close_t"] = ts
            t["exit"] = px
            t["pnl"] = float(pnl)
            t["status"] = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
            t["exit_reason"] = exit_reason

            in_pos = False
            direction = 0
            entry = 0.0
            sl = 0.0
            tp = 0.0
            peak = 0.0

    if eq and eq[-1]["t"] != df.index[-1]:
        eq.append({"t": df.index[-1], "equity": float(equity)})

    return {"equity": eq, "trades": trades}


def optimize_bbands_meanrev(df: pd.DataFrame, commission_bps: float = 0.0, top_n: int = 5):
    bb_periods = [20]
    bb_mults = [1.5, 2.0, 2.5]
    rsi_lows = [35, 40, 45]
    rsi_highs = [55, 60, 65]
    sl_mults = [1.5, 2.0, 2.5]
    tp_mults = [3.0, 4.0, 6.0]
    trailing_mults = [None, 2.0, 3.0]

    out = []
    for p in bb_periods:
        for bm in bb_mults:
            for rl in rsi_lows:
                for rh in rsi_highs:
                    for sl in sl_mults:
                        for tp in tp_mults:
                            for tr in trailing_mults:
                                bt = backtest_bbands_meanrev(
                                    df,
                                    bb_period=int(p),
                                    bb_mult=float(bm),
                                    rsi_low=float(rl),
                                    rsi_high=float(rh),
                                    sl_atr_mult=float(sl),
                                    tp_atr_mult=float(tp),
                                    trailing_atr_mult=(float(tr) if tr is not None else None),
                                    commission_bps=float(commission_bps),
                                )
                                mt = backtest_metrics(bt)
                                if not mt.get("ok"):
                                    continue
                                out.append({"strategy": "BB_MEANREV", "bb_period": int(p), "bb_mult": float(bm), "rsi_low": float(rl), "rsi_high": float(rh), "sl_atr": float(sl), "tp_atr": float(tp), "trail_atr": tr, **mt})

    out.sort(key=lambda x: (float(x.get("sortino") or 0.0), float(x.get("total_return_pct") or 0.0)), reverse=True)
    return out[: int(top_n)]


def variant_report(bt: dict, params: dict | None = None, strategy: str | None = None):
    mt = backtest_metrics(bt or {})
    trades = list(bt.get("trades") or []) if isinstance(bt, dict) else []
    closed = [t for t in trades if t.get("status") in ["WIN", "LOSS", "FLAT"]]

    wins = sorted([t for t in closed if float(t.get("pnl") or 0.0) > 0], key=lambda x: float(x.get("pnl") or 0.0), reverse=True)
    losses = sorted([t for t in closed if float(t.get("pnl") or 0.0) < 0], key=lambda x: float(x.get("pnl") or 0.0))

    def _pick(rows, n=5):
        out = []
        for r in rows[: int(n)]:
            out.append(
                {
                    "open_t": str(r.get("open_t")),
                    "close_t": str(r.get("close_t")),
                    "direction": int(r.get("direction") or 0),
                    "entry": float(r.get("entry") or 0.0),
                    "exit": float(r.get("exit") or 0.0) if r.get("exit") is not None else None,
                    "pnl": float(r.get("pnl") or 0.0),
                    "exit_reason": r.get("exit_reason"),
                }
            )
        return out

    exit_reasons = {}
    for t in closed:
        k = str(t.get("exit_reason") or "?")
        exit_reasons[k] = int(exit_reasons.get(k, 0)) + 1

    avg_pnl = float(np.mean([float(t.get("pnl") or 0.0) for t in closed])) if closed else 0.0

    return {
        "strategy": strategy or (params or {}).get("strategy"),
        "params": params or {},
        "metrics": mt,
        "summary": {
            "trades": int(len(closed)),
            "wins": int(len([t for t in closed if t.get("status") == "WIN"])),
            "losses": int(len([t for t in closed if t.get("status") == "LOSS"])),
            "avg_pnl": avg_pnl,
        },
        "exit_reasons": exit_reasons,
        "top_wins": _pick(wins, 5),
        "top_losses": _pick(losses, 5),
    }


def _wf_slices(n: int, train_bars: int, test_bars: int, step_bars: int):
    train_bars = int(train_bars)
    test_bars = int(test_bars)
    step_bars = int(step_bars)
    if train_bars <= 0 or test_bars <= 0 or step_bars <= 0:
        return []

    out = []
    start = 0
    while True:
        train_end = start + train_bars
        test_end = train_end + test_bars
        if test_end > n:
            break
        out.append((start, train_end, train_end, test_end))
        start += step_bars
    return out


def walk_forward_optimize_macd_rsi(df: pd.DataFrame, commission_bps: float = 0.0, train_bars: int = 400, test_bars: int = 120, step_bars: int = 120):
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if df.empty:
        return []

    slices = _wf_slices(len(df), int(train_bars), int(test_bars), int(step_bars))
    rows = []

    for tr0, tr1, te0, te1 in slices:
        d_train = df.iloc[tr0:tr1]
        d_test = df.iloc[te0:te1]

        best = (optimize_macd_rsi(d_train, commission_bps=float(commission_bps), top_n=1) or [None])[0]
        if not best:
            continue

        train_metrics = {k: best.get(k) for k in ["total_return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "sharpe", "sortino"]}

        macd_fast, macd_slow, macd_sig = [int(x) for x in str(best.get("macd") or "12,26,9").split(",")[:3]]
        bt = backtest_macd_rsi(
            d_test,
            rsi_min=float(best.get("rsi_min") or 50),
            macd_fast=macd_fast,
            macd_slow=macd_slow,
            macd_signal=macd_sig,
            sl_atr_mult=float(best.get("sl_atr") or 2.0),
            tp_atr_mult=float(best.get("tp_atr") or 6.0),
            trailing_atr_mult=(float(best.get("trail_atr")) if best.get("trail_atr") is not None else None),
            commission_bps=float(commission_bps),
        )
        oos = backtest_metrics(bt)
        if not oos.get("ok"):
            continue

        rows.append(
            {
                "strategy": "MACD_RSI",
                "train_start": str(d_train.index[0]),
                "train_end": str(d_train.index[-1]),
                "test_start": str(d_test.index[0]),
                "test_end": str(d_test.index[-1]),
                "params": {k: best.get(k) for k in ["rsi_min", "macd", "sl_atr", "tp_atr", "trail_atr"]},
                "train": train_metrics,
                "oos": {k: oos.get(k) for k in ["total_return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "sharpe", "sortino"]},
            }
        )

    return rows


def walk_forward_optimize_bbands_meanrev(df: pd.DataFrame, commission_bps: float = 0.0, train_bars: int = 400, test_bars: int = 120, step_bars: int = 120):
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if df.empty:
        return []

    slices = _wf_slices(len(df), int(train_bars), int(test_bars), int(step_bars))
    rows = []

    for tr0, tr1, te0, te1 in slices:
        d_train = df.iloc[tr0:tr1]
        d_test = df.iloc[te0:te1]

        best = (optimize_bbands_meanrev(d_train, commission_bps=float(commission_bps), top_n=1) or [None])[0]
        if not best:
            continue

        train_metrics = {k: best.get(k) for k in ["total_return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "sharpe", "sortino"]}

        bt = backtest_bbands_meanrev(
            d_test,
            bb_period=int(best.get("bb_period") or 20),
            bb_mult=float(best.get("bb_mult") or 2.0),
            rsi_low=float(best.get("rsi_low") or 40),
            rsi_high=float(best.get("rsi_high") or 60),
            sl_atr_mult=float(best.get("sl_atr") or 2.0),
            tp_atr_mult=float(best.get("tp_atr") or 4.0),
            trailing_atr_mult=(float(best.get("trail_atr")) if best.get("trail_atr") is not None else None),
            commission_bps=float(commission_bps),
        )
        oos = backtest_metrics(bt)
        if not oos.get("ok"):
            continue

        rows.append(
            {
                "strategy": "BB_MEANREV",
                "train_start": str(d_train.index[0]),
                "train_end": str(d_train.index[-1]),
                "test_start": str(d_test.index[0]),
                "test_end": str(d_test.index[-1]),
                "params": {k: best.get(k) for k in ["bb_period", "bb_mult", "rsi_low", "rsi_high", "sl_atr", "tp_atr", "trail_atr"]},
                "train": train_metrics,
                "oos": {k: oos.get(k) for k in ["total_return_pct", "max_dd_pct", "trades", "win_rate_pct", "profit_factor", "sharpe", "sortino"]},
            }
        )

    return rows



def check_signal(ind, enforce_hours=True):
    signals = []
    rsi = ind['rsi']
    trend = ind['trend']
    price = ind['price']
    sma = ind['sma']
    change = ind['change']

    proxy = change

    if proxy < -0.02 and rsi < 45:
        signals.append("Proxy DOWN + RSI")
    if proxy > 0.02 and rsi > 55:
        signals.append("Proxy UP + RSI")
    if rsi < 35:
        signals.append("RSI Oversold")
    if rsi > 65:
        signals.append("RSI Overbought")
    if trend == 1 and price < sma * 1.002:
        signals.append("Trend Up")
    if trend == -1 and price > sma * 0.998:
        signals.append("Trend Down")
    if proxy < -0.03:
        signals.append("Strong Down")
    if proxy > 0.03:
        signals.append("Strong Up")

    if enforce_hours and get_seoul_time().hour not in GOOD_HOURS:
        return 0, signals

    buy_keys = ["Proxy DOWN", "Strong Down", "RSI Oversold", "Trend Up"]
    sell_keys = ["Proxy UP", "Strong Up", "RSI Overbought", "Trend Down"]

    buy = sum(1 for s in signals if any(k in s for k in buy_keys))
    sell = sum(1 for s in signals if any(k in s for k in sell_keys))

    min_signals = 2

    if buy >= min_signals and buy > sell:
        return 1, signals
    if sell >= min_signals and sell > buy:
        return -1, signals

    return 0, signals


def get_intraday_signal(pair, ticker, enforce_hours=True):
    if pair in CRYPTO_PAIRS:
        d15 = update_market_data(pair, tf="15m", bars=1200, min_age_sec=60)
        d1h = update_market_data(pair, tf="1h", bars=2000, min_age_sec=180)
        if d15 is not None and not d15.empty:
            d15 = d15.tail(600)
        if d1h is not None and not d1h.empty:
            d1h = d1h.tail(800)
    else:
        d15 = get_data(ticker, period="5d", interval="15m")
        d1h = get_data(ticker, period="60d", interval="1h")

    if d15 is None or d15.empty or d1h is None or d1h.empty:
        return 0, ["No data"], None, None

    ind15 = get_indicators(d15, pair)
    ind1h = get_indicators(d1h, pair)

    sig15, reasons = check_signal(ind15, enforce_hours=enforce_hours)
    sig = sig15

    reasons = list(reasons)
    reasons.append(f"TF 15m RSI:{ind15['rsi']:.0f} Trend:{'UP' if ind15['trend']==1 else 'DOWN'}")
    reasons.append(f"Confirm 1h RSI:{ind1h['rsi']:.0f} Trend:{'UP' if ind1h['trend']==1 else 'DOWN'}")

    if pair not in CRYPTO_PAIRS:
        if sig15 == 1 and ind15['trend'] != 1:
            reasons.append("Filter: 15m trend not UP")
            sig = 0
        if sig15 == -1 and ind15['trend'] != -1:
            reasons.append("Filter: 15m trend not DOWN")
            sig = 0

        if sig15 == 1 and ind1h['trend'] != 1:
            reasons.append("Filter: 1h trend not UP")
            sig = 0
        if sig15 == -1 and ind1h['trend'] != -1:
            reasons.append("Filter: 1h trend not DOWN")
            sig = 0
    else:
        reasons.append("Crypto mode: trend filters disabled")

    if pair not in CRYPTO_PAIRS:
        if pair == "EURUSD":
            try:
                macro = _dxy_confirmation_for_eurusd(ind15, interval="15m")
                if macro is not None:
                    reasons.append(
                        f"DXY({macro['interval']}): {macro['dxy']:.2f} ret={macro['ret']*100:+.2f}% mom3={macro['mom3']*100:+.2f}% rsi={macro['rsi']:.0f} score={macro['score']}"
                    )
                    for d in (macro.get("details") or [])[:4]:
                        reasons.append(f"DXY confirm: {d}")

                    if sig != 0 and int(macro.get("dir") or 0) != 0 and int(macro.get("dir") or 0) != int(sig):
                        reasons.append("Filter: DXY disagrees")
                        sig = 0
            except Exception:
                pass

        if sig15 == 1 and float(ind1h.get('rsi', 50) or 50) < 48:
            reasons.append("Filter: 1h RSI < 48")
            sig = 0
        if sig15 == -1 and float(ind1h.get('rsi', 50) or 50) > 52:
            reasons.append("Filter: 1h RSI > 52")
            sig = 0

        try:
            lookback = 10
            if len(d15) > lookback + 2:
                prev_high = float(d15['High'].iloc[-(lookback + 1):-1].max())
                prev_low = float(d15['Low'].iloc[-(lookback + 1):-1].min())

                strong_up = any("Strong Up" in s or "Proxy UP" in s for s in reasons)
                strong_dn = any("Strong Down" in s or "Proxy DOWN" in s for s in reasons)

                if sig15 == 1 and float(ind15.get('price')) < prev_high and not strong_up:
                    reasons.append("Filter: no 10-bar breakout")
                    sig = 0
                if sig15 == -1 and float(ind15.get('price')) > prev_low and not strong_dn:
                    reasons.append("Filter: no 10-bar breakdown")
                    sig = 0
        except Exception:
            pass
    else:
        reasons.append("Crypto mode: relaxed filters (no 1h RSI / no breakout)")

    p_up = None
    try:
        model = load_direction_model(pair, tf="15m")
        if model is not None:
            p_up = predict_direction_proba(d15, model)
            if p_up is not None:
                reasons.append(f"Model p(up)={p_up:.2f} acc={float((model.get('metrics') or {}).get('acc') or 0):.2f}")
                if sig == 1 and p_up < 0.52:
                    reasons.append("Filter: model p(up) < 0.52")
                    sig = 0
                if sig == -1 and p_up > 0.48:
                    reasons.append("Filter: model p(up) > 0.48")
                    sig = 0
    except Exception:
        pass

    try:
        gs = _goya_score_local(pair, d15, ind15, ind1h, p_up=p_up)
        sc = int(gs.get("score") or 0) if gs else 0
        ind15["goya_score"] = sc
        v20 = gs.get("vol20") if gs else None
        if v20 is not None:
            reasons.append(f"VitalityScore: {sc:+d} vol20={float(v20)*100:.2f}%")
        else:
            reasons.append(f"VitalityScore: {sc:+d}")

        with config_lock:
            goya_on = bool(CONFIG.get("goya_score_enabled", True))
            min_sc = int(CONFIG.get("goya_min_score", 35) or 0)

        if goya_on and (pair in CRYPTO_PAIRS or pair == "EURUSD"):
            if abs(sc) < int(min_sc):
                if sig != 0:
                    reasons.append(f"Filter: goya_score abs<{int(min_sc)}")
                sig = 0
            else:
                dir_sc = 1 if sc > 0 else -1
                if sig != 0 and dir_sc != int(sig):
                    reasons.append("Filter: goya_score disagrees")
                    sig = 0

        ds = _deepseek_goya_score(pair, "15m", ind15, ind1h, local_score=sc)
        if ds is not None:
            reasons.append(f"DeepSeekScore: {int(ds.get('score') or 0):+d} conf={float(ds.get('confidence') or 0):.2f}")
            for r in (ds.get("reasons") or [])[:4]:
                reasons.append(f"DeepSeek: {r}")

            with config_lock:
                min_conf = float(CONFIG.get("deepseek_min_confidence", 0.6) or 0.6)

            if sig != 0 and float(ds.get("confidence") or 0) >= float(min_conf):
                ddir = int(ds.get("dir") or 0)
                if ddir != 0 and ddir != int(sig):
                    reasons.append("Filter: deepseek disagrees")
                    sig = 0
    except Exception:
        pass

    return sig, reasons, ind15, ind1h


class Account:
    def __init__(self):
        self.balance = CONFIG["initial_balance"]
        self.initial = CONFIG["initial_balance"]
        self.trades = []
        self.positions = []
        self.peak = self.balance
        self.max_dd = 0
        self.last_report = datetime.now()
        self.runtime = {}
        self.load_state()
    
    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)

                self.balance = data.get("balance", CONFIG["initial_balance"])
                self.initial = data.get("initial", CONFIG["initial_balance"])
                self.trades = data.get("trades", []) or []
                self.positions = data.get("positions", []) or []
                self.peak = data.get("peak", self.balance)
                self.max_dd = data.get("max_dd", 0)
                self.runtime = data.get("runtime", {}) or {}

                ts = data.get("last_report_ts")
                if ts:
                    self.last_report = datetime.fromtimestamp(float(ts))
            except Exception:
                pass
    
    def save_state(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "balance": self.balance,
                        "initial": self.initial,
                        "trades": self.trades,
                        "positions": [p for p in self.positions if p.get("status") == "OPEN"],
                        "peak": self.peak,
                        "max_dd": self.max_dd,
                        "last_report_ts": self.last_report.timestamp() if self.last_report else None,
                        "runtime": {
                            **(self.runtime or {}),
                            "pid": os.getpid(),
                            "ts": time.time(),
                            "auto_trade_last_loop_ts": RUNTIME.get("auto_trade_last_loop_ts"),
                            "auto_trade_last_cycle_ts": RUNTIME.get("auto_trade_last_cycle_ts"),
                            "auto_trade_last_error": RUNTIME.get("auto_trade_last_error"),
                            "auto_trade_last_open_ts": RUNTIME.get("auto_trade_last_open_ts"),
                            "auto_trade_last_open_pair": RUNTIME.get("auto_trade_last_open_pair"),
                            "auto_trade_last_candidates": RUNTIME.get("auto_trade_last_candidates"),
                            "auto_trade_last_sample": RUNTIME.get("auto_trade_last_sample"),
                            "bot_poll_last_error": RUNTIME.get("bot_poll_last_error"),
                            "trading_paused_reason": RUNTIME.get("trading_paused_reason"),
                            "day_key": RUNTIME.get("day_key"),
                            "day_start_balance": RUNTIME.get("day_start_balance"),
                            "config_last_reload_ts": RUNTIME.get("config_last_reload_ts"),
                            "config_last_reload_error": RUNTIME.get("config_last_reload_error"),
                        },
                    },
                    f,
                    indent=2,
                )
            os.replace(tmp, STATE_FILE)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    
    def open_trade(self, direction, ind):
        pair = ind['pair']

        with config_lock:
            risk_pct = 10.0
            leverage = float(CONFIG["leverage"])
            trailing_stop = bool(CONFIG.get("trailing_stop", True))
            ts_atr_mult = float(CONFIG.get("trailing_stop_atr_multiplier", 1.5))

        risk = self.balance * (risk_pct / 100.0)
        atr = float(ind.get("atr") or 0.0)
        sl_dist, tp_dist = get_sl_tp_distance(pair, atr=atr)

        if direction == 1:
            sl_price = ind['price'] - sl_dist
            tp_price = ind['price'] + tp_dist
        else:
            sl_price = ind['price'] + sl_dist
            tp_price = ind['price'] - tp_dist

        # Calculate lot size
        sl_pips_fallback = float(CONFIG.get("sl_pips", 100))
        if atr > 0:
            lot = risk / (sl_dist * 10) * leverage
        else:
            lot = risk / (max(sl_pips_fallback, 0.00001) * 10) * leverage
        lot = max(0.01, min(lot, 1.0))
        
        now_ts = time.time()
        pos = {
            'pair': ind['pair'],
            'direction': direction,
            'entry': ind['price'],
            'sl': sl_price,
            'tp': tp_price,
            'lot': lot,
            'risk': risk,
            'open_ts': now_ts,
            'time': datetime.now().strftime('%H:%M'),
            'status': 'OPEN',
            'trailing_stop': trailing_stop,
            'trailing_stop_atr': ts_atr_mult * atr if atr > 0 else None,
            'highest_since_open': ind['price'] if direction == 1 else None,
            'lowest_since_open': ind['price'] if direction == -1 else None,
        }
        self.positions.append(pos)
        _append_trade_event(
            {
                "type": "OPEN",
                "ts": now_ts,
                "pair": pos.get("pair"),
                "direction": int(pos.get("direction") or 0),
                "entry": float(pos.get("entry") or 0.0),
                "sl": float(pos.get("sl") or 0.0),
                "tp": float(pos.get("tp") or 0.0),
                "risk": float(pos.get("risk") or 0.0),
                "lot": float(pos.get("lot") or 0.0),
            }
        )
        self.save_state()
        return pos
    
    def check_all_positions(self, prices):
        closed = []
        for pos in self.positions[:]:
            if pos['status'] != 'OPEN':
                continue
            pair = pos['pair']
            if pair not in prices:
                continue
            price = prices[pair]
            with config_lock:
                sl_pips = float(CONFIG.get("sl_pips", 100))
                tp_pips = float(CONFIG.get("tp_pips", 300))

            # Update trailing stop
            if pos.get("trailing_stop"):
                if pos['direction'] == 1:
                    # Update highest since open
                    if pos.get("highest_since_open") is None or price > pos["highest_since_open"]:
                        pos["highest_since_open"] = price
                    # Move SL up if trailing stop triggered
                    ts_dist = pos.get("trailing_stop_atr")
                    if ts_dist is not None:
                        new_sl = pos["highest_since_open"] - ts_dist
                        if new_sl > pos["sl"]:
                            pos["sl"] = new_sl
                else:
                    # Update lowest since open
                    if pos.get("lowest_since_open") is None or price < pos["lowest_since_open"]:
                        pos["lowest_since_open"] = price
                    # Move SL down if trailing stop triggered
                    ts_dist = pos.get("trailing_stop_atr")
                    if ts_dist is not None:
                        new_sl = pos["lowest_since_open"] + ts_dist
                        if new_sl < pos["sl"]:
                            pos["sl"] = new_sl

            # Check TP/SL
            if pos['direction'] == 1:
                if price >= pos['tp']:
                    # Use ATR multipliers for RR if available
                    sl_mult = float(CONFIG.get("sl_atr_multiplier", 2.0))
                    tp_mult = float(CONFIG.get("tp_atr_multiplier", 6.0))
                    pnl = pos['risk'] * (tp_mult / sl_mult)
                    pos['status'] = 'WIN'
                    pos['pnl'] = pnl
                    self.balance += pnl
                    closed.append(pos)
                elif price <= pos['sl']:
                    pos['status'] = 'LOSS'
                    pos['pnl'] = -pos['risk']
                    self.balance += pos['pnl']
                    closed.append(pos)
            else:
                if price <= pos['tp']:
                    # Use ATR multipliers for RR if available
                    sl_mult = float(CONFIG.get("sl_atr_multiplier", 2.0))
                    tp_mult = float(CONFIG.get("tp_atr_multiplier", 6.0))
                    pnl = pos['risk'] * (tp_mult / sl_mult)
                    pos['status'] = 'WIN'
                    pos['pnl'] = pnl
                    self.balance += pnl
                    closed.append(pos)
                elif price >= pos['sl']:
                    pos['status'] = 'LOSS'
                    pos['pnl'] = -pos['risk']
                    self.balance += pos['pnl']
                    closed.append(pos)
        
        close_ts = time.time()
        for pos in closed:
            if pos.get("close_ts") is None:
                pos["close_ts"] = close_ts
                try:
                    pos["close_price"] = float(prices.get(pos.get("pair")))
                except Exception:
                    pass
            self.positions.remove(pos)
            self.trades.append(pos)
            _append_trade_event(
                {
                    "type": "CLOSE",
                    "ts": float(pos.get("close_ts") or close_ts),
                    "pair": pos.get("pair"),
                    "direction": int(pos.get("direction") or 0),
                    "entry": float(pos.get("entry") or 0.0),
                    "close": float(pos.get("close_price") or 0.0),
                    "status": pos.get("status"),
                    "pnl": float(pos.get("pnl") or 0.0),
                    "risk": float(pos.get("risk") or 0.0),
                }
            )
        self.save_state()
        return closed
    
    def _mtm_pnl(self, pos, price):
        with config_lock:
            sl_pips = float(CONFIG["sl_pips"])
            tp_pips = float(CONFIG["tp_pips"])

        risk = float(pos.get("risk") or 0.0)
        max_profit = risk * (tp_pips / max(sl_pips, 0.00001))

        entry = float(pos.get("entry") or 0.0)
        sl = float(pos.get("sl") or entry)

        if int(pos.get("direction") or 0) == 1:
            sl_move = max(entry - sl, 1e-9)
            rr = (float(price) - entry) / sl_move
        else:
            sl_move = max(sl - entry, 1e-9)
            rr = (entry - float(price)) / sl_move

        pnl = risk * rr
        pnl = max(-risk, min(pnl, max_profit))
        return float(pnl)

    def force_close_all(self, prices, reason="MANUAL"):
        if not prices:
            return []
        closed = []
        close_ts = time.time()
        for pos in self.positions[:]:
            if pos.get("status") != "OPEN":
                continue
            pair = pos.get("pair")
            if pair not in prices:
                continue
            price = float(prices[pair])

            pnl = self._mtm_pnl(pos, price)
            pos["status"] = str(reason)
            pos["pnl"] = pnl
            pos["close_ts"] = close_ts
            pos["close_price"] = price

            self.balance += pnl
            closed.append(pos)
            self.positions.remove(pos)
            self.trades.append(pos)
            _append_trade_event(
                {
                    "type": "CLOSE",
                    "ts": float(pos.get("close_ts") or close_ts),
                    "pair": pos.get("pair"),
                    "direction": int(pos.get("direction") or 0),
                    "entry": float(pos.get("entry") or 0.0),
                    "close": float(pos.get("close_price") or 0.0),
                    "status": pos.get("status"),
                    "pnl": float(pos.get("pnl") or 0.0),
                    "risk": float(pos.get("risk") or 0.0),
                }
            )

        if closed:
            self.save_state()

        return closed

    def stats(self):
        total = len(self.trades)
        wins = len([t for t in self.trades if t['status'] == 'WIN'])
        wr = (wins / total * 100) if total > 0 else 0
        if self.balance > self.peak:
            self.peak = self.balance
        dd = (self.peak - self.balance) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd
        return {
            'balance': self.balance,
            'return': ((self.balance / self.initial) - 1) * 100,
            'trades': total, 'wins': wins, 'wr': wr,
            'peak': self.peak, 'max_dd': self.max_dd,
            'open': len([p for p in self.positions if p['status'] == 'OPEN']),
        }

bot = telebot.TeleBot(BOT_TOKEN)
telebot.logger.setLevel(logging.CRITICAL)
telebot.logger.propagate = False
account = Account()
current_prices = {}

DASHBOARD_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>LocalTRBot Dashboard</title>
  <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
  <script src=\"https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js\"></script>
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:24px;max-width:1100px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .card{border:1px solid #ddd;border-radius:10px;padding:14px}
    label{display:block;margin:10px 0 4px;font-size:13px;color:#333}
    input,select{width:100%;padding:8px;border:1px solid #ccc;border-radius:8px}
    button{padding:10px 12px;border:0;border-radius:8px;background:#111;color:#fff;cursor:pointer}
    button.ghost{background:#fff;color:#111;border:1px solid #ddd}
    .row{display:flex;gap:10px}
    .row>div{flex:1}
    .muted{color:#666;font-size:13px}
    pre{white-space:pre-wrap}

    .fab{position:fixed;right:18px;bottom:18px;z-index:50;border-radius:999px;padding:12px 14px;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.18)}
    .backdrop{position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:40;opacity:0;pointer-events:none;transition:opacity .18s ease}
    .backdrop.open{opacity:1;pointer-events:auto}
    .drawer{position:fixed;top:0;right:0;height:100%;width:min(420px, 92vw);background:#fff;border-left:1px solid #ddd;z-index:60;transform:translateX(100%);transition:transform .18s ease;overflow:auto;padding:16px}
    .drawer.open{transform:translateX(0)}

    .carousel{margin-top:10px}
    .slide{display:none}
    .slide.active{display:block}
  </style>
</head>
<body>
  <h2>LocalTRBot Dashboard</h2>
  <div class=\"muted\">Bot + Dashboard running as one service (Railway-friendly).</div>
  <div class=\"muted\" id=\"page_error\" style=\"margin-top:8px;color:#d1242f\"></div>

  <div class=\"grid\" style=\"margin-top:16px\">
    <div class=\"card\"><h3>Equity</h3><canvas id=\"equity\"></canvas></div>
    <div class=\"card\"><h3>Win/Loss</h3><canvas id=\"wl\"></canvas></div>
  </div>

  <div class=\"grid\" style=\"margin-top:16px\">
    <div class=\"card\" style=\"grid-column:1 / -1\">
      <h3>Wave Analysis (10Y) + Entry Signals</h3>
      <div class=\"row\">
        <div>
          <label>Pair</label>
          <select id=\"pair_select\"></select>
        </div>
        <div>
          <label>TF</label>
          <select id=\"tf_select\">
            <option value=\"15m\">15M</option>
            <option value=\"1h\">1H</option>
          </select>
        </div>
        <div>
          <label>ZigZag</label>
          <input id=\"zz_mult\" type=\"range\" min=\"0.5\" max=\"3\" step=\"0.1\" value=\"1\" />
          <div class=\"muted\" id=\"zz_val\">1.0x</div>
        </div>
        <div>
          <label>Only Valid</label>
          <input id=\"only_valid\" type=\"checkbox\" />
        </div>
        <div>
          <label>Signal</label>
          <div class=\"muted\" id=\"signal_box\">Loading...</div>
        </div>
      </div>
      <div id=\"wavechart\" style=\"height:420px;margin-top:10px\"></div>
      <div class=\"muted\" id=\"plan_box\" style=\"margin-top:8px\"></div>
      <div class=\"muted\" id=\"wave_meta\" style=\"margin-top:8px\"></div>
    </div>
  </div>

  <button id=\"ctrlBtn\" class=\"fab\" type=\"button\" onclick=\"toggleCtrl()\">Control</button>
  <div id=\"ctrlBackdrop\" class=\"backdrop\" onclick=\"toggleCtrl(false)\"></div>
  <div id=\"ctrlDrawer\" class=\"drawer\">
    <h3 style=\"margin:0\">Control Panel</h3>
    <div class=\"muted\" style=\"margin-top:6px\">Настройки бота</div>

    <div class=\"row\" style=\"margin-top:10px\">
      <div>
        <label>Risk per trade (%)</label>
        <input id=\"risk_per_trade\" type=\"number\" step=\"0.1\" />
      </div>
      <div>
        <label>Trades per pair</label>
        <input id=\"trades_per_pair\" type=\"number\" step=\"1\" />
      </div>
    </div>
    <div class=\"row\">
      <div>
        <label>SL (pips / $)</label>
        <input id=\"sl_pips\" type=\"number\" step=\"0.01\" />
      </div>
      <div>
        <label>TP (pips / $)</label>
        <input id=\"tp_pips\" type=\"number\" step=\"0.01\" />
      </div>
    </div>
    <div class=\"row\">
      <div>
        <label>Leverage</label>
        <input id=\"leverage\" type=\"number\" step=\"1\" />
      </div>
      <div>
        <label>Check interval (sec)</label>
        <input id=\"check_interval\" type=\"number\" step=\"1\" />
      </div>
    </div>
    <label>Auto-trade enabled (true/false)</label>
    <input id=\"auto_trade_enabled\" />

    <div class=\"row\" style=\"margin-top:12px\">
      <button type=\"button\" onclick=\"saveCfg()\">Save settings</button>
      <button type=\"button\" class=\"ghost\" onclick=\"toggleCtrl(false)\">Close</button>
    </div>
    <div class=\"muted\" id=\"saveMsg\" style=\"margin-top:8px\"></div>
  </div>

  <div class=\"grid\" style=\"margin-top:16px\">
    <div class=\"card\" style=\"grid-column:1 / -1\">
      <div class=\"row\" style=\"align-items:center;justify-content:space-between\">
        <h3 style=\"margin:0\">Stats</h3>
        <div class=\"row\" style=\"justify-content:flex-end\">
          <button type=\"button\" class=\"ghost\" onclick=\"prevSlide()\">Prev</button>
          <button type=\"button\" class=\"ghost\" onclick=\"nextSlide()\">Next</button>
        </div>
      </div>
      <div class=\"carousel\">
        <div class=\"slide active\" data-slide=\"0\"><pre id=\"stats\">Loading...</pre></div>
        <div class=\"slide\" data-slide=\"1\"><pre id=\"stats_positions\">Loading...</pre></div>
        <div class=\"slide\" data-slide=\"2\"><pre id=\"stats_trades\">Loading...</pre></div>
      </div>
      <div class=\"muted\" id=\"carousel_label\" style=\"margin-top:8px\"></div>
    </div>
  </div>

<script>
let equityChart, wlChart;
let tvChart, candleSeries, waveSeries;
let priceLines = [];
let openPosByPair = {};
let historyLoadedOnce = false;
let ctrlOpen = false;
let slideIndex = 0;
let carouselInit = false;
const slideTitles = ['State', 'Open Positions', 'Last Trades'];

function setText(id, t){
  const el = document.getElementById(id);
  if (el) el.textContent = t;
}

function toggleCtrl(force){
  ctrlOpen = (typeof force === 'boolean') ? force : !ctrlOpen;
  const d = document.getElementById('ctrlDrawer');
  const b = document.getElementById('ctrlBackdrop');
  if (d) d.classList.toggle('open', ctrlOpen);
  if (b) b.classList.toggle('open', ctrlOpen);
}

function setSlide(i){
  const slides = Array.from(document.querySelectorAll('.slide'));
  if (!slides.length) return;
  slideIndex = (i + slides.length) % slides.length;
  for (const s of slides){
    const n = Number(s.getAttribute('data-slide') || 0);
    s.classList.toggle('active', n === slideIndex);
  }
  setText('carousel_label', slideTitles[slideIndex] || '');
}

function nextSlide(){
  setSlide(slideIndex + 1);
}

function prevSlide(){
  setSlide(slideIndex - 1);
}

function ensurePairs(pairs){
  const sel = document.getElementById('pair_select');
  if (!sel || !Array.isArray(pairs)) return;
  if (sel.options.length) return;

  for (const p of pairs){
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = p;
    sel.appendChild(opt);
  }

  sel.addEventListener('change', () => loadHistory(sel.value));
}

function ensureWaveChart(){
  if (tvChart) return;
  const container = document.getElementById('wavechart');
  if (!container || !window.LightweightCharts) return;

  tvChart = LightweightCharts.createChart(container, {
    layout: { background: { type: 'solid', color: '#ffffff' }, textColor: '#111111' },
    grid: { vertLines: { color: '#eeeeee' }, horzLines: { color: '#eeeeee' } },
    rightPriceScale: { borderVisible: false },
    timeScale: { borderVisible: false },
  });

  candleSeries = tvChart.addCandlestickSeries({
    upColor: '#1a7f37', downColor: '#d1242f',
    wickUpColor: '#1a7f37', wickDownColor: '#d1242f',
    borderVisible: false,
  });

  waveSeries = tvChart.addLineSeries({ color: '#111111', lineWidth: 2 });
}

async function loadHistory(pair){
  if (!pair) return;
  const tfSel = document.getElementById('tf_select');
  const tf = tfSel ? tfSel.value : '15m';

  const zzEl = document.getElementById('zz_mult');
  const zz = zzEl ? Number(zzEl.value) : 1.0;
  setText('zz_val', (Number.isFinite(zz) ? zz.toFixed(1) : '1.0') + 'x');

  const onlyValid = !!(document.getElementById('only_valid') && document.getElementById('only_valid').checked);

  setText('signal_box', 'Loading...');
  setText('plan_box', '');
  setText('wave_meta', '');

  const res = await fetch('/api/history?pair=' + encodeURIComponent(pair) + '&tf=' + encodeURIComponent(tf) + '&limit=2000&zz=' + encodeURIComponent(String(zz || 1.0)));
  const data = await res.json();
  if (data && data.ok === false) throw new Error(data.error || 'history error');

  ensureWaveChart();
  if (!tvChart) return;

  const candles = Array.isArray(data.candles) ? data.candles : [];
  candleSeries.setData(candles);

  if (priceLines.length){
    for (const pl of priceLines){
      try{ candleSeries.removePriceLine(pl); }catch(e){}
    }
    priceLines = [];
  }

  const dashed = (window.LightweightCharts && LightweightCharts.LineStyle && LightweightCharts.LineStyle.Dashed) || 2;
  const posList = (openPosByPair && openPosByPair[pair]) ? openPosByPair[pair] : [];
  for (const p of posList){
    const side = p.direction === 1 ? 'LONG' : 'SHORT';
    try{
      priceLines.push(candleSeries.createPriceLine({ price: Number(p.entry), color: 'rgba(17,17,17,.85)', lineWidth: 2, lineStyle: dashed, axisLabelVisible: true, title: side + ' Entry' }));
      priceLines.push(candleSeries.createPriceLine({ price: Number(p.sl), color: 'rgba(209,36,47,.85)', lineWidth: 2, lineStyle: dashed, axisLabelVisible: true, title: 'SL' }));
      priceLines.push(candleSeries.createPriceLine({ price: Number(p.tp), color: 'rgba(26,127,55,.85)', lineWidth: 2, lineStyle: dashed, axisLabelVisible: true, title: 'TP' }));
    }catch(e){}
  }

  const plan = data.plan || {};
  if ((plan.direction === 1 || plan.direction === -1) && !posList.length){
    try{
      priceLines.push(candleSeries.createPriceLine({ price: Number(plan.entry), color: 'rgba(17,17,17,.85)', lineWidth: 2, lineStyle: dashed, axisLabelVisible: true, title: 'Plan Entry' }));
      priceLines.push(candleSeries.createPriceLine({ price: Number(plan.sl), color: 'rgba(209,36,47,.85)', lineWidth: 2, lineStyle: dashed, axisLabelVisible: true, title: 'Plan SL' }));
      priceLines.push(candleSeries.createPriceLine({ price: Number(plan.tp), color: 'rgba(26,127,55,.85)', lineWidth: 2, lineStyle: dashed, axisLabelVisible: true, title: 'Plan TP' }));
    }catch(e){}
  }

  const imp = data.elliott && data.elliott.impulse ? data.elliott.impulse : null;
  const impOk = imp ? !!imp.ok : null;

  const swings = Array.isArray(data.swings) ? data.swings : [];
  if (onlyValid && impOk === false){
    waveSeries.setData([]);
    candleSeries.setMarkers([]);
  }else{
    waveSeries.setData(swings.map(p => ({ time: p.time, value: p.price })));
    candleSeries.setMarkers(swings.map(p => ({
      time: p.time,
      position: (p.kind === 'H') ? 'aboveBar' : 'belowBar',
      color: '#111111',
      shape: (p.kind === 'H') ? 'arrowDown' : 'arrowUp',
      text: p.label || '',
    })));
  }

  let sigTxt = 'No signal';
  if (data.signal === 1) sigTxt = 'BUY signal';
  if (data.signal === -1) sigTxt = 'SELL signal';
  const reasons = Array.isArray(data.signal_reasons) ? data.signal_reasons : [];
  if (reasons.length) sigTxt += ' • ' + reasons.join(', ');
  setText('signal_box', sigTxt);

  const plan = data.plan || {};
  if (plan.direction === 1 || plan.direction === -1){
    const side = plan.direction === 1 ? 'LONG' : 'SHORT';
    const rr = (typeof plan.rr === 'number') ? plan.rr.toFixed(2) : '—';
    setText('plan_box', `Plan: ${side} Entry ${Number(plan.entry).toFixed(5)} • SL ${Number(plan.sl).toFixed(5)} • TP ${Number(plan.tp).toFixed(5)} • RR ${rr}`);
  }else{
    setText('plan_box', 'Plan: —');
  }

  const last = data.last || {};
  const imp = data.elliott && data.elliott.impulse ? data.elliott.impulse : null;
  const impTxt = imp ? (imp.ok ? 'Impulse OK' : ('Impulse INVALID: ' + ((imp.errors || []).join('; ') || 'rules'))) : 'Impulse: —';
  const warnTxt = imp && imp.warnings && imp.warnings.length ? (' • ' + imp.warnings.join('; ')) : '';
  const meta = `Pair ${data.pair || pair} • TF ${data.tf || '—'} • Bars ${candles.length} • RSI ${Number(last.rsi).toFixed(1)} • Price ${Number(last.price).toFixed(5)} • ${impTxt}${warnTxt}`;
  setText('wave_meta', meta);

  tvChart.timeScale().fitContent();
}

async function loadAll(){
  try{
    setText('page_error', '');
    const st = await fetch('/api/state').then(r=>r.json());
    const statsEl = document.getElementById('stats');
    if (statsEl) statsEl.textContent = JSON.stringify(st, null, 2);

  const posEl = document.getElementById('stats_positions');
  if (posEl) posEl.textContent = JSON.stringify(st.open_positions || [], null, 2);

  const opens = Array.isArray(st.open_positions) ? st.open_positions : [];
  openPosByPair = {};
  for (const p of opens){
    const k = p && p.pair ? p.pair : '?';
    if (!openPosByPair[k]) openPosByPair[k] = [];
    openPosByPair[k].push(p);
  }

  const trEl = document.getElementById('stats_trades');
  if (trEl) trEl.textContent = JSON.stringify(st.last_trades || [], null, 2);

  if (!carouselInit){
    carouselInit = true;
    setSlide(0);
  }

  for (const k of ['risk_per_trade','trades_per_pair','sl_pips','tp_pips','leverage','check_interval','auto_trade_enabled']){
    document.getElementById(k).value = st.config[k];
  }

  ensurePairs(st.pairs || []);

  const sel = document.getElementById('pair_select');
  if (sel && !sel.value && sel.options.length) sel.value = sel.options[0].value;

  const tfSel = document.getElementById('tf_select');
  if (tfSel && !tfSel.dataset.bound){
    tfSel.dataset.bound = '1';
    tfSel.addEventListener('change', () => {
      if (sel && sel.value) loadHistory(sel.value).catch(e => setText('signal_box', 'Error: ' + (e?.message || String(e))));
    });
  }

  if (sel && sel.value && !historyLoadedOnce){
    historyLoadedOnce = true;
    loadHistory(sel.value).catch(e => setText('signal_box', 'Error: ' + (e?.message || String(e))));
  }

  const eq = await fetch('/api/equity').then(r=>r.json());

  const ctx = document.getElementById('equity');
  if (equityChart) equityChart.destroy();
  equityChart = new Chart(ctx, {
    type: 'line',
    data: { labels: eq.labels, datasets: [{ label:'Balance', data:eq.balances, borderColor:'#111', tension:0.2 }]},
    options: { responsive:true, plugins:{legend:{display:true}} }
  });

    const ctx2 = document.getElementById('wl');
    if (wlChart) wlChart.destroy();
    wlChart = new Chart(ctx2, {
      type:'doughnut',
      data:{ labels:['WIN','LOSS'], datasets:[{ data:[st.stats.wins, st.stats.trades - st.stats.wins], backgroundColor:['#1a7f37','#d1242f'] }]},
      options:{ responsive:true }
    });
  }catch(e){
    setText('page_error', 'Dashboard error: ' + (e?.message || String(e)));
  }
}

async function saveCfg(){
  const payload = {};
  for (const k of ['risk_per_trade','trades_per_pair','sl_pips','tp_pips','leverage','check_interval','auto_trade_enabled']){
    payload[k] = document.getElementById(k).value;
  }
  const res = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const out = await res.json();
  document.getElementById('saveMsg').textContent = out.ok ? 'Saved' : ('Error: ' + (out.error || 'unknown'));
  await loadAll();
}

function safeLoadAll(){
  loadAll().catch(e => setText('page_error', 'Dashboard error: ' + (e?.message || String(e))));
}

window.addEventListener('error', (e) => {
  const msg = (e && e.message) ? e.message : 'script error';
  setText('page_error', 'Dashboard error: ' + msg);
});
window.addEventListener('unhandledrejection', (e) => {
  const msg = (e && e.reason && e.reason.message) ? e.reason.message : String(e.reason || 'promise rejection');
  setText('page_error', 'Dashboard error: ' + msg);
});

safeLoadAll();
setInterval(safeLoadAll, 15000);
</script>
</body>
</html>"""


def build_equity_series(initial, trades):
    labels = ["0"]
    balances = [float(initial)]
    bal = float(initial)
    for i, t in enumerate(trades, start=1):
        bal += float(t.get("pnl", 0) or 0)
        labels.append(str(i))
        balances.append(bal)
    return labels, balances


def build_pair_stats(trades):
    out = {}
    for t in trades:
        pair = t.get("pair", "?")
        out.setdefault(pair, {"trades": 0, "wins": 0, "pnl": 0.0})
        out[pair]["trades"] += 1
        if t.get("status") == "WIN":
            out[pair]["wins"] += 1
        out[pair]["pnl"] += float(t.get("pnl", 0) or 0)
    for pair, s in out.items():
        s["wr"] = (s["wins"] / s["trades"] * 100.0) if s["trades"] else 0.0
    return out


_HISTORY_CACHE = {}
_HISTORY_CACHE_LOCK = threading.Lock()


def _pair_is_crypto(pair):
    return pair in CRYPTO_PAIRS


def _history_threshold(pair, tf):
    tf = (tf or "15m").strip().lower()
    if _pair_is_crypto(pair):
        return 0.02 if tf == "15m" else 0.03
    return 0.006 if tf == "15m" else 0.012


def _df_to_candles(df):
    out = []
    if df is None or df.empty:
        return out

    idx = df.index
    for i in range(len(df)):
        ts = idx[i]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        t = int(ts.replace(tzinfo=timezone.utc).timestamp())
        out.append(
            {
                "time": t,
                "open": float(df["Open"].iloc[i]),
                "high": float(df["High"].iloc[i]),
                "low": float(df["Low"].iloc[i]),
                "close": float(df["Close"].iloc[i]),
                "volume": float(df["Volume"].iloc[i]) if "Volume" in df.columns else 0.0,
            }
        )
    return out


def _zigzag_swings(candles, threshold):
    if not candles:
        return []

    prices = [c["close"] for c in candles]
    times = [c["time"] for c in candles]

    direction = 0
    last_pivot_i = 0
    extreme_i = 0
    extreme_p = prices[0]
    pivots = []

    for i in range(1, len(prices)):
        p = prices[i]
        base = prices[last_pivot_i] if prices[last_pivot_i] else 1e-9
        chg = (p - prices[last_pivot_i]) / base

        if direction == 0:
            if abs(chg) >= threshold:
                direction = 1 if chg > 0 else -1
                extreme_i = i
                extreme_p = p
            continue

        if direction == 1:
            if p >= extreme_p:
                extreme_i = i
                extreme_p = p
                continue
            if (extreme_p - p) / (extreme_p if extreme_p else 1e-9) >= threshold:
                pivots.append({"time": times[extreme_i], "price": float(extreme_p), "kind": "H"})
                last_pivot_i = extreme_i
                direction = -1
                extreme_i = i
                extreme_p = p
            continue

        if direction == -1:
            if p <= extreme_p:
                extreme_i = i
                extreme_p = p
                continue
            if (p - extreme_p) / (extreme_p if extreme_p else 1e-9) >= threshold:
                pivots.append({"time": times[extreme_i], "price": float(extreme_p), "kind": "L"})
                last_pivot_i = extreme_i
                direction = 1
                extreme_i = i
                extreme_p = p
            continue

    if pivots:
        last_kind = pivots[-1]["kind"]
        pivots.append(
            {
                "time": times[extreme_i],
                "price": float(extreme_p),
                "kind": "H" if last_kind == "L" else "L",
            }
        )

    labels = ["1", "2", "3", "4", "5", "A", "B", "C"]
    if len(pivots) >= 2:
        start = max(0, len(pivots) - len(labels))
        j = 0
        for i in range(start, len(pivots)):
            pivots[i]["label"] = labels[j]
            j += 1

    return pivots


def _elliott_impulse_check(swings):
    out = {
        "ok": False,
        "direction": None,
        "errors": [],
        "warnings": [],
        "metrics": {},
        "points": [],
    }

    if not swings or len(swings) < 6:
        out["errors"].append("Need at least 6 swing points for a 1-5 impulse candidate")
        return out

    pts = swings[-6:]
    kinds = [p.get("kind") for p in pts]

    if kinds == ["L", "H", "L", "H", "L", "H"]:
        direction = 1
    elif kinds == ["H", "L", "H", "L", "H", "L"]:
        direction = -1
    else:
        out["errors"].append("Swings do not match an alternating 6-point impulse skeleton")
        return out

    out["direction"] = direction

    p0, p1, p2, p3, p4, p5 = pts
    s0 = float(p0.get("price", 0) or 0)
    w1_end = float(p1.get("price", 0) or 0)
    w2_end = float(p2.get("price", 0) or 0)
    w3_end = float(p3.get("price", 0) or 0)
    w4_end = float(p4.get("price", 0) or 0)
    w5_end = float(p5.get("price", 0) or 0)

    if direction == 1:
        if w2_end <= s0:
            out["errors"].append("Invalid impulse: Wave 2 reached/overlapped the start of Wave 1")
    else:
        if w2_end >= s0:
            out["errors"].append("Invalid impulse: Wave 2 reached/overlapped the start of Wave 1")

    w1_len = abs(w1_end - s0)
    w2_ret = abs(w1_end - w2_end)
    w3_len = abs(w3_end - w2_end)
    w5_len = abs(w5_end - w4_end)

    if w1_len > 0:
        out["metrics"]["wave2_retrace_pct"] = (w2_ret / w1_len) * 100.0
        if out["metrics"]["wave2_retrace_pct"] > 95:
            out["warnings"].append("Recommendation: Wave 2 retrace is very deep; consider correction instead of impulse")

    if w1_len > 0 and w3_len > 0 and w5_len > 0:
        if w3_len <= min(w1_len, w5_len):
            out["warnings"].append("Recommendation: Wave 3 should not be the shortest among Waves 1, 3, 5")

    if direction == 1:
        if w4_end <= w1_end:
            out["warnings"].append("Recommendation: Wave 4 should not overlap Wave 1 territory (non-diagonal impulse)")
    else:
        if w4_end >= w1_end:
            out["warnings"].append("Recommendation: Wave 4 should not overlap Wave 1 territory (non-diagonal impulse)")

    out["ok"] = len(out["errors"]) == 0
    out["points"] = [
        {"label": "0", "time": p0.get("time"), "price": s0},
        {"label": "1", "time": p1.get("time"), "price": w1_end},
        {"label": "2", "time": p2.get("time"), "price": w2_end},
        {"label": "3", "time": p3.get("time"), "price": w3_end},
        {"label": "4", "time": p4.get("time"), "price": w4_end},
        {"label": "5", "time": p5.get("time"), "price": w5_end},
    ]

    return out


def _apply_impulse_labels(swings, impulse):
    out = [dict(p) for p in (swings or [])]

    for p in out:
        if p.get("label") in ["1", "2", "3", "4", "5"]:
            p.pop("label", None)

    if not impulse or not impulse.get("ok") or len(out) < 6:
        return out

    pts = out[-6:]
    pts[1]["label"] = "1"
    pts[2]["label"] = "2"
    pts[3]["label"] = "3"
    pts[4]["label"] = "4"
    pts[5]["label"] = "5"

    return out


def get_fib_levels(swings, impulse=None):
    levels = []

    def _add(name, ratio, price, kind):
        try:
            levels.append({"name": str(name), "ratio": float(ratio), "price": float(price), "kind": str(kind)})
        except Exception:
            pass

    def _from_move(a, b, direction, kind_prefix):
        a = float(a)
        b = float(b)
        move = abs(b - a)
        if move <= 0:
            return
        if direction == 1:
            base = b
            for r in [0.236, 0.382, 0.5, 0.618, 0.786]:
                _add(f"{kind_prefix}R{r:g}", r, base - move * r, "retrace")
        else:
            base = b
            for r in [0.236, 0.382, 0.5, 0.618, 0.786]:
                _add(f"{kind_prefix}R{r:g}", r, base + move * r, "retrace")

    def _extensions(p2, a, b, direction, kind_prefix):
        a = float(a)
        b = float(b)
        p2 = float(p2)
        move = abs(b - a)
        if move <= 0:
            return
        if direction == 1:
            for r in [1.0, 1.272, 1.618, 2.0, 2.618]:
                _add(f"{kind_prefix}E{r:g}", r, p2 + move * r, "extension")
        else:
            for r in [1.0, 1.272, 1.618, 2.0, 2.618]:
                _add(f"{kind_prefix}E{r:g}", r, p2 - move * r, "extension")

    if impulse and isinstance(impulse, dict) and impulse.get("ok") and impulse.get("points") and impulse.get("direction") in [1, -1]:
        pts = impulse.get("points") or []
        if len(pts) >= 3:
            direction = int(impulse.get("direction") or 0)
            p0 = float(pts[0].get("price") or 0.0)
            p1 = float(pts[1].get("price") or 0.0)
            p2 = float(pts[2].get("price") or 0.0)
            _from_move(p0, p1, direction, "W1-")
            _extensions(p2, p0, p1, direction, "W3-")

    return levels


def _history_key(ticker, tf):
    return f"{ticker}|{tf}"


def _history_params(tf):
    tf = (tf or "15m").strip().lower()
    if tf in ["15m", "15min", "15minute", "m15"]:
        return "60d", "15m", "15m"
    if tf in ["1h", "60m", "hour", "h1"]:
        return "60d", "1h", "1h"
    return "60d", "15m", "15m"


def get_history(pair, tf="1d"):
    if pair not in PAIRS:
        raise ValueError("unknown pair")

    period, interval, tf_norm = _history_params(tf)
    ticker = PAIRS[pair]

    if pair in CRYPTO_PAIRS and tf_norm in ["15m", "1h"]:
        df = update_market_data(pair, tf=tf_norm, bars=3000, min_age_sec=60 if tf_norm == "15m" else 180)
        if df is None or df.empty or len(df) < 50:
            raise ValueError("no data")
        return df, tf_norm

    if isinstance(ticker, (list, tuple, set)):
        ticker = list(ticker)[0] if ticker else ""

    now = time.time()
    key = _history_key(ticker, tf_norm)

    ttl = 3600
    if tf_norm == "15m":
        ttl = 900

    with _HISTORY_CACHE_LOCK:
        hit = _HISTORY_CACHE.get(key)
        if hit and (now - hit.get("ts", 0)) < ttl:
            return hit["df"], tf_norm

    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period, interval=interval)
        if df is None or df.empty or len(df) < 50:
            raise ValueError("no data")
        df = df.dropna()

        with _HISTORY_CACHE_LOCK:
            _HISTORY_CACHE[key] = {"ts": now, "df": df}

        return df, tf_norm
    except Exception:
        with _HISTORY_CACHE_LOCK:
            hit = _HISTORY_CACHE.get(key)
            if hit:
                return hit["df"], tf_norm
        raise


def get_history_10y(pair):
    df, _ = get_history(pair, tf="1d")
    return df


def get_public_config():
    with config_lock:
        return {k: CONFIG.get(k) for k in CONFIG_DEFAULTS.keys()}


def apply_config_patch(patch):
    def to_bool(v):
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ["1", "true", "yes", "y", "on"]:
            return True
        if s in ["0", "false", "no", "n", "off"]:
            return False
        raise ValueError("auto_trade_enabled must be boolean")

    out = {}
    if "risk_per_trade" in patch:
        out["risk_per_trade"] = float(patch["risk_per_trade"])
        if out["risk_per_trade"] <= 0 or out["risk_per_trade"] > 100:
            raise ValueError("risk_per_trade must be in (0, 100]")
    if "trades_per_pair" in patch:
        out["trades_per_pair"] = int(float(patch["trades_per_pair"]))
        if out["trades_per_pair"] < 0 or out["trades_per_pair"] > 20:
            raise ValueError("trades_per_pair must be in [0, 20]")
    if "max_total_positions" in patch:
        out["max_total_positions"] = int(float(patch["max_total_positions"]))
        if out["max_total_positions"] < 0 or out["max_total_positions"] > 50:
            raise ValueError("max_total_positions must be in [0, 50]")
    if "sl_atr_multiplier" in patch:
        out["sl_atr_multiplier"] = float(patch["sl_atr_multiplier"])
        if out["sl_atr_multiplier"] <= 0 or out["sl_atr_multiplier"] > 20:
            raise ValueError("sl_atr_multiplier must be in (0, 20]")
    if "tp_atr_multiplier" in patch:
        out["tp_atr_multiplier"] = float(patch["tp_atr_multiplier"])
        if out["tp_atr_multiplier"] <= 0 or out["tp_atr_multiplier"] > 50:
            raise ValueError("tp_atr_multiplier must be in (0, 50]")
    if "trailing_stop" in patch:
        out["trailing_stop"] = to_bool(patch["trailing_stop"])
    if "trailing_stop_atr_multiplier" in patch:
        out["trailing_stop_atr_multiplier"] = float(patch["trailing_stop_atr_multiplier"])
        if out["trailing_stop_atr_multiplier"] <= 0 or out["trailing_stop_atr_multiplier"] > 20:
            raise ValueError("trailing_stop_atr_multiplier must be in (0, 20]")
    if "sl_pips" in patch:
        out["sl_pips"] = float(patch["sl_pips"])
        if out["sl_pips"] <= 0:
            raise ValueError("sl_pips must be > 0")
    if "tp_pips" in patch:
        out["tp_pips"] = float(patch["tp_pips"])
        if out["tp_pips"] <= 0:
            raise ValueError("tp_pips must be > 0")
    if "leverage" in patch:
        out["leverage"] = float(patch["leverage"])
        if out["leverage"] <= 0 or out["leverage"] > 1000:
            raise ValueError("leverage must be in (0, 1000]")
    if "check_interval" in patch:
        out["check_interval"] = int(float(patch["check_interval"]))
        if out["check_interval"] < 5:
            raise ValueError("check_interval must be >= 5")
    if "auto_trade_enabled" in patch:
        out["auto_trade_enabled"] = to_bool(patch["auto_trade_enabled"])

    if "daily_profit_target_pct" in patch:
        out["daily_profit_target_pct"] = float(patch["daily_profit_target_pct"])
        if out["daily_profit_target_pct"] < 0 or out["daily_profit_target_pct"] > 100:
            raise ValueError("daily_profit_target_pct must be in [0, 100]")

    if "daily_loss_limit_pct" in patch:
        out["daily_loss_limit_pct"] = float(patch["daily_loss_limit_pct"])
        if out["daily_loss_limit_pct"] < 0 or out["daily_loss_limit_pct"] > 100:
            raise ValueError("daily_loss_limit_pct must be in [0, 100]")

    if "close_positions_on_stop" in patch:
        out["close_positions_on_stop"] = to_bool(patch["close_positions_on_stop"])

    if "goya_score_enabled" in patch:
        out["goya_score_enabled"] = to_bool(patch["goya_score_enabled"])

    if "goya_min_score" in patch:
        out["goya_min_score"] = int(float(patch["goya_min_score"]))
        if out["goya_min_score"] < 0 or out["goya_min_score"] > 100:
            raise ValueError("goya_min_score must be in [0, 100]")

    if "goya_rank_candidates" in patch:
        out["goya_rank_candidates"] = to_bool(patch["goya_rank_candidates"])

    if "deepseek_enabled" in patch:
        out["deepseek_enabled"] = to_bool(patch["deepseek_enabled"])

    if "deepseek_model" in patch:
        out["deepseek_model"] = str(patch["deepseek_model"] or "").strip() or "deepseek-v4-flash"

    if "deepseek_timeout_sec" in patch:
        out["deepseek_timeout_sec"] = float(patch["deepseek_timeout_sec"])
        if out["deepseek_timeout_sec"] < 1 or out["deepseek_timeout_sec"] > 60:
            raise ValueError("deepseek_timeout_sec must be in [1, 60]")

    if "deepseek_ttl_sec" in patch:
        out["deepseek_ttl_sec"] = int(float(patch["deepseek_ttl_sec"]))
        if out["deepseek_ttl_sec"] < 0 or out["deepseek_ttl_sec"] > 86400:
            raise ValueError("deepseek_ttl_sec must be in [0, 86400]")

    if "deepseek_min_local_score" in patch:
        out["deepseek_min_local_score"] = int(float(patch["deepseek_min_local_score"]))
        if out["deepseek_min_local_score"] < 0 or out["deepseek_min_local_score"] > 100:
            raise ValueError("deepseek_min_local_score must be in [0, 100]")

    if "deepseek_min_confidence" in patch:
        out["deepseek_min_confidence"] = float(patch["deepseek_min_confidence"])
        if out["deepseek_min_confidence"] < 0 or out["deepseek_min_confidence"] > 1:
            raise ValueError("deepseek_min_confidence must be in [0, 1]")

    if "backtest_commission_bps" in patch:
        out["backtest_commission_bps"] = float(patch["backtest_commission_bps"])
        if out["backtest_commission_bps"] < 0 or out["backtest_commission_bps"] > 200:
            raise ValueError("backtest_commission_bps must be in [0, 200]")

    with config_lock:
        CONFIG.update(out)
        save_config(CONFIG)

    return get_public_config()


def _utc_day_key():
    return datetime.now(timezone.utc).date().isoformat()


def _ensure_day_start():
    dk = _utc_day_key()
    if RUNTIME.get("day_key") != dk or RUNTIME.get("day_start_balance") is None:
        RUNTIME["day_key"] = dk
        RUNTIME["day_start_balance"] = float(account.balance)
        RUNTIME["trading_paused_reason"] = None


def set_auto_trade_enabled(enabled, reason=None):
    with config_lock:
        CONFIG["auto_trade_enabled"] = bool(enabled)
        save_config(CONFIG)

    if enabled:
        RUNTIME["trading_paused_reason"] = None
    else:
        RUNTIME["trading_paused_reason"] = str(reason) if reason else "PAUSED"

    return get_public_config()


def close_all_positions(reason="MANUAL"):
    prices = {}
    for pair, ind in (current_prices or {}).items():
        try:
            prices[pair] = float(ind.get("price"))
        except Exception:
            continue
    return account.force_close_all(prices, reason=reason)


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, code, content_type, body_bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_json(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
            return
        if self.path == "/api/state":
            st = account.stats()
            self._send_json(
                200,
                {
                    "stats": st,
                    "config": get_public_config(),
                    "pairs": list(PAIRS.keys()),
                    "pair_stats": build_pair_stats(account.trades),
                    "open_positions": [p for p in account.positions if p.get("status") == "OPEN"],
                    "last_trades": list(reversed(account.trades[-20:])),
                },
            )
            return
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        if self.path.startswith("/api/history"):
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query or "")
                pair = (q.get("pair") or [""])[0].strip()
                tf = (q.get("tf") or ["15m"])[0].strip()
                limit = int(float((q.get("limit") or ["2000"])[0] or 2000))
                limit = max(200, min(limit, 5000))
                zz = float((q.get("zz") or ["1.0"])[0] or 1.0)
                zz = max(0.5, min(zz, 3.0))

                df, tf_norm = get_history(pair, tf=tf)
                df = df.tail(limit)
                candles = _df_to_candles(df)

                tail = df.tail(240)
                ind = get_indicators(tail, pair)

                sig = 0
                reasons = []
                confirm = None

                if tf_norm == "15m":
                    sig, reasons, ind15, ind1h = get_intraday_signal(pair, PAIRS[pair], enforce_hours=False)
                    if ind15 is not None:
                        ind = ind15
                    if ind1h is not None:
                        confirm = {"tf": "1h", "rsi": ind1h.get("rsi"), "trend": ind1h.get("trend"), "price": ind1h.get("price")}
                else:
                    sig, reasons = check_signal(ind, enforce_hours=False)

                swings = _zigzag_swings(candles, _history_threshold(pair, tf_norm) * zz)
                swings = swings[-12:]

                impulse = _elliott_impulse_check(swings)
                swings = _apply_impulse_labels(swings, impulse)

                plan = {"direction": 0}
                try:
                    sl_dist, tp_dist = get_sl_tp_distance(pair)
                    if sig == 1:
                        plan = {
                            "direction": 1,
                            "entry": float(ind.get("price")),
                            "sl": float(ind.get("price") - sl_dist),
                            "tp": float(ind.get("price") + tp_dist),
                            "rr": float(tp_dist / sl_dist) if sl_dist else None,
                        }
                    elif sig == -1:
                        plan = {
                            "direction": -1,
                            "entry": float(ind.get("price")),
                            "sl": float(ind.get("price") + sl_dist),
                            "tp": float(ind.get("price") - tp_dist),
                            "rr": float(tp_dist / sl_dist) if sl_dist else None,
                        }
                except Exception:
                    pass

                self._send_json(
                    200,
                    {
                        "pair": pair,
                        "tf": tf_norm,
                        "candles": candles,
                        "swings": swings,
                        "signal": sig,
                        "signal_reasons": reasons,
                        "confirm": confirm,
                        "elliott": {"impulse": impulse},
                        "plan": plan,
                        "last": {"price": ind.get("price"), "rsi": ind.get("rsi"), "trend": ind.get("trend")},
                    },
                )
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/equity":
            labels, balances = build_equity_series(account.initial, account.trades)
            self._send_json(200, {"labels": labels, "balances": balances})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if TELEGRAM_WEBHOOK_SECRET and self.path == f"/telegram/{TELEGRAM_WEBHOOK_SECRET}":
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(n)
                payload = json.loads(raw.decode("utf-8") or "{}")
                update = telebot.types.Update.de_json(payload)
                if update is not None:
                    bot.process_new_updates([update])
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            return

        if self.path != "/api/config":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n)
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be object")
            cfg = apply_config_patch(payload)
            self._send_json(200, {"ok": True, "config": cfg})
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})

def notify(text):
    try:
        bot.send_message(ADMIN_ID, text, parse_mode='HTML')
    except:
        pass

def fp(pair, ind):
    return fmt_price(pair, ind['price'])


def _is_admin(m):
    try:
        uid = int(getattr(getattr(m, "from_user", None), "id", 0) or 0)
    except Exception:
        uid = 0
    try:
        cid = int(getattr(getattr(m, "chat", None), "id", 0) or 0)
    except Exception:
        cid = 0
    return uid == int(ADMIN_ID) or cid == int(ADMIN_ID)


def _require_admin(m):
    if _is_admin(m):
        return True
    bot.reply_to(m, "Access denied")
    return False


@bot.message_handler(commands=['trading'])
def trading(m):
    with config_lock:
        enabled = bool(CONFIG.get("auto_trade_enabled", True))
    paused_reason = (RUNTIME.get("trading_paused_reason") if isinstance(RUNTIME, dict) else None) or ""
    bot.reply_to(m, f"Trading: {'ON' if enabled else 'OFF'}\nReason: {paused_reason or '—'}")


@bot.message_handler(commands=['pause'])
def pause(m):
    if not _require_admin(m):
        return
    set_auto_trade_enabled(False, reason="MANUAL PAUSE")
    bot.reply_to(m, "Trading paused")


@bot.message_handler(commands=['resume'])
def resume(m):
    if not _require_admin(m):
        return
    set_auto_trade_enabled(True, reason=None)
    bot.reply_to(m, "Trading resumed")


@bot.message_handler(commands=['closeall'])
def closeall(m):
    if not _require_admin(m):
        return
    closed = close_all_positions(reason="MANUAL CLOSE")
    bot.reply_to(m, f"Closed: {len(closed)}")


@bot.message_handler(commands=['start'])
def start(m):
    pairs_line = ", ".join(PAIRS.keys())

    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton('/market'),
        types.KeyboardButton('/signal'),
        types.KeyboardButton('/ai'),
        types.KeyboardButton('/backtest'),
        types.KeyboardButton('/trade'),
        types.KeyboardButton('/trading'),
        types.KeyboardButton('/pause'),
        types.KeyboardButton('/resume'),
        types.KeyboardButton('/closeall'),
        types.KeyboardButton('/status'),
        types.KeyboardButton('/stats'),
        types.KeyboardButton('/dashboard'),
    )

    bot.send_message(
        m.chat.id,
        f"""MULTI-PAIRS BOT
=====================

Pairs: {pairs_line}

Выбери команду кнопкой ниже или введи вручную:
/market
/signal
/trade
/status
/stats
/dashboard
""",
        reply_markup=kb,
    )

@bot.message_handler(commands=['market'])
def market(m):
    text = "<b>Market Data:</b>\n\n"
    for pair, ticker in PAIRS.items():
        if pair in CRYPTO_PAIRS:
            data = update_market_data(pair, tf="15m", bars=200, min_age_sec=60)
        else:
            data = get_data(ticker)

        if data is None or data.empty:
            text += f"{pair}: N/A\n"
            continue

        ind = get_indicators(data.tail(200), pair)
        emoji = "UP" if ind['change'] > 0 else "DOWN"
        text += f"{pair}: {fp(pair, ind)} ({emoji} {ind['change']:+.2f}%) RSI:{ind['rsi']:.0f}\n"
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['signal'])
def signal(m):
    text = "<b>Signals:</b>\n\n"
    best = None
    best_count = 0

    for pair, ticker in PAIRS.items():
        sig, sigs, ind15, ind1h = get_intraday_signal(pair, ticker, enforce_hours=False)
        if ind15 is None:
            continue

        if sig != 0:
            direction = "BUY" if sig == 1 else "SELL"
            conf = "UP" if (ind1h and ind1h.get('trend') == 1) else "DOWN" if (ind1h and ind1h.get('trend') == -1) else "N/A"
            gs = None
            try:
                gs = int(ind15.get("goya_score"))
            except Exception:
                gs = None
            gs_txt = f" Vitality:{gs:+d}" if isinstance(gs, int) else ""
            text += f"{pair}: <b>{direction}</b> TF:15m RSI:{ind15['rsi']:.0f} | 1h:{conf}{gs_txt}\n"
            if len(sigs) > best_count:
                best_count = len(sigs)
                best = (pair, sig, ind15, sigs)

    if best:
        pair, sig, ind, sigs = best
        text += f"\n<b>BEST: {pair}</b>\n"
        text += f"Entry: {fp(pair, ind)}\n"

    bot.reply_to(m, text if "BUY" in text or "SELL" in text else "No signals.", parse_mode='HTML')


@bot.message_handler(commands=['ai'])
def ai_cmd(m):
    parts = (m.text or "").strip().split()
    pair = parts[1].upper().strip() if len(parts) >= 2 else "BTCUSD"
    if pair not in PAIRS:
        bot.reply_to(m, f"Unknown pair: {pair}")
        return

    sig, reasons, ind15, ind1h = get_intraday_signal(pair, PAIRS[pair], enforce_hours=False)
    if ind15 is None:
        bot.reply_to(m, "No data")
        return

    sig_txt = "NO SIGNAL" if sig == 0 else ("BUY" if sig == 1 else "SELL")
    lines = [f"<b>AI</b> {pair} TF:15m • {sig_txt}"]

    for r in (reasons or []):
        if not isinstance(r, str):
            continue
        if r.startswith("VitalityScore:") or r.startswith("GoyaScore:") or r.startswith("DeepSeekScore:") or r.startswith("DXY("):
            lines.append(r)
        if r.startswith("DeepSeek:") or r.startswith("DXY confirm:"):
            lines.append(r)

    bot.reply_to(m, "\n".join(lines), parse_mode='HTML')


@bot.message_handler(commands=['backtest'])
def backtest_cmd(m):
    parts = (m.text or "").strip().split()
    pair = parts[1].upper().strip() if len(parts) >= 2 else "EURUSD"
    tf = parts[2].lower().strip() if len(parts) >= 3 else "1h"

    if pair not in PAIRS:
        bot.reply_to(m, f"Unknown pair: {pair}")
        return

    if pair in CRYPTO_PAIRS and tf not in ["15m", "1h"]:
        bot.reply_to(m, "Для крипты бэктест доступен только на 15m/1h")
        return

    try:
        df, tf_norm = get_history(pair, tf=tf)
    except Exception as e:
        bot.reply_to(m, f"History error: {e}")
        return

    if df is None or df.empty:
        bot.reply_to(m, "No history")
        return

    with config_lock:
        sl = float(CONFIG.get("sl_atr_multiplier", 2.0))
        tp = float(CONFIG.get("tp_atr_multiplier", 6.0))
        tr_on = bool(CONFIG.get("trailing_stop", True))
        tr = float(CONFIG.get("trailing_stop_atr_multiplier", 1.5)) if tr_on else None
        fee = float(CONFIG.get("backtest_commission_bps", 0.0) or 0.0)

    bt = backtest_macd_rsi(df, rsi_min=50.0, macd_fast=12, macd_slow=26, macd_signal=9, sl_atr_mult=sl, tp_atr_mult=tp, trailing_atr_mult=tr, commission_bps=fee)
    mt = backtest_metrics(bt)
    if not mt.get("ok"):
        bot.reply_to(m, f"Backtest error: {mt.get('error')}")
        return

    bot.reply_to(
        m,
        f"<b>Backtest</b> {pair} TF:{tf}\n"
        f"Return: {mt['total_return_pct']:+.2f}%\n"
        f"MaxDD: {mt['max_dd_pct']:.2f}%\n"
        f"Trades: {mt['trades']} | WR: {mt['win_rate_pct']:.1f}%\n"
        f"PF: {mt['profit_factor']:.2f}\n"
        f"Sharpe: {mt['sharpe']:.2f} | Sortino: {mt['sortino']:.2f}",
        parse_mode='HTML',
    )

@bot.message_handler(commands=['trade'])
def trade(m):
    if not _require_admin(m):
        return

    best = None
    best_count = 0
    
    for pair, ticker in PAIRS.items():
        if len([p for p in account.positions if p['pair'] == pair]) >= CONFIG["trades_per_pair"]:
            continue

        sig, sigs, ind15, ind1h = get_intraday_signal(pair, ticker, enforce_hours=False)
        if sig != 0 and ind15 is not None and len(sigs) > best_count:
            best_count = len(sigs)
            best = (pair, sig, ind15, sigs)
    
    if best is None:
        bot.reply_to(m, "No signals!")
        return
    
    pair, sig, ind, sigs = best
    pos = account.open_trade(sig, ind)
    direction = "LONG" if sig == 1 else "SHORT"
    
    notify(f"TRADE [{direction}]\n\nPair: {pair}\nEntry: {fp(pair, ind)}\n\nSignals:\n" + "\n".join(f"- {s}" for s in sigs))
    
    bot.reply_to(m, f"""TRADE [{direction}]

Pair: {pair}
Entry: {fp(pair, ind)}
SL: {fmt_price(pair, pos['sl'])}
TP: {fmt_price(pair, pos['tp'])}
Risk: ${pos['risk']:.2f}

Balance: ${account.balance:.2f}
    """)

@bot.message_handler(commands=['status'])
def status(m):
    st = account.stats()
    open_pos = "\n".join(f"- {p['pair']} {'LONG' if p['direction']==1 else 'SHORT'}" for p in account.positions)
    
    bot.reply_to(m, f"""Account
========

Balance: ${st['balance']:.2f}
Return: {st['return']:+.2f}%

Trades: {st['trades']} | WR: {st['wr']:.1f}%
Open: {st['open']}
{open_pos if open_pos else '(none)'}

Peak: ${st['peak']:.2f}
Max DD: {st['max_dd']:.2f}%
    """)

@bot.message_handler(commands=['stats'])
def stats(m):
    st = account.stats()
    bot.reply_to(m, f"""Stats
=====

Balance: ${st['balance']:.2f}
Return: {st['return']:+.2f}%
Trades: {st['trades']}
Win Rate: {st['wr']:.1f}%
    """)


@bot.message_handler(commands=['dashboard'])
def dashboard_cmd(m):
    stt = account.stats()

    last_trades = account.trades[-5:]
    last_text = "\n".join(
        f"- {t['pair']} {('LONG' if t['direction']==1 else 'SHORT')} {t['status']} ${t.get('pnl', 0):+.2f}"
        for t in reversed(last_trades)
    )

    open_text = "\n".join(
        f"- {p['pair']} {('LONG' if p['direction']==1 else 'SHORT')} Entry:{fmt_price(p['pair'], p['entry'])} SL:{fmt_price(p['pair'], p['sl'])} TP:{fmt_price(p['pair'], p['tp'])}"
        for p in account.positions
        if p.get('status') == 'OPEN'
    )

    bot.reply_to(
        m,
        f"<b>DASHBOARD</b>\n\n"
        f"Balance: ${stt['balance']:.2f}\n"
        f"Return: {stt['return']:+.2f}%\n"
        f"Trades: {stt['trades']} | WR: {stt['wr']:.1f}%\n"
        f"Peak: ${stt['peak']:.2f}\n"
        f"Max DD: {stt['max_dd']:.2f}%\n"
        f"Open: {stt['open']}\n\n"
        f"<b>Open Positions</b>\n{open_text if open_text else '(none)'}\n\n"
        f"<b>Last 5 Trades</b>\n{last_text if last_text else '(none)'}",
        parse_mode='HTML',
    )







def auto_trade():
    while True:
        try:
            _reload_config_if_needed()
            RUNTIME["auto_trade_last_loop_ts"] = time.time()
            now = datetime.now()
            prices = {}
            
            for pair, ticker in PAIRS.items():
                data = None
                if pair in CRYPTO_PAIRS:
                    data = update_market_data(pair, tf="15m", bars=200, min_age_sec=60)
                else:
                    data = get_data(ticker)

                if data is not None and not data.empty:
                    ind = get_indicators(data, pair)
                    prices[pair] = ind['price']
                    current_prices[pair] = ind
            
            if prices and account.positions:
                closed = account.check_all_positions(prices)
                if closed:
                    text = "<b>CLOSED:</b>\n"
                    for pos in closed:
                        text += f"{pos['pair']}: {pos['status']} ${pos['pnl']:+.2f}\n"
                    notify(text)
            
            _ensure_day_start()

            with config_lock:
                enabled = bool(CONFIG.get("auto_trade_enabled", True))
                trades_per_pair = int(CONFIG["trades_per_pair"])
                daily_profit_target_pct = float(CONFIG.get("daily_profit_target_pct", 0) or 0)
                daily_loss_limit_pct = float(CONFIG.get("daily_loss_limit_pct", 0) or 0)
                close_positions_on_stop = bool(CONFIG.get("close_positions_on_stop", False))

            day_start = float(RUNTIME.get("day_start_balance") or account.balance)
            daily_pnl = float(account.balance - day_start)
            daily_pct = (daily_pnl / day_start * 100.0) if day_start else 0.0

            if enabled and daily_profit_target_pct > 0 and daily_pct >= daily_profit_target_pct:
                reason = f"DAILY PROFIT TARGET {daily_pct:.2f}% >= {daily_profit_target_pct:.2f}%"
                set_auto_trade_enabled(False, reason=reason)
                enabled = False
                notify(f"<b>PAUSE</b>\n{reason}")
                if close_positions_on_stop and prices:
                    c = account.force_close_all(prices, reason="PROFIT STOP")
                    if c:
                        notify(f"<b>CLOSE ALL</b>\nReason: PROFIT STOP\nClosed: {len(c)}")

            if enabled and daily_loss_limit_pct > 0 and daily_pct <= -daily_loss_limit_pct:
                reason = f"DAILY LOSS LIMIT {daily_pct:.2f}% <= -{daily_loss_limit_pct:.2f}%"
                set_auto_trade_enabled(False, reason=reason)
                enabled = False
                notify(f"<b>PAUSE</b>\n{reason}")
                if close_positions_on_stop and prices:
                    c = account.force_close_all(prices, reason="LOSS STOP")
                    if c:
                        notify(f"<b>CLOSE ALL</b>\nReason: LOSS STOP\nClosed: {len(c)}")

            candidates = 0
            sample = None

            open_total = len([p for p in account.positions if p.get("status") == "OPEN"])
            max_total = int(CONFIG.get("max_total_positions", 10))

            scored = []

            for pair, ticker in PAIRS.items():
                if len([p for p in account.positions if p['pair'] == pair]) >= trades_per_pair:
                    continue
                if not enabled:
                    continue
                if open_total >= max_total:
                    continue

                sig, sigs, ind15, ind1h = get_intraday_signal(pair, ticker, enforce_hours=False)
                sc = 0
                if ind15 is not None:
                    try:
                        sc = int(ind15.get("goya_score") or 0)
                    except Exception:
                        sc = 0

                if ind15 is not None and sample is None:
                    sample = (pair, sig, list(sigs)[:6], sc)

                if sig != 0 and ind15 is not None:
                    candidates += 1
                    scored.append((abs(sc), sc, pair, sig, sigs, ind15))

            with config_lock:
                rank_on = bool(CONFIG.get("goya_rank_candidates", True))

            if rank_on:
                scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

            slots = max(0, int(max_total) - int(open_total))
            for _, sc, pair, sig, sigs, ind15 in scored[:slots]:
                if len([p for p in account.positions if p['pair'] == pair]) >= trades_per_pair:
                    continue
                if not enabled:
                    break
                if len([p for p in account.positions if p.get("status") == "OPEN"]) >= max_total:
                    break

                pos = account.open_trade(sig, ind15)
                account.save_state()
                RUNTIME["auto_trade_last_open_ts"] = time.time()
                RUNTIME["auto_trade_last_open_pair"] = pair
                direction = "LONG" if sig == 1 else "SHORT"
                notify(
                    f"AUTO [{direction}]\n\nTF: 15m (confirm 1h)\nPair: {pair}\nEntry: {fp(pair, ind15)}\nVitalityScore: {int(sc):+d}\n\n"
                    + "\n".join(f"- {s}" for s in sigs)
                )
                print(f"Auto: {pair} {direction}")
            
            if (now - account.last_report).seconds >= 3600:
                st = account.stats()
                text = f"<b>HOURLY</b>\n\nBalance: ${st['balance']:.2f}\nReturn: {st['return']:+.2f}%\nOpen: {st['open']}\n"
                if account.positions:
                    text += "\nPositions:\n"
                    for p in account.positions:
                        text += f"- {p['pair']}\n"
                notify(text)
                account.last_report = now
                account.save_state()
            
            RUNTIME["auto_trade_last_cycle_ts"] = time.time()

            try:
                now_ts = time.time()
                last_save = float(RUNTIME.get("state_last_save_ts") or 0.0)
                if (now_ts - last_save) >= 30.0:
                    account.save_state()
                    RUNTIME["state_last_save_ts"] = now_ts
            except Exception:
                pass

            try:
                open_cnt = len([p for p in account.positions if p.get("status") == "OPEN"])

                RUNTIME["auto_trade_last_candidates"] = int(candidates)
                if sample is not None:
                    p, s, rs, sc = sample
                    dir_txt = "BUY" if s == 1 else "SELL" if s == -1 else "NO SIGNAL"
                    RUNTIME["auto_trade_last_sample"] = f"{p} {dir_txt} VitalityScore={int(sc):+d}: " + ", ".join(rs)
                else:
                    RUNTIME["auto_trade_last_sample"] = None

                msg = f"[AUTO] enabled={enabled} open={open_cnt} candidates={candidates}"
                if sample is not None:
                    p, s, rs, sc = sample
                    dir_txt = "BUY" if s == 1 else "SELL" if s == -1 else "NO SIGNAL"
                    msg += f" sample={p} {dir_txt} VitalityScore={int(sc):+d}: " + ", ".join(rs)
                print(msg, flush=True)
            except Exception:
                pass

            with config_lock:
                interval = int(CONFIG["check_interval"])
            time.sleep(max(1, interval))
        
        except Exception as e:
            RUNTIME["auto_trade_last_error"] = str(e)
            print(f"Error: {e}", flush=True)
            time.sleep(60)

def run_bot_polling():
    try:
        bot.remove_webhook()
    except:
        pass

    backoff = 5
    while True:
        try:
            bot.polling(none_stop=True)
            backoff = 5
        except ApiTelegramException as e:
            RUNTIME["bot_poll_last_error"] = str(e)
            if getattr(e, "error_code", None) == 409:
                print("Telegram 409 Conflict: another getUpdates is running for this bot token. Ensure only one polling instance is running or enable webhook mode.", flush=True)
                time.sleep(15)
                continue
            print(f"Telegram API error: {e}", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        except Exception as e:
            RUNTIME["bot_poll_last_error"] = str(e)
            print(f"Polling error: {e}", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


def run_http_server():
    port = int(os.environ.get("PORT", "8080") or 8080)
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard: http://0.0.0.0:{port}/")
    server.serve_forever()


def _worker_data_updater():
    print("[DATA] updater started (worker)", flush=True)
    while True:
        try:
            n = 0
            for p in sorted(list(CRYPTO_PAIRS or [])):
                update_market_data(p, tf="15m", bars=1500, min_age_sec=180)
                update_market_data(p, tf="1h", bars=2000, min_age_sec=300)
                n += 1
                time.sleep(0.1)
            print(f"[DATA] updater cycle ok pairs={n}", flush=True)
        except Exception as e:
            print(f"[DATA] updater error: {e}", flush=True)
        time.sleep(30)


def main():
    print("MULTI-PAIRS BOT")

    print("\nPairs:")
    for pair, ticker in PAIRS.items():
        data = get_data(ticker)
        if data is not None and not data.empty:
            ind = get_indicators(data, pair)
            print(f"  {pair}: {fp(pair, ind)}")
        else:
            print(f"  {pair}: N/A")

    with config_lock:
        initial_balance = CONFIG["initial_balance"]
    print(f"\nAccount: ${initial_balance}")

    t1 = threading.Thread(target=auto_trade, daemon=True)
    t1.start()

    t3 = threading.Thread(target=_worker_data_updater, daemon=True)
    t3.start()

    if WEBHOOK_BASE_URL and TELEGRAM_WEBHOOK_SECRET:
        try:
            bot.remove_webhook()
            time.sleep(1)
            url = WEBHOOK_BASE_URL.rstrip("/") + f"/telegram/{TELEGRAM_WEBHOOK_SECRET}"
            ok = bot.set_webhook(url=url)
            print(f"Telegram webhook: {'OK' if ok else 'FAILED'} -> {url}")
            if not ok:
                raise RuntimeError("set_webhook returned False")
        except Exception as e:
            print(f"Webhook setup error: {e}")
            t2 = threading.Thread(target=run_bot_polling, daemon=True)
            t2.start()
    else:
        t2 = threading.Thread(target=run_bot_polling, daemon=True)
        t2.start()

    if str(os.environ.get("RUN_HTTP_SERVER", "1") or "1").strip().lower() not in ["0", "false", "no", "off"]:
        run_http_server()
    else:
        print("HTTP server disabled (RUN_HTTP_SERVER=0)", flush=True)
        while True:
            time.sleep(10)

if __name__ == "__main__":
    main()