"""
EURUSD Trading Bot - MULTI-PAIRS + NOTIFICATIONS
==============================================
Multiple pairs: FX + Crypto
Notifications: Trade alerts + Hourly reports
"""

import telebot
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
import time
import threading
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Missing BOT_TOKEN or ADMIN_ID in .env")

CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
config_lock = threading.Lock()

CONFIG_DEFAULTS = {
    "initial_balance": 1000.0,
    "trades_per_pair": 2,
    "risk_per_trade": 10.0,
    "leverage": 10,
    "sl_pips": 100,
    "tp_pips": 300,
    "check_interval": 1800,
    "auto_trade_enabled": True,
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
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except:
        pass


CONFIG = load_config()

# OPTIMIZED PAIRS (by performance analysis)
# REMOVED: AUDUSD, USDCHF (0% win rate)
PAIRS = {
    "BTCUSD": "BTC-USD",   # BEST: 64% WR, +$428
    "ETHUSD": "ETH-USD",   # 2nd best: 47% WR, +$271
    "USDJPY": "USDJPY=X",  # 40% WR, +$141
    "EURJPY": "EURJPY=X",  # 37% WR, +$96
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "EURGBP": "EURGBP=X",
}

# Timezone for Korea (UTC+9)
def get_seoul_time():
    return datetime.now(timezone(timedelta(hours=9)))

GOOD_HOURS = [5, 7, 8, 18, 19]


def fmt_price(pair, price):
    if pair in ["BTCUSD", "ETHUSD"]:
        return f"{price:.2f}"
    if pair.endswith("JPY"):
        return f"{price:.3f}"
    return f"{price:.5f}"


def get_sl_tp_distance(pair):
    with config_lock:
        sl_pips = float(CONFIG["sl_pips"])
        tp_pips = float(CONFIG["tp_pips"])

    if pair in ["BTCUSD", "ETHUSD"]:
        return sl_pips, tp_pips

    pip = 0.01 if pair.endswith("JPY") else 0.0001
    return sl_pips * pip, tp_pips * pip

# PAIR PRIORITY & RISK MODIFIERS
PAIR_CONFIG = {
    "BTCUSD": {"priority": 1, "risk_mult": 1.5},  # Top performer
    "ETHUSD": {"priority": 2, "risk_mult": 1.2},
    "USDJPY": {"priority": 3, "risk_mult": 1.0},
    "EURJPY": {"priority": 4, "risk_mult": 1.0},
    "EURUSD": {"priority": 5, "risk_mult": 0.8},
    "GBPUSD": {"priority": 6, "risk_mult": 0.8},
    "EURGBP": {"priority": 7, "risk_mult": 0.8},
}

def get_data(ticker, period="5d", interval="1h"):
    try:
        t = yf.Ticker(ticker)
        d = t.history(period=period, interval=interval)
        if d.empty or len(d) < 20:
            return None
        return d
    except:
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
    
    return {
        'price': closes[-1],
        'prev': closes[-2] if len(closes) > 1 else closes[-1],
        'change': change,
        'rsi': rsi,
        'trend': trend,
        'sma': sma,
        'pair': pair_name,
    }

def check_signal(ind, enforce_hours=True):
    signals = []
    rsi = ind['rsi']
    trend = ind['trend']
    price = ind['price']
    sma = ind['sma']
    change = ind['change']

    proxy = -change

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

    buy = sum(1 for s in signals if any(x in s for x in ["DOWN", "Oversold"]))
    sell = sum(1 for s in signals if any(x in s for x in ["UP", "Overbought"]))

    min_signals = 2
    if PAIR_CONFIG.get(ind['pair'], {}).get('priority', 99) > 2:
        min_signals = 3

    if buy >= min_signals and buy > sell:
        return 1, signals
    if sell >= min_signals and sell > buy:
        return -1, signals

    return 0, signals

class Account:
    def __init__(self):
        self.balance = CONFIG["initial_balance"]
        self.initial = CONFIG["initial_balance"]
        self.trades = []
        self.positions = []
        self.peak = self.balance
        self.max_dd = 0
        self.last_report = datetime.now()
        self.load_state()
    
    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    data = json.load(f)
                    self.balance = data.get('balance', CONFIG["initial_balance"])
                    self.initial = data.get('initial', CONFIG["initial_balance"])
                    self.trades = data.get('trades', [])
                    self.peak = data.get('peak', self.balance)
                    self.max_dd = data.get('max_dd', 0)
            except:
                pass
    
    def save_state(self):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'balance': self.balance,
                    'initial': self.initial,
                    'trades': self.trades[-100:],  # keep last 100
                    'peak': self.peak,
                    'max_dd': self.max_dd
                }, f, indent=2)
        except:
            pass
    
    def open_trade(self, direction, ind):
        pair = ind['pair']

        with config_lock:
            risk_pct = float(CONFIG["risk_per_trade"])
            leverage = float(CONFIG["leverage"])
            sl_pips = float(CONFIG["sl_pips"])

        risk = self.balance * (risk_pct / 100)
        sl_dist, tp_dist = get_sl_tp_distance(pair)

        if direction == 1:
            sl_price = ind['price'] - sl_dist
            tp_price = ind['price'] + tp_dist
        else:
            sl_price = ind['price'] + sl_dist
            tp_price = ind['price'] - tp_dist

        lot = risk / (max(sl_pips, 0.00001) * 10) * leverage
        lot = max(0.01, min(lot, 1.0))
        
        pos = {
            'pair': ind['pair'],
            'direction': direction,
            'entry': ind['price'],
            'sl': sl_price,
            'tp': tp_price,
            'lot': lot,
            'risk': risk,
            'time': datetime.now().strftime('%H:%M'),
            'status': 'OPEN'
        }
        self.positions.append(pos)
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
                sl_pips = float(CONFIG["sl_pips"])
                tp_pips = float(CONFIG["tp_pips"])
            
            if pos['direction'] == 1:
                if price >= pos['tp']:
                    pnl = pos['risk'] * (tp_pips / sl_pips)
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
                    pnl = pos['risk'] * (tp_pips / sl_pips)
                    pos['status'] = 'WIN'
                    pos['pnl'] = pnl
                    self.balance += pnl
                    closed.append(pos)
                elif price >= pos['sl']:
                    pos['status'] = 'LOSS'
                    pos['pnl'] = -pos['risk']
                    self.balance += pos['pnl']
                    closed.append(pos)
        
        for pos in closed:
            self.positions.remove(pos)
            self.trades.append(pos)
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
account = Account()
current_prices = {}

DASHBOARD_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <title>LocalTRBot Dashboard</title>
  <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
  <style>
    body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:24px;max-width:1100px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    .card{border:1px solid #ddd;border-radius:10px;padding:14px}
    label{display:block;margin:10px 0 4px;font-size:13px;color:#333}
    input{width:100%;padding:8px;border:1px solid #ccc;border-radius:8px}
    button{padding:10px 12px;border:0;border-radius:8px;background:#111;color:#fff;cursor:pointer}
    .row{display:flex;gap:10px}
    .row>div{flex:1}
    .muted{color:#666;font-size:13px}
    pre{white-space:pre-wrap}
  </style>
</head>
<body>
  <h2>LocalTRBot Dashboard</h2>
  <div class=\"muted\">Bot + Dashboard running as one service (Railway-friendly).</div>

  <div class=\"grid\" style=\"margin-top:16px\">
    <div class=\"card\">
      <h3>Stats</h3>
      <pre id=\"stats\">Loading...</pre>
    </div>
    <div class=\"card\">
      <h3>Control Panel</h3>
      <div class=\"row\">
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

      <div style=\"margin-top:12px\">
        <button onclick=\"saveCfg()\">Save settings</button>
      </div>
      <div class=\"muted\" id=\"saveMsg\" style=\"margin-top:8px\"></div>
    </div>
  </div>

  <div class=\"grid\" style=\"margin-top:16px\">
    <div class=\"card\"><h3>Equity</h3><canvas id=\"equity\"></canvas></div>
    <div class=\"card\"><h3>Win/Loss</h3><canvas id=\"wl\"></canvas></div>
  </div>

<script>
let equityChart, wlChart;

async function loadAll(){
  const st = await fetch('/api/state').then(r=>r.json());
  document.getElementById('stats').textContent = JSON.stringify(st, null, 2);

  for (const k of ['risk_per_trade','trades_per_pair','sl_pips','tp_pips','leverage','check_interval','auto_trade_enabled']){
    document.getElementById(k).value = st.config[k];
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

loadAll();
setInterval(loadAll, 15000);
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

    with config_lock:
        CONFIG.update(out)
        save_config(CONFIG)

    return get_public_config()


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
                    "pair_stats": build_pair_stats(account.trades),
                    "open_positions": [p for p in account.positions if p.get("status") == "OPEN"],
                    "last_trades": list(reversed(account.trades[-20:])),
                },
            )
            return
        if self.path == "/api/equity":
            labels, balances = build_equity_series(account.initial, account.trades)
            self._send_json(200, {"labels": labels, "balances": balances})
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
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

@bot.message_handler(commands=['start'])
def start(m):
    bot.reply_to(m, """MULTI-PAIRS BOT
=====================

Pairs: BTCUSD, ETHUSD, USDJPY, EURJPY, EURUSD, GBPUSD, EURGBP

Commands:
/signal - All signals
/trade - Best trade
/status - Account
/market - Market data
/stats - Performance
/dashboard - Dashboard
    """)

@bot.message_handler(commands=['market'])
def market(m):
    text = "<b>Market Data:</b>\n\n"
    for pair, ticker in PAIRS.items():
        data = get_data(ticker)
        if data is None or data.empty:
            text += f"{pair}: N/A\n"
            continue
        ind = get_indicators(data, pair)
        emoji = "UP" if ind['change'] > 0 else "DOWN"
        text += f"{pair}: {fp(pair, ind)} ({emoji} {ind['change']:+.2f}%) RSI:{ind['rsi']:.0f}\n"
    bot.reply_to(m, text, parse_mode='HTML')

@bot.message_handler(commands=['signal'])
def signal(m):
    text = "<b>Signals:</b>\n\n"
    best = None
    best_count = 0
    
    for pair, ticker in PAIRS.items():
        data = get_data(ticker)
        if data is None or data.empty:
            continue
        ind = get_indicators(data, pair)
        sig, sigs = check_signal(ind, enforce_hours=False)
        
        if sig != 0:
            direction = "BUY" if sig == 1 else "SELL"
            text += f"{pair}: <b>{direction}</b> RSI:{ind['rsi']:.0f}\n"
            if len(sigs) > best_count:
                best_count = len(sigs)
                best = (pair, sig, ind, sigs)
    
    if best:
        pair, sig, ind, sigs = best
        text += f"\n<b>BEST: {pair}</b>\n"
        text += f"Entry: {fp(pair, ind)}\n"
    
    bot.reply_to(m, text if "BUY" in text or "SELL" in text else "No signals.", parse_mode='HTML')

@bot.message_handler(commands=['trade'])
def trade(m):
    best = None
    best_count = 0
    
    for pair, ticker in PAIRS.items():
        if len([p for p in account.positions if p['pair'] == pair]) >= CONFIG["trades_per_pair"]:
            continue
        data = get_data(ticker)
        if data is None or data.empty:
            continue
        ind = get_indicators(data, pair)
        sig, sigs = check_signal(ind, enforce_hours=False)
        if sig != 0 and len(sigs) > best_count:
            best_count = len(sigs)
            best = (pair, sig, ind, sigs)
    
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
def dashboard(m):
    st = account.stats()

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
        f"Balance: ${st['balance']:.2f}\n"
        f"Return: {st['return']:+.2f}%\n"
        f"Trades: {st['trades']} | WR: {st['wr']:.1f}%\n"
        f"Peak: ${st['peak']:.2f}\n"
        f"Max DD: {st['max_dd']:.2f}%\n"
        f"Open: {st['open']}\n\n"
        f"<b>Open Positions</b>\n{open_text if open_text else '(none)'}\n\n"
        f"<b>Last 5 Trades</b>\n{last_text if last_text else '(none)'}",
        parse_mode='HTML',
    )


@bot.message_handler(commands=['dashboard'])
def dashboard(m):
    st = account.stats()

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
        f"Balance: ${st['balance']:.2f}\n"
        f"Return: {st['return']:+.2f}%\n"
        f"Trades: {st['trades']} | WR: {st['wr']:.1f}%\n"
        f"Peak: ${st['peak']:.2f}\n"
        f"Max DD: {st['max_dd']:.2f}%\n"
        f"Open: {st['open']}\n\n"
        f"<b>Open Positions</b>\n{open_text if open_text else '(none)'}\n\n"
        f"<b>Last 5 Trades</b>\n{last_text if last_text else '(none)'}",
        parse_mode='HTML',
    )


@bot.message_handler(commands=['dashboard'])
def dashboard(m):
    st = account.stats()

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
        f"Balance: ${st['balance']:.2f}\n"
        f"Return: {st['return']:+.2f}%\n"
        f"Trades: {st['trades']} | WR: {st['wr']:.1f}%\n"
        f"Peak: ${st['peak']:.2f}\n"
        f"Max DD: {st['max_dd']:.2f}%\n"
        f"Open: {st['open']}\n\n"
        f"<b>Open Positions</b>\n{open_text if open_text else '(none)'}\n\n"
        f"<b>Last 5 Trades</b>\n{last_text if last_text else '(none)'}",
        parse_mode='HTML',
    )

def auto_trade():
    while True:
        try:
            now = datetime.now()
            prices = {}
            
            for pair, ticker in PAIRS.items():
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
            
            with config_lock:
                enabled = bool(CONFIG.get("auto_trade_enabled", True))
                trades_per_pair = int(CONFIG["trades_per_pair"])

            for pair, ticker in PAIRS.items():
                if len([p for p in account.positions if p['pair'] == pair]) >= trades_per_pair:
                    continue
                data = get_data(ticker)
                if data is None or data.empty:
                    continue
                ind = get_indicators(data, pair)
                if not enabled:
                    continue

                sig, sigs = check_signal(ind, enforce_hours=True)
                
                if sig != 0 and len(sigs) >= 2:
                    pos = account.open_trade(sig, ind)
                    account.save_state()
                    direction = "LONG" if sig == 1 else "SHORT"
                    notify(f"AUTO [{direction}]\n\nPair: {pair}\nEntry: {fp(pair, ind)}\n\n" + "\n".join(f"- {s}" for s in sigs))
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
            
            with config_lock:
                interval = int(CONFIG["check_interval"])
            time.sleep(max(1, interval))
        
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

def run_bot_polling():
    try:
        bot.polling(none_stop=True)
    except:
        pass


def run_http_server():
    port = int(os.environ.get("PORT", "8080") or 8080)
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard: http://0.0.0.0:{port}/")
    server.serve_forever()


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

    t2 = threading.Thread(target=run_bot_polling, daemon=True)
    t2.start()

    run_http_server()

if __name__ == "__main__":
    main()