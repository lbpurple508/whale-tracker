import os
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE_URL = "https://data-api.binance.vision"
COOLDOWN_FILE = Path("monitor_cooldown.json")
REJECT_FILE = Path("monitor_rejections.json")
HISTORY_FILE = Path("monitor_history.json")
COOLDOWN_MINUTES = 45
HISTORY_HOURS = 6

MONITORING_TOKENS = [
    "RAREUSDT", "ARKUSDT", "WIFUSDT", "QIUSDT", "MOVEUSDT",
    "STXUSDT", "LSKUSDT", "SYNUSDT", "MOVRUSDT", "NOMUSDT",
    "JASMYUSDT", "TLMUSDT", "GLMRUSDT", "QUICKUSDT", "ACTUSDT",
    "BLURUSDT", "RESOLVUSDT", "AVAUSDT", "DODOUSDT", "PORTALUSDT",
    "VELODROMEUSDT", "EPICUSDT", "SOPHUSDT", "AWEUSDT", "SCRUSDT",
    "HEIUSDT", "TOWNSUSDT", "GTCUSDT", "FTTUSDT", "COOKIEUSDT",
    "QKCUSDT", "GNSUSDT",
]

def format_price(p):
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.5f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.8f}"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}

def save_json(path, data):
    try:
        path.write_text(json.dumps(data))
    except Exception as e:
        print(f"save error: {e}")

def load_cooldown():
    data = load_json(COOLDOWN_FILE)
    now = datetime.utcnow()
    cleaned = {}
    for sym, ts in data.items():
        try:
            t = datetime.fromisoformat(ts)
            if (now - t).total_seconds() < COOLDOWN_MINUTES * 60:
                cleaned[sym] = ts
        except Exception:
            pass
    return cleaned

def get_alert_count(symbol):
    history = load_json(HISTORY_FILE)
    now = datetime.utcnow()
    if symbol not in history:
        return 0
    events = history[symbol].get("events", [])
    count = 0
    for ts_str in events:
        try:
            t = datetime.fromisoformat(ts_str)
            if (now - t).total_seconds() < HISTORY_HOURS * 3600:
                count += 1
        except Exception:
            pass
    return count

def record_alert(symbol):
    history = load_json(HISTORY_FILE)
    now = datetime.utcnow()
    if symbol not in history:
        history[symbol] = {"events": []}
    history[symbol]["events"].append(now.isoformat())
    history[symbol]["events"] = [
        ts for ts in history[symbol]["events"]
        if (now - datetime.fromisoformat(ts)).total_seconds() < HISTORY_HOURS * 3600
    ]
    save_json(HISTORY_FILE, history)

def log_rejection(symbol, reasons, price, change_24h):
    try:
        data = load_json(REJECT_FILE)
        key = symbol
        if key not in data:
            data[key] = {"symbol": symbol, "count": 0}
        data[key]["count"] += 1
        data[key]["last_seen"] = datetime.utcnow().isoformat()
        data[key]["price"] = price
        data[key]["change_24h"] = change_24h
        data[key]["top_reason"] = reasons[0] if reasons else "unknown"
        if len(data) > 200:
            sorted_items = sorted(data.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)
            data = dict(sorted_items[:200])
        save_json(REJECT_FILE, data)
    except Exception as e:
        print(f"reject log error: {e}")

def btc_is_healthy():
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=2"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return True
        current_close = float(data[-1][4])
        prev_close = float(data[-2][4])
        if prev_close == 0:
            return True
        change = ((current_close - prev_close) / prev_close) * 100
        print(f"BTC 1h change: {change:.2f}%")
        return change > -3
    except Exception:
        return True

def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_rsi_series(closes, period=14):
    """Return full RSI series."""
    if len(closes) < period + 1:
        return []
    rsis = []
    for i in range(period, len(closes)):
        window = closes[i-period:i+1]
        gains, losses = [], []
        for j in range(1, len(window)):
            diff = window[j] - window[j-1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            rsis.append(100)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100 - (100 / (1 + rs)))
    return rsis

def check_depth(symbol, price):
    try:
        url = f"{BASE_URL}/api/v3/depth?symbol={symbol}&limit=100"
        r = requests.get(url, timeout=10)
        book = r.json()
        low, high = price * 0.98, price * 1.02
        bid_depth = sum(float(b[1]) * float(b[0]) for b in book.get("bids", []) if float(b[0]) >= low)
        ask_depth = sum(float(a[1]) * float(a[0]) for a in book.get("asks", []) if float(a[0]) <= high)
        return bid_depth, ask_depth
    except Exception:
        return 0, 0

def detect_stage(symbol):
    """
    Returns:
      ("COILING", data)  - pre-pump accumulation (WATCH)
      ("BREAKOUT", data) - breakout candle fired (ENTER)
      (None, reasons)    - no signal
    """
    reasons = []
    try:
        url = f"{BASE_URL}/api/v3/klines?symbol={symbol}&interval=15m&limit=48"
        r = requests.get(url, timeout=15)
        klines = r.json()
        if not isinstance(klines, list) or len(klines) < 30:
            return None, ["not_enough_klines"]

        current_close = float(klines[-1][4])
        current_open = float(klines[-1][1])
        current_high = float(klines[-1][2])
        current_low = float(klines[-1][3])
        current_vol = float(klines[-1][5])
        current_taker = float(klines[-1][9])

        # Volume baseline (20 candles before current)
        prior_vols = [float(k[5]) for k in klines[-21:-1]]
        avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0
        if avg_vol == 0:
            return None, ["avg_vol_zero"]
        vol_ratio = current_vol / avg_vol

        # 6h range
        highs_6h = [float(k[2]) for k in klines[-24:]]
        lows_6h = [float(k[3]) for k in klines[-24:]]
        range_pct = ((max(highs_6h) - min(lows_6h)) / min(lows_6h)) * 100

        # 6h change
        price_6h_ago = float(klines[-25][4])
        change_6h = ((current_close - price_6h_ago) / price_6h_ago) * 100

        # 1h change
        price_1h_ago = float(klines[-5][4])
        change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100

        # RSI current and 2h ago
        closes = [float(k[4]) for k in klines]
        rsi_series = compute_rsi_series(closes, 14)
        if not rsi_series:
            return None, ["rsi_fail"]
        rsi_now = rsi_series[-1]
        rsi_2h_ago = rsi_series[-9] if len(rsi_series) >= 9 else rsi_now
        rsi_4h_ago = rsi_series[-17] if len(rsi_series) >= 17 else rsi_now

        rsi_declining = rsi_now < rsi_2h_ago

        # Buy pressure last 2h
        recent_taker = sum(float(k[9]) for k in klines[-8:])
        recent_total = sum(float(k[5]) for k in klines[-8:])
        buy_pressure = recent_taker / recent_total if recent_total > 0 else 0

        # Depth
        bid_depth, ask_depth = check_depth(symbol, current_close)

        # ============================
        # BREAKOUT CHECK (Priority)
        # ============================
        breakout_conditions = []
        if current_close > current_open:  # green candle
            breakout_conditions.append(True)
        else:
            breakout_conditions.append(False)

        if vol_ratio >= 3:  # 3x volume on current candle
            breakout_conditions.append(True)
        else:
            breakout_conditions.append(False)

        if change_1h >= 3:  # up 3%+ in last hour
            breakout_conditions.append(True)
        else:
            breakout_conditions.append(False)

        if 40 <= rsi_now <= 72:  # RSI in valid range
            breakout_conditions.append(True)
        else:
            breakout_conditions.append(False)

        # Not already extended
        if change_6h <= 20:
            breakout_conditions.append(True)
        else:
            breakout_conditions.append(False)

        if all(breakout_conditions):
            return "BREAKOUT", {
                "price": current_close,
                "vol_ratio": vol_ratio,
                "rsi": rsi_now,
                "rsi_2h_ago": rsi_2h_ago,
                "rsi_declining": rsi_declining,
                "change_1h": change_1h,
                "change_6h": change_6h,
                "range_pct": range_pct,
                "buy_pressure": buy_pressure * 100,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth,
            }

        # ============================
        # COILING CHECK (Pre-pump WATCH)
        # ============================
        coiling_reasons = []

        # RSI in 30-50 AND declining (bear trap pattern)
        if rsi_now < 30 or rsi_now > 55:
            coiling_reasons.append(f"RSI_{rsi_now:.1f}")

        if not rsi_declining:
            coiling_reasons.append("rsi_rising")

        # Tight range
        if range_pct > 10:
            coiling_reasons.append(f"range_{range_pct:.1f}%")

        # 6h change small
        if change_6h < -5 or change_6h > 5:
            coiling_reasons.append(f"6h_{change_6h:.1f}%")

        # Volume creeping 1.0x - 2.5x
        if vol_ratio < 1.0:
            coiling_reasons.append(f"vol_{vol_ratio:.1f}x_low")
        if vol_ratio > 2.5:
            coiling_reasons.append(f"vol_{vol_ratio:.1f}x_high")

        # Buy pressure
        if buy_pressure < 0.50:
            coiling_reasons.append(f"buy_{buy_pressure*100:.1f}%")

        # Green candles
        greens = sum(1 for k in klines[-6:] if float(k[4]) > float(k[1]))
        if greens < 3:
            coiling_reasons.append(f"greens_{greens}")

        # Depth
        if bid_depth < 5_000:
            coiling_reasons.append(f"bid_{bid_depth:.0f}")
        if ask_depth < 5_000:
            coiling_reasons.append(f"ask_{ask_depth:.0f}")

        if coiling_reasons:
            return None, coiling_reasons

        return "COILING", {
            "price": current_close,
            "vol_ratio": vol_ratio,
            "rsi": rsi_now,
            "rsi_2h_ago": rsi_2h_ago,
            "rsi_4h_ago": rsi_4h_ago,
            "rsi_declining": rsi_declining,
            "change_1h": change_1h,
            "change_6h": change_6h,
            "range_pct": range_pct,
            "buy_pressure": buy_pressure * 100,
            "greens": greens,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }
    except Exception as e:
        return None, [f"exception_{e}"]

def scan():
    print(f"Scanning {len(MONITORING_TOKENS)} monitoring tokens...")
    cooldown = load_cooldown()
    hits = []
    rejection = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(detect_stage, s): s for s in MONITORING_TOKENS}
        for future in as_completed(futures):
            symbol = futures[future]
            if symbol in cooldown:
                continue
            stage, data = future.result()
            if stage:
                data["symbol"] = symbol
                data["stage"] = stage
                hits.append(data)
            else:
                if isinstance(data, list) and data:
                    top = data[0].split("_")[0]
                    rejection[top] = rejection.get(top, 0) + 1
                    log_rejection(symbol, data, 0, 0)
    print(f"Rejection: {rejection}")
    now = datetime.utcnow()
    for h in hits:
        cooldown[h["symbol"]] = now.isoformat()
    save_json(COOLDOWN_FILE, cooldown)
    return hits

def get_session_label(hour, minute):
    if hour == 5 and minute >= 30:
        return "Asia"
    if 6 <= hour <= 10:
        return "Asia"
    if hour == 11 and minute < 30:
        return "Asia"
    if hour == 12 and minute >= 30:
        return "Europe"
    if 13 <= hour <= 14:
        return "Europe"
    if hour == 15 and minute < 30:
        return "Europe"
    if hour == 18 and minute >= 30:
        return "US"
    if 19 <= hour <= 20:
        return "US"
    if hour == 21 and minute < 30:
        return "US"
    return "Dead-Zone"

def main():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    session = get_session_label(ist.hour, ist.minute)
    print(f"Monitoring Scanner starting at {ist} IST — Session: {session}")

    if not btc_is_healthy():
        print("BTC dumping >3%. Skipping.")
        return

    hits = scan()
    print(f"Found {len(hits)} signals")

    for h in hits:
        count = get_alert_count(h["symbol"]) + 1
        stop = h["price"] * 0.97
        stage = h.get("stage", "COILING")

        if stage == "BREAKOUT":
            header = f"🚀🚀 <b>MONITORING BREAKOUT — ENTER NOW</b> [{session}]"
        elif count == 1:
            header = f"⭐ <b>MONITORING COILING (WATCH)</b> [{session}]"
        elif count == 2:
            header = f"⭐⭐ <b>MONITORING COILING — 2ND (WATCH)</b> [{session}]"
        else:
            header = f"🎯🎯 <b>MONITORING COILING — {count}X</b> [{session}]"

        record_alert(h["symbol"])

        msg = (
            f"{header}\n\n"
            f"<b>Stage:</b> {stage}\n"
            f"<b>Alerts (6h):</b> {count}\n"
            f"<b>Coin:</b> {h['symbol']} ⭐ MONITORING\n"
            f"<b>Price:</b> {format_price(h['price'])}\n"
            f"<b>6h Range:</b> {h['range_pct']:.2f}%\n"
            f"<b>6h Change:</b> {h['change_6h']:+.2f}%\n"
            f"<b>1h Change:</b> {h['change_1h']:+.2f}%\n"
            f"<b>RSI now:</b> {h['rsi']:.1f}\n"
            f"<b>RSI 2h ago:</b> {h.get('rsi_2h_ago', 'N/A')}\n"
            f"<b>RSI Declining:</b> {'YES' if h.get('rsi_declining') else 'NO'}\n"
            f"<b>Vol Ratio:</b> {h['vol_ratio']:.2f}x\n"
            f"<b>Buy Pressure:</b> {h['buy_pressure']:.1f}%\n"
            f"<b>Bid:</b> ${h['bid_depth']:,.0f}\n"
            f"<b>Ask:</b> ${h['ask_depth']:,.0f}\n"
            f"<b>Time:</b> {ist.strftime('%H:%M:%S')} IST\n\n"
            f"📋 <b>PLAN</b>\n"
            f"Entry: {format_price(h['price'])}\n"
            f"Stop: {format_price(stop)} (-3%)\n"
            f"Trail: +3%→BE, +5%→+2%, +10%→+6%, +25%→+18%\n\n"
        )

        if stage == "BREAKOUT":
            msg += "🚀 BREAKOUT FIRED — ENTER IMMEDIATELY\n⚠️ Monitoring token whale play"
        else:
            msg += "⚠️ COILING — whale preparing\n⚠️ Wait for BREAKOUT alert to enter"

        send_telegram(msg)

def safe_main():
    try:
        main()
    except Exception as e:
        send_telegram(f"🚨 <b>MONITORING SCANNER CRASHED</b>\n\n<b>Error:</b> {str(e)[:300]}")
        raise

if __name__ == "__main__":
    safe_main()
